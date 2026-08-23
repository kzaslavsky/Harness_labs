"""Live Claude-backed semantic task execution for the hybrid controller.

This mirrors :class:`harness_labs.core.controller_live.CodexSemanticTaskExecutor`
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
from typing import Any, Mapping, Sequence

from harness_labs.core.attempts import TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import (
    ATTEMPT_START_BASELINE_RESTORATION_EVENT,
    DELIVERABLE_FLOOR_RETRY_LIMIT,
    _RAW_OUTPUT_SCHEMA,
    _WORKSPACE_CHANGE_RECEIPT_KIND,
    LiveExecutionError,
    deliverable_floor_retry_addendum,
    extract_park_disposition,
    _filter_satisfied_criteria,
    _is_latest_writable_attempt,
    _parse_context,
    _record_writable_attempt_started,
    _snapshot_delta_paths,
    _worker_prompt,
    dirty_baseline_grant_refusal_detail,
    restore_attempt_start_baseline,
    select_dirty_baseline_receipt,
    semantic_shape_retry_addendum,
    verify_dirty_baseline_grant,
)
from harness_labs.core.controller_results import (
    DeliverableFloorViolation,
    SemanticResultError,
    enforce_deliverable_floor,
    semantic_payload,
    validate_semantic_result,
)
from harness_labs.core.git_transaction import (
    GitTransactionError,
    normalize_allowed_paths,
    paths_outside_scope,
    workspace_snapshot,
)
from harness_labs.core.usage import ModelPrice, parse_claude_result_usage, usage_payload
from harness_labs.core.verification_images import attached_image_paths


_READ_ONLY_TOOLS = "Read,Glob,Grep"
_WORKSPACE_WRITE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"

# The claude CLI's own terminal classification for a worker that exhausted
# its structured-output retries (subtype "error_max_structured_output_retries",
# terminal_reason "structured_output_retry_exhausted"): the model kept
# producing text that failed schema validation until the CLI gave up on its
# side, five turns deep in real workspace edits. Claude-CLI-specific -- the
# Codex backend has no equivalent failure shape, so this stays local to this
# module rather than living alongside the shared Codex/Claude retry helpers
# in controller_live.py.
_STRUCTURED_OUTPUT_EXHAUSTION_TERMINAL_REASON = "structured_output_retry_exhausted"
_STRUCTURED_OUTPUT_EXHAUSTION_SUBTYPE = "error_max_structured_output_retries"


class StructuredOutputExhaustionError(LiveExecutionError):
    """Raised when the claude CLI reports it exhausted its structured-output retries.

    Carries the CLI's own ``terminal_reason``/``subtype``/``errors`` fields
    so the corrective addendum can quote the exact CLI-side failure without
    the caller re-parsing the raw transcript.
    """

    def __init__(
        self,
        message: str,
        *,
        terminal_reason: str,
        subtype: str,
        cli_errors: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.terminal_reason = terminal_reason
        self.subtype = subtype
        self.cli_errors = cli_errors


def _structured_output_exhaustion_detail(
    result_payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Classify a failed CLI result envelope as structured-output exhaustion.

    ``result_payload`` is the JSON envelope already parsed from the CLI's
    stdout by ``_parse_result_envelope`` -- the claude CLI still emits its
    full result envelope on stdout for this failure (nonzero exit status),
    so no second parse of the raw transcript is needed. Returns ``None`` for
    any other payload shape, leaving existing behavior unchanged.
    """

    if not isinstance(result_payload, Mapping):
        return None
    if (
        result_payload.get("terminal_reason")
        != _STRUCTURED_OUTPUT_EXHAUSTION_TERMINAL_REASON
        or result_payload.get("subtype") != _STRUCTURED_OUTPUT_EXHAUSTION_SUBTYPE
    ):
        return None
    errors = result_payload.get("errors", [])
    cli_errors = (
        tuple(str(item) for item in errors) if isinstance(errors, list) else ()
    )
    return {
        "terminal_reason": str(result_payload.get("terminal_reason", "")),
        "subtype": str(result_payload.get("subtype", "")),
        "cli_errors": cli_errors,
    }


def structured_output_exhaustion_retry_addendum(
    exc: StructuredOutputExhaustionError,
) -> str:
    """Corrective instructions appended to the prompt for an exhaustion retry.

    Names the CLI's own failure ("Failed to provide valid structured output
    after N attempts") and restates the required schema, since the previous
    session never got a schema-valid submission accepted at all -- unlike
    the floor and shape retries, there is no refused-but-parsed payload to
    quote back.
    """

    errors_text = "; ".join(exc.cli_errors) if exc.cli_errors else exc.subtype
    return (
        "\n\nCorrective addendum: your previous session failed to submit "
        f"valid structured output ({errors_text}). The schema is "
        f"{json.dumps(_RAW_OUTPUT_SCHEMA, sort_keys=True)}. Your workspace "
        "edits are intact -- resubmit only the structured result now, "
        "matching the schema exactly.\n"
    )


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
    # Per-execute() bookkeeping for CB3-04 restoration -- not a constructor
    # input; set from the attempt-start and post-worker workspace snapshots
    # already taken during ``_execute`` and consumed once by
    # ``_maybe_restore_attempt_start_baseline``.
    _attempt_start_baseline: Mapping[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _attempt_end_workspace: Mapping[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _attempt_sequence_token: int | None = field(
        default=None, init=False, repr=False, compare=False
    )

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
        self._attempt_start_baseline = None
        self._attempt_end_workspace = None
        self._attempt_sequence_token = None
        result: TaskResult | None = None
        try:
            result = self._execute(attempt)
            return result
        except DeliverableFloorViolation as exc:
            self._audit_deliverable_floor_violation(attempt, exc)
            result = TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "field": exc.field,
                    "reason": exc.reason,
                },
            )
            return result
        except (
            GitTransactionError,
            LiveExecutionError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            payload: dict[str, Any] = {
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            parked = getattr(exc, "park_disposition", None)
            if isinstance(parked, Mapping):
                payload["park_disposition"] = dict(parked)
            result = TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload=payload,
            )
            return result
        finally:
            # A ``finally`` (not a plain trailer after the ``except``
            # clauses) so restoration also runs for exception types outside
            # the caught tuple above -- the attempt still terminated without
            # succeeding, and any residue it left is still this attempt's
            # own to restore or decline.
            if result is None or result.status != "succeeded":
                self._maybe_restore_attempt_start_baseline(attempt)

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
        self._attempt_start_baseline = initial_workspace
        if self.sandbox == "workspace-write":
            self._attempt_sequence_token = _record_writable_attempt_started(
                repository
            )
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
        # The Claude CLI is driven here with a plain-text prompt on stdin, so
        # the images reach the model through its own file-reading tool (which
        # renders an image into context) rather than as inline content blocks;
        # switching stdin to the stream-json content-block protocol would
        # change the transport for every task, not just repair rounds.
        image_paths = attached_image_paths(context)

        retry_addendum = ""
        for dispatch_index in range(DELIVERABLE_FLOOR_RETRY_LIMIT + 1):
            try:
                return self._dispatch_and_validate(
                    attempt,
                    claude,
                    repository,
                    context,
                    initial_workspace,
                    adoption_grant,
                    artifact_kind,
                    preflight_artifact,
                    image_paths,
                    retry_addendum,
                )
            except DeliverableFloorViolation as exc:
                if dispatch_index >= DELIVERABLE_FLOOR_RETRY_LIMIT:
                    # ``execute()`` catches this and audits the terminal
                    # refusal itself -- auditing here too would double the
                    # ``deliverable_floor_refused`` event for the same
                    # violation.
                    raise
                self._audit_deliverable_floor_violation(attempt, exc)
                self._audit_deliverable_floor_retry(
                    attempt, exc, dispatch_index + 2
                )
                retry_addendum = deliverable_floor_retry_addendum(exc)
            except StructuredOutputExhaustionError as exc:
                # The claude CLI itself gave up on structured output after
                # real workspace edits -- unlike a shape refusal, there is
                # no outer handler in ``execute()`` specific to this class,
                # only the generic ``LiveExecutionError`` catch-all, so both
                # the retry and the terminal refusal are audited here.
                if dispatch_index >= DELIVERABLE_FLOOR_RETRY_LIMIT:
                    self._audit_structured_output_exhaustion(attempt, exc)
                    raise
                self._audit_structured_output_exhaustion(attempt, exc)
                self._audit_structured_output_exhaustion_retry(
                    attempt, exc, dispatch_index + 2
                )
                retry_addendum = structured_output_exhaustion_retry_addendum(exc)
            except SemanticResultError as exc:
                # Broader than ``DeliverableFloorViolation`` (caught above):
                # any other typed shape refusal from ``validate_semantic_result``,
                # e.g. an invalid ``addressed_finding_keys`` list. No outer
                # ``except SemanticResultError`` in ``execute()`` either, only
                # the generic ``ValueError`` catch-all, so the terminal
                # refusal is audited here too.
                if dispatch_index >= DELIVERABLE_FLOOR_RETRY_LIMIT:
                    self._audit_semantic_shape_violation(attempt, exc)
                    raise
                self._audit_semantic_shape_violation(attempt, exc)
                self._audit_semantic_shape_retry(attempt, exc, dispatch_index + 2)
                retry_addendum = semantic_shape_retry_addendum(exc)
        raise AssertionError(
            "unreachable: deliverable floor retry loop exhausted without "
            "returning or raising"
        )

    def _dispatch_and_validate(
        self,
        attempt: TaskAttempt,
        claude: str,
        repository: Path,
        context: Mapping[str, Any],
        initial_workspace: Mapping[str, Any],
        adoption_grant: Mapping[str, Any] | None,
        artifact_kind: str,
        preflight_artifact: Any,
        image_paths: Sequence[Path],
        retry_addendum: str,
    ) -> TaskResult:
        prompt = _worker_prompt(
            self.task,
            context,
            self.role_instructions,
            image_paths,
            images_attached=False,
            retry_addendum=retry_addendum,
        )

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
        # The captured images live under the audit run directory, outside the
        # worker's cwd. Claude Code's file-reading tool refuses paths outside
        # its allowed directories, and `-p` has no prompt to answer, so telling
        # the worker to open them is inert without this grant -- verified
        # against the installed CLI, which answers "CANNOT" without it. Granted
        # per-directory rather than leaning on --dangerously-skip-permissions,
        # so a read-only worker gets the pixels too and the access stays
        # narrowed to the artifacts the controller itself produced.
        for directory in sorted({str(path.parent) for path in image_paths}):
            argv.extend(("--add-dir", directory))
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
            exhaustion = _structured_output_exhaustion_detail(result_payload)
            if exhaustion is not None:
                raise StructuredOutputExhaustionError(
                    f"Claude exited with status {completed.returncode}: {detail}",
                    **exhaustion,
                )
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
        self._attempt_end_workspace = final_workspace
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
                    "writable worker completed without changing the repository",
                    park_disposition=extract_park_disposition(
                        raw, self.writable_paths
                    ),
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

    def _audit_deliverable_floor_retry(
        self,
        attempt: TaskAttempt,
        exc: DeliverableFloorViolation,
        attempt_number: int,
    ) -> None:
        """Journal that a floor violation is being retried, distinct from a refusal.

        Recorded once per in-dispatch retry, between the ``deliverable_floor_refused``
        event for the violation that triggered it and the re-dispatch itself, so the
        audit trail shows the retry was deliberate rather than a second unrelated
        refusal. ``attempt_number`` names which dispatch is about to run (2 for the
        first retry, and so on) so the trail stays legible if the bound ever grows.
        """

        if self.audit is None:
            return
        self.audit.append(
            "deliverable_floor_retry_dispatched",
            status="retrying",
            payload={
                "field": exc.field,
                "reason": exc.reason,
                "attempt": attempt_number,
            },
            actor=AuditActor(
                attempt.attempt_id,
                "capability_adapter",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="subprocess",
        )

    def _audit_semantic_shape_violation(
        self,
        attempt: TaskAttempt,
        exc: SemanticResultError,
    ) -> None:
        if self.audit is None:
            return
        self.audit.append(
            "semantic_shape_refused",
            status="failed",
            payload={"violation": type(exc).__name__, "message": str(exc)},
            actor=AuditActor(
                attempt.attempt_id,
                "capability_adapter",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="subprocess",
        )

    def _audit_semantic_shape_retry(
        self,
        attempt: TaskAttempt,
        exc: SemanticResultError,
        attempt_number: int,
    ) -> None:
        """Journal that a semantic-shape violation is being retried.

        Mirrors :meth:`_audit_deliverable_floor_retry` for the broader
        ``SemanticResultError`` family.
        """

        if self.audit is None:
            return
        self.audit.append(
            "semantic_shape_retry_dispatched",
            status="retrying",
            payload={
                "violation": type(exc).__name__,
                "message": str(exc),
                "attempt": attempt_number,
            },
            actor=AuditActor(
                attempt.attempt_id,
                "capability_adapter",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="subprocess",
        )

    def _audit_structured_output_exhaustion(
        self,
        attempt: TaskAttempt,
        exc: StructuredOutputExhaustionError,
    ) -> None:
        if self.audit is None:
            return
        self.audit.append(
            "structured_output_exhaustion_refused",
            status="failed",
            payload={
                "terminal_reason": exc.terminal_reason,
                "subtype": exc.subtype,
                "errors": list(exc.cli_errors),
            },
            actor=AuditActor(
                attempt.attempt_id,
                "capability_adapter",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="subprocess",
        )

    def _audit_structured_output_exhaustion_retry(
        self,
        attempt: TaskAttempt,
        exc: StructuredOutputExhaustionError,
        attempt_number: int,
    ) -> None:
        """Journal that a structured-output exhaustion is being retried.

        Mirrors :meth:`_audit_deliverable_floor_retry` for the CLI-level
        structured-output-exhaustion class.
        """

        if self.audit is None:
            return
        self.audit.append(
            "structured_output_exhaustion_retry_dispatched",
            status="retrying",
            payload={
                "terminal_reason": exc.terminal_reason,
                "subtype": exc.subtype,
                "errors": list(exc.cli_errors),
                "attempt": attempt_number,
            },
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

        Delegates to the shared :func:`verify_dirty_baseline_grant` -- the
        same receipt-resolution, changed-path-coverage, and per-file
        content-state check an issuer runs before journaling a grant as
        granted, so a supplied grant can never pass at issue time and fail
        here. No grant supplied refuses with the generic clean-baseline
        message; a supplied-but-failing grant refuses with a typed message
        naming the specific uncovered or content-mismatched paths.
        """

        grant = self.dirty_baseline_grant
        verification = verify_dirty_baseline_grant(
            evidence=self.evidence,
            grant=grant,
            dirty_paths=initial_workspace["changed_paths"],
            dirty_files=initial_workspace["files"],
        )
        if not verification.ok:
            if grant is None:
                raise LiveExecutionError(
                    "writable worker requires a clean repository baseline"
                )
            raise LiveExecutionError(
                "dirty-baseline grant refused: "
                + dirty_baseline_grant_refusal_detail(verification)
            )
        return {
            "receipt_ref": verification.receipt_ref,
            "receipted_paths": list(verification.receipted_paths),
        }

    def _maybe_restore_attempt_start_baseline(self, attempt: TaskAttempt) -> None:
        """Restore a failed writable attempt's own residue when nothing can adopt it.

        Triggers only when the attempt started from a clean baseline (this
        workspace's dirty state, if any, is entirely the attempt's own
        making) and no workspace-change receipt covers the current dirty
        state (a covering receipt means CB3-03's dispatch-chokepoint
        adoption can recover the work instead, so this never fires there --
        AC-CB304-1). A best-effort safety net: any anomaly while inspecting
        or mutating the workspace here is swallowed rather than raised,
        since restoration must never mask the attempt's own failure result
        or crash the caller.

        ``final`` -- the post-worker workspace snapshot -- may never have
        been taken if the attempt failed before reaching it (a timeout, a
        nonzero worker exit, a missing or unparsable result); in that case
        it is taken here on demand so restoration is still reachable.
        """

        baseline = self._attempt_start_baseline
        final = self._attempt_end_workspace
        token = self._attempt_sequence_token
        self._attempt_start_baseline = None
        self._attempt_end_workspace = None
        self._attempt_sequence_token = None
        if self.sandbox != "workspace-write" or baseline is None:
            return
        try:
            if final is None:
                final = workspace_snapshot(self.repository.resolve(strict=True))
            dirty_paths = list(final.get("changed_paths", ()))
            if not dirty_paths:
                return
            self._evaluate_and_apply_baseline_restoration(
                attempt, baseline, final, dirty_paths, token
            )
        except Exception:
            return

    def _evaluate_and_apply_baseline_restoration(
        self,
        attempt: TaskAttempt,
        baseline: Mapping[str, Any],
        final: Mapping[str, Any],
        dirty_paths: list[str],
        sequence_token: int | None,
    ) -> None:
        started_clean = not baseline.get("changed_paths")
        head_unchanged = final.get("head") == baseline.get("head")
        branch_unchanged = final.get("branch") == baseline.get("branch")
        repository = self.repository.resolve(strict=True)
        no_newer_attempt_started = sequence_token is not None and (
            _is_latest_writable_attempt(repository, sequence_token)
        )
        receipt_ref: str | None = None
        if started_clean:
            receipt_ref, _ = select_dirty_baseline_receipt(
                evidence=self.evidence,
                dirty_paths=dirty_paths,
                dirty_files=final.get("files", {}),
            )
        conditions = {
            "attempt_terminated_failed": True,
            "attempt_started_clean": started_clean,
            "head_unchanged": head_unchanged,
            "branch_unchanged": branch_unchanged,
            "no_newer_attempt_started": no_newer_attempt_started,
            "no_covering_receipt": receipt_ref is None,
        }
        baseline_commit = str(baseline["head"])
        if all(conditions.values()):
            actions = restore_attempt_start_baseline(
                repository, baseline_commit, dirty_paths
            )
            self._journal_baseline_restoration(
                attempt,
                status="restored",
                baseline_commit=baseline_commit,
                dirty_paths=dirty_paths,
                conditions=conditions,
                receipt_ref=receipt_ref,
                actions=actions,
            )
        else:
            self._journal_baseline_restoration(
                attempt,
                status="declined",
                baseline_commit=baseline_commit,
                dirty_paths=dirty_paths,
                conditions=conditions,
                receipt_ref=receipt_ref,
                actions=None,
            )

    def _journal_baseline_restoration(
        self,
        attempt: TaskAttempt,
        *,
        status: str,
        baseline_commit: str,
        dirty_paths: list[str],
        conditions: Mapping[str, bool],
        receipt_ref: str | None,
        actions: Mapping[str, str] | None,
    ) -> None:
        if self.audit is None:
            return
        payload: dict[str, Any] = {
            "baseline_commit": baseline_commit,
            "dirty_paths": dirty_paths,
            "conditions": dict(conditions),
            "receipt_ref": receipt_ref,
        }
        if actions is not None:
            payload["actions"] = dict(actions)
        self.audit.append(
            ATTEMPT_START_BASELINE_RESTORATION_EVENT,
            status=status,
            payload=payload,
            actor=AuditActor(
                attempt.attempt_id,
                "capability_adapter",
                parent_id=attempt.parent_attempt_id,
            ),
            attempt_id=attempt.attempt_id,
            parent_attempt_id=attempt.parent_attempt_id,
            backend_id="claude-print",
        )

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


__all__ = [
    "ClaudeSemanticTaskExecutor",
    "StructuredOutputExhaustionError",
    "structured_output_exhaustion_retry_addendum",
]
