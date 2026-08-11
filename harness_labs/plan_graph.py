"""A small sequential queue for dependent FeatureRuns.

PlanGraph deliberately owns scheduling only.  A caller supplies the approved
plan references, the FeatureRun launcher, and (optionally) the final test
runner; selecting or configuring a backend remains outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from .audit import AuditError
from .plan_graph_audit import PlanGraphAudit
from .plan_graph_contract import (
    PLAN_GRAPH_PROTOCOL,
    PlanGraphContractError,
    canonical_plan_graph_payload,
    path_is_allowed,
    plan_graph_identity,
    sha256_json,
)


class PlanGraphError(ValueError):
    """Raised when a proposed plan decomposition cannot be executed safely."""


@dataclass(frozen=True)
class RequiredPath:
    """One repository path needed by a verification command."""

    path: str
    availability: str = "base"
    producer_run_id: str | None = None


@dataclass(frozen=True)
class PathIntent:
    """One path a FeatureRun is approved to create or modify."""

    path: str
    action: str


@dataclass(frozen=True)
class FunctionalityCommand:
    """One final command with an explicit timeout and path dependencies."""

    argv: tuple[str, ...]
    timeout_seconds: float = 1200.0
    required_paths: tuple[RequiredPath, ...] = ()


@dataclass(frozen=True)
class PlanRun:
    """One FeatureRun proposed from an approved plan."""

    id: str
    objective: str
    plan_sections: tuple[str, ...]
    criteria: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    verification_argv: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    path_intents: tuple[PathIntent, ...] = ()
    verification_timeout_seconds: float = 1200.0
    verification_required_paths: tuple[RequiredPath, ...] = ()


@dataclass(frozen=True)
class PlanGraphPlan:
    """The approved plan references and its proposed sequential decomposition."""

    plan: str
    base_commit: str
    runs: tuple[PlanRun, ...]
    plan_sections: Mapping[str, str]
    acceptance_criteria: Mapping[str, str]
    functionality_tests: tuple[FunctionalityCommand | str, ...] = ()
    repository_id: str = "component-plan"
    protocol: str = "component-plan/0"
    plan_sha256: str | None = None
    canonical_decomposition: Mapping[str, object] | None = None


@dataclass(frozen=True)
class FeatureRunRequest:
    """The complete queue-owned input given to an injected FeatureRun launcher."""

    run: PlanRun
    base_commit: str
    plan: str
    plan_graph_id: str | None = None
    plan_node_id: str | None = None
    feature_run_id: str | None = None
    run_dir: Path | None = None


@dataclass(frozen=True)
class FeatureRunOutcome:
    """The only terminal information PlanGraph requires from FeatureRun."""

    status: str
    candidate_commit: str | None = None
    evidence: object | None = None
    plan_graph_id: str | None = None
    plan_node_id: str | None = None
    feature_run_id: str | None = None
    run_dir: str | None = None


@dataclass(frozen=True)
class PlanGraphResult:
    """Terminal queue result, including completed candidate commits."""

    status: str
    candidate_commit: str | None
    completed: Mapping[str, str]
    failed_run_id: str | None = None
    functionality_failure: str | None = None


@dataclass(frozen=True)
class ApprovalEvidence:
    """Revalidated approval identity supplied by the admission controller."""

    subject_sha256: str
    receipt_sha256: str
    plan_graph_digest: str
    audit_record: Mapping[str, object] | None = None


FeatureRunLauncher = Callable[[FeatureRunRequest], FeatureRunOutcome]
FunctionalityTestRunner = Callable[[FunctionalityCommand, str], None]
ApprovalValidator = Callable[[], ApprovalEvidence]


class SubprocessFeatureRunLauncher:
    """Invoke one backend-neutral FeatureRun command for each queued node.

    The controller writes one JSON request to stdin and requires one JSON
    :class:`FeatureRunOutcome` on stdout.  The child may use any implementation
    backend; PlanGraph neither selects nor interprets it.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if not argv or any(not value for value in argv):
            raise PlanGraphError("launcher command must contain non-empty arguments")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise PlanGraphError("launcher timeout must be positive or None")
        self.argv = tuple(argv)
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def __call__(self, request: FeatureRunRequest) -> FeatureRunOutcome:
        payload = {
            "run": {
                "id": request.run.id,
                "objective": request.run.objective,
                "plan_sections": list(request.run.plan_sections),
                "criteria": list(request.run.criteria),
                "depends_on": list(request.run.depends_on),
                "verification_argv": list(request.run.verification_argv),
                "verification_timeout_seconds": (
                    request.run.verification_timeout_seconds
                ),
                "verification_required_paths": [
                    _required_path_mapping(value)
                    for value in request.run.verification_required_paths
                ],
                "allowed_paths": list(request.run.allowed_paths),
                "path_intents": [
                    {"path": value.path, "action": value.action}
                    for value in request.run.path_intents
                ],
            },
            "base_commit": request.base_commit,
            "plan": request.plan,
            "plan_graph_id": request.plan_graph_id,
            "plan_node_id": request.plan_node_id,
            "feature_run_id": request.feature_run_id,
            "run_dir": str(request.run_dir) if request.run_dir is not None else None,
        }
        try:
            completed = subprocess.run(
                self.argv,
                input=json.dumps(payload, sort_keys=True) + "\n",
                cwd=self.cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return FeatureRunOutcome(
                "failed",
                evidence={"error": str(exc), "error_type": type(exc).__name__},
            )
        if completed.returncode:
            return FeatureRunOutcome(
                "failed",
                evidence={
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )
        try:
            result = json.loads(completed.stdout)
            if not isinstance(result, dict):
                raise TypeError("result must be an object")
            status = result["status"]
            candidate_commit = result.get("candidate_commit")
            if status not in {"succeeded", "failed", "blocked"}:
                raise ValueError(f"invalid status {status!r}")
            if candidate_commit is not None and not isinstance(candidate_commit, str):
                raise TypeError("candidate_commit must be a string or null")
            for name in ("plan_graph_id", "plan_node_id", "feature_run_id", "run_dir"):
                value = result.get(name)
                if value is not None and not isinstance(value, str):
                    raise TypeError(f"{name} must be a string or null")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return FeatureRunOutcome(
                "failed",
                evidence={
                    "error": f"invalid launcher result: {exc}",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )
        return FeatureRunOutcome(
            status,
            candidate_commit,
            result.get("evidence"),
            result.get("plan_graph_id"),
            result.get("plan_node_id"),
            result.get("feature_run_id"),
            result.get("run_dir"),
        )


class PlanGraph:
    """Execute an already approved FeatureRun decomposition in dependency order."""

    def __init__(
        self,
        plan: PlanGraphPlan,
        launcher: FeatureRunLauncher,
        *,
        run_root: Path,
        graph_run_id: str | None = None,
        functionality_tests: Sequence[FunctionalityCommand | Sequence[str] | str] = (),
        functionality_test_runner: FunctionalityTestRunner | None = None,
        approval_validator: ApprovalValidator | None = None,
        repository_root: Path | None = None,
    ) -> None:
        self.plan = plan
        self.launcher = launcher
        if run_root is None:
            raise PlanGraphError("run_root is required for audited PlanGraph execution")
        self.run_root = run_root
        self.graph_run_id = graph_run_id or f"plan-graph-{uuid4().hex}"
        self._audit: PlanGraphAudit | None = None
        self.repository_root = (repository_root or Path.cwd()).resolve()
        if plan.protocol == PLAN_GRAPH_PROTOCOL and functionality_tests:
            raise PlanGraphError(
                "approved PlanGraph forbids functionality-test overrides"
            )
        self.functionality_tests = tuple(
            _normalize_functionality_command(value)
            for value in tuple(plan.functionality_tests) + tuple(functionality_tests)
        )
        self.functionality_test_runner = (
            functionality_test_runner
            or (
                lambda command, commit: _run_functionality_test(
                    command, commit, self.repository_root
                )
            )
        )
        self.approval_validator = approval_validator
        if plan.protocol == PLAN_GRAPH_PROTOCOL and approval_validator is None:
            raise PlanGraphError("approved PlanGraph requires an approval receipt")

    def validate(self) -> None:
        """Reject invalid references and cycles before a launcher can run."""

        if not self.plan.plan:
            raise PlanGraphError("plan path must not be empty")
        if not self.plan.base_commit:
            raise PlanGraphError("base_commit must not be empty")
        seen: set[str] = set()
        known_runs = {run.id for run in self.plan.runs}
        covered: set[str] = set()
        strict = self.plan.protocol == PLAN_GRAPH_PROTOCOL
        for run in self.plan.runs:
            if not run.id or run.id in seen:
                raise PlanGraphError(f"duplicate or empty run id: {run.id!r}")
            seen.add(run.id)
            if not run.objective.strip():
                raise PlanGraphError(f"run {run.id!r} has an empty objective")
            if not run.plan_sections:
                raise PlanGraphError(f"run {run.id!r} cites no plan sections")
            if any(not value for value in run.verification_argv):
                raise PlanGraphError(
                    f"run {run.id!r} verification_argv contains an empty value"
                )
            if strict and not run.verification_argv:
                raise PlanGraphError(
                    f"run {run.id!r} requires controller-owned verification"
                )
            if run.verification_timeout_seconds <= 0:
                raise PlanGraphError(
                    f"run {run.id!r} verification timeout must be positive"
                )
            if strict and not run.allowed_paths:
                raise PlanGraphError(f"run {run.id!r} has no allowed_paths")
            for intent in run.path_intents:
                if intent.action not in {"create", "modify"}:
                    raise PlanGraphError(
                        f"run {run.id!r} has invalid path intent {intent.action!r}"
                    )
                if not path_is_allowed(intent.path, run.allowed_paths):
                    raise PlanGraphError(
                        f"run {run.id!r} path intent {intent.path!r} is outside allowed_paths"
                    )
            cited_sections = "\n".join(
                self.plan.plan_sections[section]
                for section in run.plan_sections
                if section in self.plan.plan_sections
            )
            for section in run.plan_sections:
                if section not in self.plan.plan_sections:
                    raise PlanGraphError(
                        f"run {run.id!r} references unknown plan section {section!r}"
                    )
            if run.objective not in cited_sections:
                raise PlanGraphError(
                    f"run {run.id!r} objective is absent from its cited plan sections"
                )
            for criterion in run.criteria:
                if criterion not in self.plan.acceptance_criteria:
                    raise PlanGraphError(
                        f"run {run.id!r} references unknown criterion {criterion!r}"
                    )
                if (
                    criterion not in cited_sections
                    or self.plan.acceptance_criteria[criterion] not in cited_sections
                ):
                    raise PlanGraphError(
                        f"run {run.id!r} criterion {criterion!r} is absent from "
                        "its cited plan sections"
                    )
                covered.add(criterion)
            for dependency in run.depends_on:
                if dependency not in known_runs:
                    raise PlanGraphError(
                        f"run {run.id!r} depends on missing run {dependency!r}"
                    )
                if dependency == run.id:
                    raise PlanGraphError(f"run {run.id!r} depends on itself")
        missing = set(self.plan.acceptance_criteria) - covered
        if missing:
            raise PlanGraphError(
                "acceptance criteria are not assigned to a FeatureRun: "
                + ", ".join(sorted(missing))
            )
        for command in self.functionality_tests:
            if not command.argv or any(not value for value in command.argv):
                raise PlanGraphError("functionality_tests contains an empty command")
            if command.timeout_seconds <= 0:
                raise PlanGraphError("functionality test timeout must be positive")
        self._ordered_runs()
        self._validate_command_dependencies()

    def run(self) -> PlanGraphResult:
        """Validate, resume completed nodes, then launch runs sequentially."""

        approval = self._revalidate_approval()
        self.validate()
        audit = self._audit_for_run(approval)
        if audit.terminal:
            return self._result_from_audit(audit)
        completed = self._load_audit_completed(audit)
        ordered_runs = self._ordered_runs()
        self._validate_completed_dependencies(ordered_runs, completed)
        candidate_commit = self.plan.base_commit
        admission_rechecked = False
        for run in ordered_runs:
            if run.id in completed:
                candidate_commit = completed[run.id]
                continue
            if not admission_rechecked:
                self._revalidate_approval()
                admission_rechecked = True
            request = self._request_for_run(run, candidate_commit)
            audit.node_started(run.id)
            outcome = self.launcher(request)
            if not self._outcome_matches_reservation(outcome, request):
                outcome = FeatureRunOutcome(
                    "failed",
                    evidence={
                        "error": "launcher result does not match reserved child identity"
                    },
                )
            if outcome.status != "succeeded":
                result = PlanGraphResult(
                    status=outcome.status if outcome.status == "blocked" else "failed",
                    candidate_commit=None,
                    completed=dict(completed),
                    failed_run_id=run.id,
                )
                audit.node_failed(run.id, result.status, outcome.evidence)
                audit.finalize(result.status, self._result_payload(result))
                return result
            if not outcome.candidate_commit:
                raise PlanGraphError(
                    f"successful FeatureRun {run.id!r} did not provide a candidate commit"
                )
            completed[run.id] = outcome.candidate_commit
            candidate_commit = outcome.candidate_commit
            audit.node_completed(run.id, candidate_commit)

        for command in self.functionality_tests:
            try:
                self.functionality_test_runner(command, candidate_commit)
            except Exception as exc:  # final test failures are terminal evidence
                rendered = json.dumps(list(command.argv))
                result = PlanGraphResult(
                    status="failed",
                    candidate_commit=candidate_commit,
                    completed=dict(completed),
                    functionality_failure=f"{rendered}: {exc}",
                )
                audit.functionality_failed(
                    _command_mapping(command), candidate_commit, str(exc)
                )
                audit.finalize("failed", self._result_payload(result))
                return result
            audit.functionality_completed(
                _command_mapping(command), candidate_commit
            )
        result = PlanGraphResult("succeeded", candidate_commit, dict(completed))
        audit.finalize("succeeded", self._result_payload(result))
        return result

    def _revalidate_approval(self) -> ApprovalEvidence | None:
        if self.approval_validator is None:
            return None
        try:
            evidence = self.approval_validator()
        except Exception as exc:
            raise PlanGraphError(f"PlanGraph approval validation failed: {exc}") from exc
        if not isinstance(evidence, ApprovalEvidence):
            raise PlanGraphError("approval validator returned invalid evidence")
        expected = self._identity_digest()
        if evidence.plan_graph_digest != expected:
            raise PlanGraphError("approval does not match the supplied PlanGraph")
        return evidence

    def _plan_sha256(self) -> str:
        if self.plan.plan_sha256 is not None:
            return self.plan.plan_sha256
        try:
            return hashlib.sha256(Path(self.plan.plan).read_bytes()).hexdigest()
        except OSError as exc:
            raise PlanGraphError(f"could not read approved plan: {exc}") from exc

    def _identity_digest(self) -> str:
        if (
            self.plan.protocol == PLAN_GRAPH_PROTOCOL
            and self.plan.canonical_decomposition is not None
        ):
            try:
                return plan_graph_identity(
                    repository_id=self.plan.repository_id,
                    base_commit=self.plan.base_commit,
                    plan_sha256=self._plan_sha256(),
                    decomposition=self.plan.canonical_decomposition,
                )
            except PlanGraphContractError as exc:
                raise PlanGraphError(f"invalid canonical PlanGraph: {exc}") from exc
        return sha256_json(
            {
                "protocol": "component-plan-identity/0",
                "repository_id": self.plan.repository_id,
                "base_commit": self.plan.base_commit,
                "plan_sha256": self._plan_sha256(),
                "runs": [_run_mapping(run) for run in self.plan.runs],
                "functionality_tests": [
                    _command_mapping(command) for command in self.functionality_tests
                ],
            }
        )

    def _audit_for_run(self, approval: ApprovalEvidence | None) -> PlanGraphAudit:
        if self._audit is None:
            assert self.graph_run_id is not None
            nodes = {
                run.id: {
                    "status": "queued",
                    "objective": run.objective,
                    "plan_sections": list(run.plan_sections),
                    "depends_on": list(run.depends_on),
                    "criteria": list(run.criteria),
                    "verification_argv": list(run.verification_argv),
                    "verification_timeout_seconds": run.verification_timeout_seconds,
                    "allowed_paths": list(run.allowed_paths),
                    "path_intents": [
                        {"path": value.path, "action": value.action}
                        for value in run.path_intents
                    ],
                    "feature_run_id": self._feature_run_id(run.id),
                    "run_dir": str(
                        (self.run_root / self._feature_run_id(run.id)).resolve()
                    ),
                    "started_at": None,
                    "finished_at": None,
                    "candidate_commit": None,
                }
                for run in self._ordered_runs()
            }
            try:
                self._audit = PlanGraphAudit(
                    run_root=self.run_root,
                    graph_run_id=self.graph_run_id,
                    plan=self.plan.plan,
                    plan_sha256=self._plan_sha256(),
                    base_commit=self.plan.base_commit,
                    repository_id=self.plan.repository_id,
                    repository_path=self.repository_root,
                    plan_graph_digest=self._identity_digest(),
                    approval=(
                        dict(approval.audit_record)
                        if approval is not None and approval.audit_record is not None
                        else (
                            {
                                "subject_sha256": approval.subject_sha256,
                                "receipt_sha256": approval.receipt_sha256,
                            }
                            if approval is not None
                            else None
                        )
                    ),
                    objective="; ".join(run.objective for run in self.plan.runs),
                    nodes=nodes,
                    functionality_tests=tuple(
                        _command_mapping(command)
                        for command in self.functionality_tests
                    ),
                )
            except (AuditError, OSError, ValueError) as exc:
                raise PlanGraphError(f"could not open PlanGraph audit: {exc}") from exc
        return self._audit

    @staticmethod
    def _load_audit_completed(audit: PlanGraphAudit) -> dict[str, str]:
        nodes = audit.state.get("nodes")
        if not isinstance(nodes, dict):
            raise PlanGraphError(
                "invalid PlanGraph audit checkpoint: nodes must be an object"
            )
        completed: dict[str, str] = {}
        for node_id, node in nodes.items():
            if not isinstance(node_id, str) or not isinstance(node, dict):
                raise PlanGraphError("invalid PlanGraph audit checkpoint node")
            if node.get("status") == "succeeded":
                candidate = node.get("candidate_commit")
                if not isinstance(candidate, str):
                    raise PlanGraphError(
                        "successful PlanGraph audit node has no candidate commit"
                    )
                completed[node_id] = candidate
        return completed

    def _feature_run_id(self, node_id: str) -> str:
        assert self.graph_run_id is not None
        return f"{self.graph_run_id}-{node_id}"

    def _request_for_run(self, run: PlanRun, candidate_commit: str) -> FeatureRunRequest:
        if self._audit is None:
            return FeatureRunRequest(
                run=run, base_commit=candidate_commit, plan=self.plan.plan
            )
        feature_run_id = self._feature_run_id(run.id)
        return FeatureRunRequest(
            run=run,
            base_commit=candidate_commit,
            plan=self.plan.plan,
            plan_graph_id=self.graph_run_id,
            plan_node_id=run.id,
            feature_run_id=feature_run_id,
            run_dir=(self.run_root / feature_run_id).resolve(),
        )

    @staticmethod
    def _outcome_matches_reservation(
        outcome: FeatureRunOutcome, request: FeatureRunRequest
    ) -> bool:
        if outcome.status != "succeeded":
            return True
        return (
            outcome.plan_graph_id == request.plan_graph_id
            and outcome.plan_node_id == request.plan_node_id
            and outcome.feature_run_id == request.feature_run_id
            and outcome.run_dir == str(request.run_dir)
        )

    @staticmethod
    def _result_payload(result: PlanGraphResult) -> dict[str, object]:
        return {
            "status": result.status,
            "candidate_commit": result.candidate_commit,
            "completed": dict(result.completed),
            "failed_run_id": result.failed_run_id,
            "functionality_failure": result.functionality_failure,
        }

    @staticmethod
    def _result_from_audit(audit: PlanGraphAudit) -> PlanGraphResult:
        state = audit.state
        nodes = state.get("nodes", {})
        completed = {
            node_id: node["candidate_commit"]
            for node_id, node in nodes.items()
            if isinstance(node, dict)
            and node.get("status") == "succeeded"
            and isinstance(node.get("candidate_commit"), str)
        }
        failed = next(
            (
                node_id
                for node_id, node in nodes.items()
                if isinstance(node, dict)
                and node.get("status") in {"failed", "blocked"}
            ),
            None,
        )
        functionality = state.get("functionality_test", {})
        return PlanGraphResult(
            status=str(state.get("terminal_graph_status")),
            candidate_commit=(
                state.get("current_candidate_commit")
                if isinstance(state.get("current_candidate_commit"), str)
                else None
            ),
            completed=completed,
            failed_run_id=failed,
            functionality_failure=(
                functionality.get("error")
                if isinstance(functionality, dict)
                and isinstance(functionality.get("error"), str)
                else None
            ),
        )

    def _ordered_runs(self) -> tuple[PlanRun, ...]:
        by_id = {run.id: run for run in self.plan.runs}
        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[PlanRun] = []

        def visit(run_id: str) -> None:
            if run_id in visiting:
                raise PlanGraphError(f"cycle detected at run {run_id!r}")
            if run_id in visited:
                return
            visiting.add(run_id)
            for dependency in by_id[run_id].depends_on:
                visit(dependency)
            visiting.remove(run_id)
            visited.add(run_id)
            ordered.append(by_id[run_id])

        for run in self.plan.runs:
            visit(run.id)
        return tuple(ordered)

    def _validate_command_dependencies(self) -> None:
        by_id = {run.id: run for run in self.plan.runs}

        def ancestors(run_id: str) -> set[str]:
            result: set[str] = set()
            pending = list(by_id[run_id].depends_on)
            while pending:
                dependency = pending.pop()
                if dependency in result:
                    continue
                result.add(dependency)
                pending.extend(by_id[dependency].depends_on)
            return result

        def validate_required(required: RequiredPath, consumer: str | None) -> None:
            if required.availability == "base":
                if required.producer_run_id is not None:
                    raise PlanGraphError(
                        f"base path {required.path!r} names a producer"
                    )
                return
            if required.availability != "created_by" or not required.producer_run_id:
                raise PlanGraphError(
                    f"required path {required.path!r} has invalid availability"
                )
            producer = by_id.get(required.producer_run_id)
            if producer is None:
                raise PlanGraphError(
                    f"required path {required.path!r} has missing producer "
                    f"{required.producer_run_id!r}"
                )
            if not any(
                intent.path == required.path and intent.action == "create"
                for intent in producer.path_intents
            ):
                raise PlanGraphError(
                    f"run {producer.id!r} does not declare creation of "
                    f"{required.path!r}"
                )
            if consumer is not None and producer.id not in ancestors(consumer):
                raise PlanGraphError(
                    f"run {consumer!r} consumes {required.path!r} before producer "
                    f"{producer.id!r}"
                )

        for run in self.plan.runs:
            for required in run.verification_required_paths:
                validate_required(required, run.id)
        for command in self.functionality_tests:
            for required in command.required_paths:
                validate_required(required, None)

    @staticmethod
    def _validate_completed_dependencies(
        ordered_runs: Sequence[PlanRun], completed: Mapping[str, str]
    ) -> None:
        seen_incomplete = False
        for run in ordered_runs:
            if run.id not in completed:
                seen_incomplete = True
                continue
            if seen_incomplete:
                raise PlanGraphError(
                    "PlanGraph state does not preserve the sequential candidate lineage"
                )
            if run.id in completed and any(
                dependency not in completed for dependency in run.depends_on
            ):
                raise PlanGraphError(
                    f"PlanGraph state marks {run.id!r} complete before its dependency"
                )

def _run_functionality_test(
    command: FunctionalityCommand | str,
    candidate_commit: str,
    repository_root: Path | None = None,
) -> None:
    command = _normalize_functionality_command(command)
    repository_root = (repository_root or Path.cwd()).resolve()
    with tempfile.TemporaryDirectory(prefix="plan-graph-") as temporary:
        candidate_path = Path(temporary) / "candidate"
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--shared",
                "--no-checkout",
                str(repository_root),
                str(candidate_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if clone.returncode:
            output = (clone.stdout + clone.stderr).strip()
            raise RuntimeError(output or "could not prepare candidate checkout")
        checkout = subprocess.run(
            ["git", "-C", str(candidate_path), "checkout", "--detach", candidate_commit],
            text=True,
            capture_output=True,
            check=False,
        )
        if checkout.returncode:
            output = (checkout.stdout + checkout.stderr).strip()
            raise RuntimeError(output or f"could not check out {candidate_commit}")
        try:
            completed = subprocess.run(
                command.argv,
                cwd=candidate_path,
                text=True,
                capture_output=True,
                check=False,
                timeout=command.timeout_seconds,
            )
            if completed.returncode:
                output = (completed.stdout + completed.stderr).strip()
                raise RuntimeError(output or f"exit status {completed.returncode}")
        except OSError as exc:
            raise RuntimeError(f"could not prepare {candidate_commit} for testing: {exc}") from exc


def plan_from_mapping(
    payload: Mapping[str, object],
    *,
    base_commit: str | None = None,
    repository_id: str | None = None,
    plan_sha256: str | None = None,
) -> PlanGraphPlan:
    """Build a PlanGraphPlan from the CLI's explicit decomposition payload."""

    if payload.get("protocol") == PLAN_GRAPH_PROTOCOL:
        if base_commit is None or repository_id is None or plan_sha256 is None:
            raise PlanGraphError(
                "canonical PlanGraph requires approval-bound repository metadata"
            )
        try:
            canonical = canonical_plan_graph_payload(payload)
        except PlanGraphContractError as exc:
            raise PlanGraphError(f"invalid PlanGraph decomposition: {exc}") from exc
        runs = tuple(_run_from_mapping(item) for item in canonical["runs"])
        return PlanGraphPlan(
            plan=str(canonical["plan"]),
            base_commit=base_commit,
            runs=runs,
            plan_sections=dict(canonical["plan_sections"]),
            acceptance_criteria=dict(canonical["acceptance_criteria"]),
            functionality_tests=tuple(
                _command_from_mapping(value)
                for value in canonical["functionality_tests"]
            ),
            repository_id=repository_id,
            protocol=PLAN_GRAPH_PROTOCOL,
            plan_sha256=plan_sha256,
            canonical_decomposition=canonical,
        )
    try:
        runs = tuple(
            PlanRun(
                id=str(item["id"]),
                objective=str(item["objective"]),
                plan_sections=tuple(str(value) for value in item["plan_sections"]),
                criteria=tuple(str(value) for value in item["criteria"]),
                depends_on=tuple(str(value) for value in item.get("depends_on", ())),
                verification_argv=tuple(
                    str(value) for value in item.get("verification_argv", ())
                ),
            )
            for item in payload["runs"]  # type: ignore[index]
        )
        sections = {str(key): str(value) for key, value in payload["plan_sections"].items()}  # type: ignore[index,union-attr]
        criteria = {str(key): str(value) for key, value in payload["acceptance_criteria"].items()}  # type: ignore[index,union-attr]
        return PlanGraphPlan(
            plan=str(payload["plan"]),
            base_commit=str(payload["base_commit"]),
            runs=runs,
            plan_sections=sections,
            acceptance_criteria=criteria,
            functionality_tests=tuple(
                str(value) for value in payload.get("functionality_tests", ())
            ),
        )
    except (KeyError, TypeError) as exc:
        raise PlanGraphError(f"invalid PlanGraph decomposition: {exc}") from exc


def _run_from_mapping(value: Mapping[str, object]) -> PlanRun:
    return PlanRun(
        id=str(value["id"]),
        objective=str(value["objective"]),
        plan_sections=tuple(str(item) for item in value["plan_sections"]),  # type: ignore[arg-type]
        criteria=tuple(str(item) for item in value["criteria"]),  # type: ignore[arg-type]
        depends_on=tuple(str(item) for item in value["depends_on"]),  # type: ignore[arg-type]
        verification_argv=tuple(str(item) for item in value["verification_argv"]),  # type: ignore[arg-type]
        allowed_paths=tuple(str(item) for item in value["allowed_paths"]),  # type: ignore[arg-type]
        path_intents=tuple(
            PathIntent(str(item["path"]), str(item["action"]))
            for item in value["path_intents"]  # type: ignore[union-attr]
        ),
        verification_timeout_seconds=float(value["verification_timeout_seconds"]),
        verification_required_paths=tuple(
            _required_path_from_mapping(item)
            for item in value["verification_required_paths"]  # type: ignore[union-attr]
        ),
    )


def _command_from_mapping(value: Mapping[str, object]) -> FunctionalityCommand:
    return FunctionalityCommand(
        argv=tuple(str(item) for item in value["argv"]),  # type: ignore[arg-type]
        timeout_seconds=float(value["timeout_seconds"]),
        required_paths=tuple(
            _required_path_from_mapping(item)
            for item in value["required_paths"]  # type: ignore[union-attr]
        ),
    )


def _required_path_from_mapping(value: Mapping[str, object]) -> RequiredPath:
    producer = value.get("producer_run_id")
    return RequiredPath(
        path=str(value["path"]),
        availability=str(value["availability"]),
        producer_run_id=str(producer) if producer is not None else None,
    )


def _normalize_functionality_command(
    value: FunctionalityCommand | Sequence[str] | str,
) -> FunctionalityCommand:
    if isinstance(value, FunctionalityCommand):
        return value
    if isinstance(value, str):
        return FunctionalityCommand(("sh", "-c", value))
    return FunctionalityCommand(tuple(str(item) for item in value))


def _required_path_mapping(value: RequiredPath) -> dict[str, object]:
    result: dict[str, object] = {
        "path": value.path,
        "availability": value.availability,
    }
    if value.producer_run_id is not None:
        result["producer_run_id"] = value.producer_run_id
    return result


def _command_mapping(value: FunctionalityCommand) -> dict[str, object]:
    return {
        "argv": list(value.argv),
        "timeout_seconds": value.timeout_seconds,
        "required_paths": [
            _required_path_mapping(item) for item in value.required_paths
        ],
    }


def _run_mapping(value: PlanRun) -> dict[str, object]:
    return {
        "id": value.id,
        "objective": value.objective,
        "plan_sections": list(value.plan_sections),
        "criteria": list(value.criteria),
        "depends_on": list(value.depends_on),
        "verification_argv": list(value.verification_argv),
        "verification_timeout_seconds": value.verification_timeout_seconds,
        "verification_required_paths": [
            _required_path_mapping(item)
            for item in value.verification_required_paths
        ],
        "allowed_paths": list(value.allowed_paths),
        "path_intents": [
            {"path": item.path, "action": item.action}
            for item in value.path_intents
        ],
    }


__all__ = [
    "ApprovalEvidence",
    "FeatureRunOutcome",
    "FeatureRunRequest",
    "FunctionalityCommand",
    "PathIntent",
    "PlanGraph",
    "PlanGraphError",
    "PlanGraphPlan",
    "PlanGraphResult",
    "PlanRun",
    "RequiredPath",
    "SubprocessFeatureRunLauncher",
    "plan_from_mapping",
]
