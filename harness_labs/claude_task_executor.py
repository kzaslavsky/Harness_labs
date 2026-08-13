"""Live Claude-backed semantic task execution for the hybrid controller.

This mirrors :class:`harness_labs.controller_live.CodexSemanticTaskExecutor`
over headless Claude Code (`claude -p`). One enforcement difference is
deliberate: `codex exec --sandbox read-only` is an OS-level sandbox, while
`claude -p` bounds tools at the permission layer. To compensate, read-only
Claude workers receive no shell or edit tools at all, and every execution is
wrapped in a workspace snapshot: a read-only worker whose repository state
changed fails outright, exactly like a writable worker that escapes its grant.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic_ns
from typing import Any, Mapping

from .attempts import TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
from .controller_evidence import EvidenceCatalog, EvidenceError
from .controller_live import (
    _RAW_OUTPUT_SCHEMA,
    _WORKSPACE_CHANGE_RECEIPT_KIND,
    LiveExecutionError,
    _filter_satisfied_criteria,
    _parse_context,
    _snapshot_delta_paths,
    _worker_prompt,
)
from .controller_results import (
    DeliverableFloorViolation,
    enforce_deliverable_floor,
    semantic_payload,
    validate_semantic_result,
)
from .git_transaction import (
    GitTransactionError,
    normalize_allowed_paths,
    paths_outside_scope,
    workspace_snapshot,
)
from .usage import ModelPrice, parse_claude_result_usage, usage_payload


_READ_ONLY_TOOLS = "Read,Glob,Grep"
_WORKSPACE_WRITE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"


@dataclass
class ClaudeSemanticTaskExecutor:
    """Execute one repository task with a fresh, policy-bounded Claude process."""

    task: Mapping[str, Any]
    repository: Path
    evidence: EvidenceCatalog
    role_instructions: str
    model: str = "claude-sonnet-5"
    effort: str = "medium"
    executable: str = "claude"
    timeout_seconds: float | None = None
    preflight_argv: tuple[str, ...] = ()
    preflight_timeout_seconds: float | None = None
    require_preflight_success: bool = False
    sandbox: str = "read-only"
    require_repository_change: bool = False
    forbid_repository_change: bool = False
    writable_paths: tuple[str, ...] = ()
    # Deprecated: superseded by ``dirty_baseline_grant``, which binds a
    # per-dispatch adoption to a specific receipted change set instead of
    # blanket-accepting any dirty baseline. Accepted only so callers built
    # against the prior constructor keep working; it has no effect on the
    # writable preflight.
    allow_dirty_baseline: bool = False
    dirty_baseline_grant: Mapping[str, Any] | None = None
    audit: AuditJournal | None = field(default=None, repr=False)
    pricing: ModelPrice | None = None

    def __post_init__(self) -> None:
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox must be read-only or workspace-write")
        if self.require_repository_change and self.sandbox != "workspace-write":
            raise ValueError(
                "require_repository_change requires the workspace-write sandbox"
            )
        if self.require_repository_change and self.forbid_repository_change:
            raise ValueError(
                "repository changes cannot be both required and forbidden"
            )
        if self.sandbox == "workspace-write":
            if not self.writable_paths:
                raise ValueError("workspace-write requires explicit writable_paths")
            normalize_allowed_paths(self.writable_paths)
        elif self.writable_paths:
            raise ValueError("writable_paths require the workspace-write sandbox")
        if self.allow_dirty_baseline and self.sandbox != "workspace-write":
            raise ValueError("allow_dirty_baseline requires the workspace-write sandbox")
        if self.dirty_baseline_grant is not None:
            if self.sandbox != "workspace-write":
                raise ValueError(
                    "dirty_baseline_grant requires the workspace-write sandbox"
                )
            receipt_ref = (
                self.dirty_baseline_grant.get("receipt_ref")
                if isinstance(self.dirty_baseline_grant, Mapping)
                else None
            )
            if not isinstance(receipt_ref, str) or not receipt_ref.strip():
                raise ValueError(
                    "dirty_baseline_grant must name a receipt_ref"
                )
        if self.require_preflight_success and not self.preflight_argv:
            raise ValueError("require_preflight_success requires a preflight command")

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        try:
            return self._execute(attempt)
        except DeliverableFloorViolation as exc:
            self._audit_deliverable_floor_violation(attempt, exc)
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "field": exc.field,
                    "reason": exc.reason,
                },
            )
        except (
            GitTransactionError,
            LiveExecutionError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )

    def _execute(self, attempt: TaskAttempt) -> TaskResult:
        repository = self.repository.resolve(strict=True)
        if not (repository / ".git").exists():
            # Git worktrees use a .git file, while ordinary repositories use a dir.
            raise LiveExecutionError("repository is not a Git worktree")
        claude = shutil.which(self.executable)
        if claude is None:
            raise LiveExecutionError(
                f"Claude executable not found: {self.executable}"
            )

        context = _parse_context(str(self.task.get("context", "")))
        if self.sandbox == "workspace-write":
            context["controller_writable_paths"] = list(
                normalize_allowed_paths(self.writable_paths)
            )
        # Snapshot both sandboxes: Claude's read-only bound is permission-layer,
        # so the controller proves non-mutation rather than assuming it.
        initial_workspace = workspace_snapshot(repository)
        adoption_grant = None
        if self.sandbox == "workspace-write" and initial_workspace["changed_paths"]:
            adoption_grant = self._resolve_dirty_baseline_grant(initial_workspace)
        artifact_kind = context.pop("artifact_kind", None)
        if (
            not isinstance(artifact_kind, str)
            or not artifact_kind.strip()
            or artifact_kind == _WORKSPACE_CHANGE_RECEIPT_KIND
        ):
            artifact_kind = f"{self.task['details_schema']}-report"
        preflight_artifact = None
        if self.preflight_argv:
            preflight = self._run_preflight(attempt, repository)
            if self.require_preflight_success and preflight["exit_code"] != 0:
                raise LiveExecutionError(
                    "controller-owned preflight failed with status "
                    f"{preflight['exit_code']}: {preflight['stderr'][-1000:]}"
                )
            context["controller_verified_command"] = {
                "argv": list(self.preflight_argv),
                "cwd": str(repository),
                "exit_code": preflight["exit_code"],
                "stdout": preflight["stdout"],
                "stderr": preflight["stderr"],
            }
            preflight_artifact = self.evidence.add(
                kind="verified-command-output",
                content=context["controller_verified_command"],
                media_type="application/json",
                producer_task_id=str(self.task["id"]),
            )
        prompt = _worker_prompt(self.task, context, self.role_instructions)

        argv = [
            claude,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_RAW_OUTPUT_SCHEMA, sort_keys=True),
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--tools",
            (
                _WORKSPACE_WRITE_TOOLS
                if self.sandbox == "workspace-write"
                else _READ_ONLY_TOOLS
            ),
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if self.sandbox == "workspace-write":
            argv.append("--dangerously-skip-permissions")
        started_ns = monotonic_ns()
        try:
            completed = subprocess.run(
                argv,
                cwd=repository,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LiveExecutionError("Claude semantic task timed out") from exc
        result_payload = self._parse_result_envelope(completed)
        self._audit_transport(
            attempt,
            argv,
            prompt,
            completed,
            result_payload,
            (monotonic_ns() - started_ns) // 1_000_000,
            adoption_grant,
        )
        if completed.returncode != 0:
            detail = (completed.stderr.strip() or completed.stdout.strip())[-2000:]
            raise LiveExecutionError(
                f"Claude exited with status {completed.returncode}: {detail}"
            )
        if result_payload is None:
            raise LiveExecutionError("Claude did not return a JSON result envelope")
        if result_payload.get("is_error", False):
            detail = str(result_payload.get("result", ""))[-2000:]
            raise LiveExecutionError(f"Claude reported an execution error: {detail}")
        raw = _structured_output(result_payload)

        final_workspace = workspace_snapshot(repository)
        if final_workspace["head"] != initial_workspace["head"]:
            raise LiveExecutionError("worker changed repository HEAD")
        if final_workspace["branch"] != initial_workspace["branch"]:
            raise LiveExecutionError("worker changed repository branch")
        worker_changed_paths = _snapshot_delta_paths(
            initial_workspace,
            final_workspace,
        )
        workspace_artifact = None
        if self.sandbox == "read-only":
            if worker_changed_paths:
                raise LiveExecutionError(
                    "read-only worker changed repository paths: "
                    + ", ".join(worker_changed_paths)
                )
        else:
            outside = paths_outside_scope(worker_changed_paths, self.writable_paths)
            if outside:
                raise LiveExecutionError(
                    "writable worker changed paths outside its grant: "
                    + ", ".join(outside)
                )
            if self.require_repository_change and not worker_changed_paths:
                raise LiveExecutionError(
                    "writable worker completed without changing the repository"
                )
            if self.forbid_repository_change and worker_changed_paths:
                raise LiveExecutionError(
                    "writable verifier changed repository paths: "
                    + ", ".join(worker_changed_paths)
                )
            workspace_artifact = self.evidence.add(
                kind=_WORKSPACE_CHANGE_RECEIPT_KIND,
                content={
                    "protocol": "workspace-change-receipt/2",
                    "repository": str(repository),
                    "baseline_head": initial_workspace["head"],
                    "branch": initial_workspace["branch"],
                    "dirty_baseline_grant": adoption_grant,
                    "baseline_changed_paths": initial_workspace["changed_paths"],
                    "allowed_paths": list(
                        normalize_allowed_paths(self.writable_paths)
                    ),
                    "changed_paths": final_workspace["changed_paths"],
                    "worker_changed_paths": worker_changed_paths,
                    "baseline_files": initial_workspace["files"],
                    "files": final_workspace["files"],
                },
                media_type="application/json",
                producer_task_id=str(self.task["id"]),
            )

        deliverable = raw.get("deliverable_markdown")
        enforce_deliverable_floor(deliverable, "deliverable_markdown")
        details = json.loads(raw["details_json"])
        if not isinstance(details, Mapping):
            raise LiveExecutionError("Claude details_json must encode an object")

        artifact = self.evidence.add(
            kind=artifact_kind,
            content=deliverable.strip() + "\n",
            media_type="text/markdown",
            producer_task_id=str(self.task["id"]),
        )
        evidence_refs = [artifact.ref]
        artifacts = [artifact.as_dict()]
        if preflight_artifact is not None:
            evidence_refs.append(preflight_artifact.ref)
            artifacts.append(preflight_artifact.as_dict())
        if workspace_artifact is not None:
            evidence_refs.append(workspace_artifact.ref)
            artifacts.append(workspace_artifact.as_dict())
        satisfied = _filter_satisfied_criteria(
            raw.get("satisfied_criteria", []),
            accepted_criteria=set(self.task.get("acceptance_criteria", ())),
            audit=self.audit,
            attempt=attempt,
            backend_id="claude-print",
        )
        payload = semantic_payload(
            summary=str(raw["summary"]),
            details_schema=str(self.task["details_schema"]),
            details=dict(details),
            claims=tuple(
                {
                    **dict(item),
                    "evidence_refs": evidence_refs,
                }
                for item in raw.get("claims", [])
            ),
            findings=tuple(
                {
                    **dict(item),
                    "evidence_refs": evidence_refs,
                    "source_finding_ids": [],
                }
                for item in raw.get("findings", [])
            ),
            artifacts=tuple(artifacts),
            criterion_coverage=tuple(
                {
                    "criterion_id": criterion_id,
                    "status": "satisfied",
                    "evidence_refs": evidence_refs,
                }
                for criterion_id in satisfied
            ),
            recommendations=tuple(raw.get("recommendations", [])),
            unresolved_questions=tuple(raw.get("unresolved_questions", [])),
        )
        result = TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=payload,
            evidence=(
                *evidence_refs,
                "model-backend:claude-print",
                f"repository:{repository}",
            ),
        )
        validate_semantic_result(
            result,
            expected_details_schema=str(self.task["details_schema"]),
        )
        return result

    @staticmethod
    def _parse_result_envelope(
        completed: subprocess.CompletedProcess[str],
    ) -> Mapping[str, Any] | None:
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, Mapping) else None

    def _audit_deliverable_floor_violation(
        self,
        attempt: TaskAttempt,
        exc: DeliverableFloorViolation,
    ) -> None:
        if self.audit is None:
            return
        self.audit.append(
            "deliverable_floor_refused",
            status="failed",
            payload={"field": exc.field, "reason": exc.reason},
            actor=AuditActor(
                attempt.attempt_id,
                "capability_adapter",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="subprocess",
        )

    def _run_preflight(
        self,
        attempt: TaskAttempt,
        repository: Path,
    ) -> dict[str, Any]:
        started_ns = monotonic_ns()
        try:
            completed = subprocess.run(
                list(self.preflight_argv),
                cwd=repository,
                text=True,
                capture_output=True,
                timeout=self.preflight_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LiveExecutionError("controller-owned preflight timed out") from exc
        receipt = {
            "argv": list(self.preflight_argv),
            "cwd": str(repository),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "duration_ms": (monotonic_ns() - started_ns) // 1_000_000,
        }
        if self.audit is not None:
            output_artifact = self.audit.write_artifact(
                "controller-command-receipt",
                receipt,
            )
            self.audit.append(
                "verified_command_completed",
                status=("succeeded" if completed.returncode == 0 else "failed"),
                payload={
                    "argv": list(self.preflight_argv),
                    "cwd": str(repository),
                    "exit_code": completed.returncode,
                },
                actor=AuditActor(
                    attempt.attempt_id,
                    "capability_adapter",
                    parent_id=attempt.parent_attempt_id,
                ),
                attempt_id=attempt.attempt_id,
                parent_attempt_id=attempt.parent_attempt_id,
                backend_id="subprocess",
                duration_ms=receipt["duration_ms"],
                artifacts=(output_artifact,),
            )
        return receipt

    def _resolve_dirty_baseline_grant(
        self, initial_workspace: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Accept a dirty baseline only for a grant whose receipt covers it exactly.

        The grant must name an existing ``workspace-change-receipt`` evidence
        entry left by a prior attempt in this run whose recorded
        ``changed_paths`` covers every currently dirty path *and* whose
        recorded ``files`` content state matches what is on disk right now;
        a missing, unresolvable, path-incomplete, or content-mismatched grant
        refuses with the same clean-baseline message as no grant at all, so
        neither a shared path name nor a forged path list can substitute for
        the receipted change set's actual attested content.
        """

        dirty_paths: list[str] = initial_workspace["changed_paths"]
        dirty_files: Mapping[str, Any] = initial_workspace["files"]
        grant = self.dirty_baseline_grant
        receipt_ref = grant.get("receipt_ref") if isinstance(grant, Mapping) else None
        receipted_paths: set[str] = set()
        receipted_files: Mapping[str, Any] = {}
        if isinstance(receipt_ref, str) and receipt_ref.strip():
            try:
                record = self.evidence.metadata(receipt_ref)
                if record.kind == _WORKSPACE_CHANGE_RECEIPT_KIND:
                    receipt = json.loads(self.evidence.open(receipt_ref))
                    if isinstance(receipt, Mapping):
                        receipted_paths = set(receipt.get("changed_paths", ()))
                        raw_files = receipt.get("files")
                        if isinstance(raw_files, Mapping):
                            receipted_files = raw_files
            except (EvidenceError, json.JSONDecodeError):
                receipted_paths = set()
                receipted_files = {}
        covered = bool(receipted_paths) and set(dirty_paths) <= receipted_paths
        if covered:
            covered = all(
                dirty_files.get(path) == receipted_files.get(path)
                for path in dirty_paths
            )
        if not covered:
            raise LiveExecutionError(
                "writable worker requires a clean repository baseline"
            )
        return {"receipt_ref": receipt_ref, "receipted_paths": sorted(receipted_paths)}

    def _audit_transport(
        self,
        attempt: TaskAttempt,
        argv: list[str],
        prompt: str,
        completed: subprocess.CompletedProcess[str],
        result_payload: Mapping[str, Any] | None,
        duration_ms: int,
        adoption_grant: Mapping[str, Any] | None,
    ) -> None:
        if self.audit is None:
            return
        prompt_artifact = self.audit.write_artifact(
            "live-worker-prompt",
            prompt,
            media_type="text/plain",
        )
        stdout_artifact = self.audit.write_artifact(
            "live-worker-stdout",
            completed.stdout,
            media_type="application/json",
        )
        stderr_artifact = self.audit.write_artifact(
            "live-worker-stderr",
            completed.stderr,
            media_type="text/plain",
        )
        normalized_usage = (
            parse_claude_result_usage(result_payload)
            if result_payload is not None
            else None
        )
        permission_denials = (
            result_payload.get("permission_denials", [])
            if result_payload is not None
            else []
        )
        self.audit.append(
            "backend_transport",
            status="succeeded" if completed.returncode == 0 else "failed",
            payload={
                "transport": "claude-print",
                "argv": argv,
                "returncode": completed.returncode,
                "repository": str(self.repository),
                "model": self.model,
                "effort": self.effort,
                "sandbox": self.sandbox,
                "writable_paths": list(self.writable_paths),
                "forbid_repository_change": self.forbid_repository_change,
                "dirty_baseline_grant": adoption_grant,
                "permission_denials": permission_denials,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "usage": (
                    usage_payload(
                        model=self.model,
                        pricing=self.pricing,
                        **normalized_usage,
                    )
                    if normalized_usage is not None
                    else None
                ),
            },
            actor=AuditActor(
                attempt.attempt_id,
                "semantic_worker",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="claude-print",
            duration_ms=duration_ms,
            artifacts=(prompt_artifact, stdout_artifact, stderr_artifact),
        )


def _structured_output(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the schema-validated object from a claude -p result envelope."""

    structured = payload.get("structured_output")
    if isinstance(structured, Mapping):
        return structured
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise LiveExecutionError(
                "Claude result is not the schema-bound JSON object"
            ) from exc
        if isinstance(parsed, Mapping):
            return parsed
    raise LiveExecutionError("Claude did not return a structured semantic result")


__all__ = ["ClaudeSemanticTaskExecutor"]
