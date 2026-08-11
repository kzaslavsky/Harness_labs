"""Deterministic registration and sequential execution of approved PlanGraphs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from .audit import AuditError
from .plan_graph_audit import PlanGraphAudit, validate_plan_graph_id


REGISTRATION_PROTOCOL = "plan-graph-registration/1"
FEATURE_RUN_REQUEST_PROTOCOL = "plan-graph-feature-run-request/1"
_REGISTRATION_FIELDS = frozenset(
    {
        "protocol",
        "logical_graph_id",
        "plan_path",
        "plan_sha256",
        "base_commit",
        "graph_digest",
        "definition_json",
    }
)
_NODE_DEFINITION_FIELDS = (
    "objective",
    "plan_sections",
    "criteria",
    "depends_on",
    "verification_argv",
)


class PlanGraphError(ValueError):
    """Raised when a PlanGraph cannot be registered or executed safely."""


@dataclass(frozen=True)
class PlanRun:
    id: str
    objective: str
    plan_sections: tuple[str, ...]
    criteria: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    verification_argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanGraphPlan:
    plan: str
    base_commit: str
    runs: tuple[PlanRun, ...]
    plan_sections: Mapping[str, str]
    acceptance_criteria: Mapping[str, str]
    functionality_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanGraphRegistration:
    protocol: str
    logical_graph_id: str
    plan_path: str
    plan_sha256: str
    base_commit: str
    graph_digest: str
    definition_json: str


@dataclass(frozen=True)
class FeatureRunRequest:
    protocol: str
    run: PlanRun
    base_commit: str
    plan: str
    plan_base_commit: str
    plan_sha256: str
    plan_graph_id: str
    plan_node_id: str
    feature_run_id: str
    run_dir: Path

    def __post_init__(self) -> None:
        if self.protocol != FEATURE_RUN_REQUEST_PROTOCOL:
            raise ValueError("unsupported PlanGraph FeatureRun request protocol")


@dataclass(frozen=True)
class FeatureRunOutcome:
    status: str
    candidate_commit: str | None = None
    evidence: object | None = None
    plan_graph_id: str | None = None
    plan_node_id: str | None = None
    feature_run_id: str | None = None
    run_dir: str | None = None


@dataclass(frozen=True)
class PlanGraphResult:
    status: str
    candidate_commit: str | None
    completed: Mapping[str, str]
    failed_run_id: str | None = None
    functionality_failure: str | None = None


FeatureRunLauncher = Callable[[FeatureRunRequest], FeatureRunOutcome]
FunctionalityTestRunner = Callable[[str, str], None]


class SubprocessFeatureRunLauncher:
    """Invoke one backend-neutral FeatureRun command for each queued node."""

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
            "protocol": request.protocol,
            "run": {
                "id": request.run.id,
                "objective": request.run.objective,
                "plan_sections": list(request.run.plan_sections),
                "criteria": list(request.run.criteria),
                "depends_on": list(request.run.depends_on),
                "verification_argv": list(request.run.verification_argv),
            },
            "base_commit": request.base_commit,
            "plan": request.plan,
            "plan_base_commit": request.plan_base_commit,
            "plan_sha256": request.plan_sha256,
            "plan_graph_id": request.plan_graph_id,
            "plan_node_id": request.plan_node_id,
            "feature_run_id": request.feature_run_id,
            "run_dir": str(request.run_dir),
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
                "failed", evidence={"error": str(exc), "error_type": type(exc).__name__}
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


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_definition(plan: PlanGraphPlan) -> dict[str, object]:
    return {
        "runs": [
            {
                "id": run.id,
                "objective": run.objective,
                "plan_sections": list(run.plan_sections),
                "criteria": list(run.criteria),
                "depends_on": list(run.depends_on),
                "verification_argv": list(run.verification_argv),
            }
            for run in plan.runs
        ],
        "plan_sections": dict(plan.plan_sections),
        "acceptance_criteria": dict(plan.acceptance_criteria),
        "functionality_tests": list(plan.functionality_tests),
    }


def _git(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        error = completed.stderr.decode("utf-8", "replace").strip()
        raise PlanGraphError(error or f"git {' '.join(arguments)} failed")
    return completed.stdout


def _verify_commit(repository: Path, commit: str) -> str:
    if not isinstance(commit, str) or not commit.strip():
        raise PlanGraphError("base_commit must be non-empty")
    return _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()


def _normalize_plan_path(repository: Path, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanGraphError("plan path must be non-empty")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise PlanGraphError("plan path must be normalized and repository-relative")
    normalized = candidate.as_posix()
    if normalized != value or normalized in {"", "."}:
        raise PlanGraphError("plan path must be normalized and repository-relative")
    root = repository.resolve()
    working_path = root / normalized
    try:
        resolved = working_path.resolve(strict=False)
    except OSError as exc:
        raise PlanGraphError(f"could not resolve plan path: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise PlanGraphError("plan path escapes the repository")
    return normalized


def _plan_bytes(repository: Path, base_commit: str, plan_path: str) -> bytes:
    return _git(repository, "show", f"{base_commit}:{plan_path}")


def _digest_input(
    registration_fields: Mapping[str, object], definition: Mapping[str, object]
) -> dict[str, object]:
    return {
        "protocol": registration_fields["protocol"],
        "logical_graph_id": registration_fields["logical_graph_id"],
        "plan_path": registration_fields["plan_path"],
        "plan_sha256": registration_fields["plan_sha256"],
        "base_commit": registration_fields["base_commit"],
        "definition": dict(definition),
    }


def register_plan_graph(
    *,
    repository: Path,
    logical_graph_id: str,
    decomposition: Mapping[str, object],
) -> PlanGraphRegistration:
    repository = repository.resolve()
    validate_plan_graph_id(logical_graph_id)
    plan = plan_from_mapping(decomposition)
    validate_plan_graph_plan(plan)
    base_commit = _verify_commit(repository, plan.base_commit)
    plan_path = _normalize_plan_path(repository, plan.plan)
    plan_sha256 = hashlib.sha256(_plan_bytes(repository, base_commit, plan_path)).hexdigest()
    definition_json = canonical_json(canonical_definition(plan))
    fields: dict[str, object] = {
        "protocol": REGISTRATION_PROTOCOL,
        "logical_graph_id": logical_graph_id,
        "plan_path": plan_path,
        "plan_sha256": plan_sha256,
        "base_commit": base_commit,
    }
    graph_digest = hashlib.sha256(
        canonical_json(_digest_input(fields, json.loads(definition_json))).encode("utf-8")
    ).hexdigest()
    return PlanGraphRegistration(
        protocol=REGISTRATION_PROTOCOL,
        logical_graph_id=logical_graph_id,
        plan_path=plan_path,
        plan_sha256=plan_sha256,
        base_commit=base_commit,
        graph_digest=graph_digest,
        definition_json=definition_json,
    )


def registration_bytes(registration: PlanGraphRegistration) -> bytes:
    return (canonical_json(asdict(registration)) + "\n").encode("utf-8")


def registration_from_mapping(payload: Mapping[str, object]) -> PlanGraphRegistration:
    if set(payload) != _REGISTRATION_FIELDS:
        raise PlanGraphError("registration must contain exactly the protocol fields")
    if not all(isinstance(payload.get(name), str) for name in _REGISTRATION_FIELDS):
        raise PlanGraphError("registration fields must be strings")
    return PlanGraphRegistration(**{name: payload[name] for name in _REGISTRATION_FIELDS})  # type: ignore[arg-type]


def load_registration(path: Path) -> PlanGraphRegistration:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanGraphError(f"could not load PlanGraph registration: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanGraphError("registration must be a JSON object")
    registration = registration_from_mapping(payload)
    if raw != registration_bytes(registration):
        raise PlanGraphError("registration file is not canonical JSON")
    return registration


def persist_registration(
    *, repository: Path, registration_root: Path, registration: PlanGraphRegistration
) -> Path:
    verify_registration(repository, registration)
    registration_root.mkdir(parents=True, exist_ok=True)
    final_path = registration_root / f"{registration.logical_graph_id}.json"
    content = registration_bytes(registration)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.", dir=registration_root
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary_path, final_path)
        except FileExistsError:
            existing = load_registration(final_path)
            verify_registration(repository, existing)
            if existing.graph_digest != registration.graph_digest or final_path.read_bytes() != content:
                raise PlanGraphError(
                    f"logical graph ID {registration.logical_graph_id!r} is already registered differently"
                )
            return final_path
        except OSError as exc:
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EPERM}:
                raise PlanGraphError("registration filesystem does not support atomic hard-link publication") from exc
            raise
        directory = os.open(registration_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return final_path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def plan_from_registration(registration: PlanGraphRegistration) -> PlanGraphPlan:
    try:
        definition = json.loads(registration.definition_json)
    except json.JSONDecodeError as exc:
        raise PlanGraphError(f"invalid registration definition: {exc}") from exc
    if not isinstance(definition, dict):
        raise PlanGraphError("registration definition must be an object")
    return plan_from_mapping(
        {"plan": registration.plan_path, "base_commit": registration.base_commit, **definition}
    )


def verify_registration(
    repository: Path, registration: PlanGraphRegistration
) -> PlanGraphPlan:
    repository = repository.resolve()
    if registration.protocol != REGISTRATION_PROTOCOL:
        raise PlanGraphError("unsupported PlanGraph registration protocol")
    validate_plan_graph_id(registration.logical_graph_id)
    if _normalize_plan_path(repository, registration.plan_path) != registration.plan_path:
        raise PlanGraphError("registration plan path is not normalized")
    try:
        definition = json.loads(registration.definition_json)
    except json.JSONDecodeError as exc:
        raise PlanGraphError(f"invalid registration definition: {exc}") from exc
    if not isinstance(definition, dict) or canonical_json(definition) != registration.definition_json:
        raise PlanGraphError("registration definition is not canonical JSON")
    fields = asdict(registration)
    expected_digest = hashlib.sha256(
        canonical_json(_digest_input(fields, definition)).encode("utf-8")
    ).hexdigest()
    if expected_digest != registration.graph_digest:
        raise PlanGraphError("registration graph digest mismatch")
    if _verify_commit(repository, registration.base_commit) != registration.base_commit:
        raise PlanGraphError("registration base commit is not the full resolved commit")
    plan_bytes = _plan_bytes(repository, registration.base_commit, registration.plan_path)
    if hashlib.sha256(plan_bytes).hexdigest() != registration.plan_sha256:
        raise PlanGraphError("registration approved-plan hash mismatch")
    plan = plan_from_registration(registration)
    validate_plan_graph_plan(plan)
    if canonical_definition(plan) != definition:
        raise PlanGraphError("registration definition does not round-trip")
    return plan


class PlanGraph:
    """Execute one verified registration in dependency order."""

    def __init__(
        self,
        repository: Path,
        registration: PlanGraphRegistration,
        launcher: FeatureRunLauncher,
        *,
        run_root: Path,
        graph_run_id: str | None = None,
        functionality_test_runner: FunctionalityTestRunner | None = None,
    ) -> None:
        if run_root is None:
            raise PlanGraphError("run_root is required for audited PlanGraph execution")
        self.repository = repository.resolve()
        self.registration = registration
        self.plan = verify_registration(self.repository, registration)
        self.launcher = launcher
        self.run_root = run_root.resolve()
        self.graph_run_id = graph_run_id or f"plan-graph-{uuid4().hex}"
        validate_plan_graph_id(self.graph_run_id)
        self._audit: PlanGraphAudit | None = None
        self.functionality_tests = tuple(self.plan.functionality_tests)
        self.functionality_test_runner = functionality_test_runner or (
            lambda command, commit: _run_functionality_test(
                self.repository, command, commit
            )
        )

    def validate(self) -> None:
        validate_plan_graph_plan(self.plan)

    def run(self) -> PlanGraphResult:
        audit = self._audit_for_run()
        if audit.terminal:
            return self._result_from_audit(audit)
        completed = self._load_audit_completed(audit)
        ordered_runs = _ordered_runs(self.plan)
        self._validate_completed_dependencies(ordered_runs, completed)
        candidate_commit = self.plan.base_commit
        for run in ordered_runs:
            if run.id in completed:
                candidate_commit = completed[run.id]
                continue
            request = self._request_for_run(run, candidate_commit)
            audit.node_started(run.id)
            outcome = self.launcher(request)
            if not self._outcome_matches_reservation(outcome, request):
                outcome = FeatureRunOutcome(
                    "failed",
                    evidence={"error": "launcher result does not match reserved child identity"},
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
                raise PlanGraphError(f"successful FeatureRun {run.id!r} did not provide a candidate commit")
            completed[run.id] = outcome.candidate_commit
            candidate_commit = outcome.candidate_commit
            audit.node_completed(run.id, candidate_commit)
        for command in self.functionality_tests:
            try:
                self.functionality_test_runner(command, candidate_commit)
            except Exception as exc:
                result = PlanGraphResult(
                    "failed",
                    candidate_commit,
                    dict(completed),
                    functionality_failure=f"{command}: {exc}",
                )
                audit.functionality_failed(command, candidate_commit, str(exc))
                audit.finalize("failed", self._result_payload(result))
                return result
            audit.functionality_completed(command, candidate_commit)
        result = PlanGraphResult("succeeded", candidate_commit, dict(completed))
        audit.finalize("succeeded", self._result_payload(result))
        return result

    def _audit_for_run(self) -> PlanGraphAudit:
        if self._audit is None:
            nodes = {
                run.id: {
                    "status": "queued",
                    **{name: list(getattr(run, name)) if name != "objective" else run.objective for name in _NODE_DEFINITION_FIELDS},
                    "feature_run_id": self._feature_run_id(run.id),
                    "run_dir": str((self.run_root / self._feature_run_id(run.id)).resolve()),
                    "started_at": None,
                    "finished_at": None,
                    "candidate_commit": None,
                }
                for run in _ordered_runs(self.plan)
            }
            binding = {
                "logical_graph_id": self.registration.logical_graph_id,
                "registration_protocol": self.registration.protocol,
                "registration_digest": self.registration.graph_digest,
                "graph_attempt_id": self.graph_run_id,
            }
            try:
                self._audit = PlanGraphAudit(
                    repository=self.repository,
                    run_root=self.run_root,
                    graph_run_id=self.graph_run_id,
                    plan=self.registration.plan_path,
                    plan_sha256=self.registration.plan_sha256,
                    base_commit=self.registration.base_commit,
                    registration_binding=binding,
                    objective="; ".join(run.objective for run in self.plan.runs),
                    nodes=nodes,
                    functionality_tests=self.functionality_tests,
                )
            except (AuditError, OSError, ValueError) as exc:
                raise PlanGraphError(f"could not open PlanGraph audit: {exc}") from exc
        return self._audit

    @staticmethod
    def _load_audit_completed(audit: PlanGraphAudit) -> dict[str, str]:
        nodes = audit.state.get("nodes")
        if not isinstance(nodes, dict):
            raise PlanGraphError("invalid PlanGraph audit checkpoint: nodes must be an object")
        completed: dict[str, str] = {}
        for node_id, node in nodes.items():
            if not isinstance(node_id, str) or not isinstance(node, dict):
                raise PlanGraphError("invalid PlanGraph audit checkpoint node")
            if node.get("status") == "succeeded":
                candidate = node.get("candidate_commit")
                if not isinstance(candidate, str):
                    raise PlanGraphError("successful PlanGraph audit node has no candidate commit")
                completed[node_id] = candidate
        return completed

    def _feature_run_id(self, node_id: str) -> str:
        return f"{self.graph_run_id}-{node_id}"

    def _request_for_run(self, run: PlanRun, candidate_commit: str) -> FeatureRunRequest:
        if self._audit is None:
            raise PlanGraphError("FeatureRun request requires an initialized PlanGraph audit")
        feature_run_id = self._feature_run_id(run.id)
        return FeatureRunRequest(
            protocol=FEATURE_RUN_REQUEST_PROTOCOL,
            run=run,
            base_commit=candidate_commit,
            plan=self.registration.plan_path,
            plan_base_commit=self.registration.base_commit,
            plan_sha256=self.registration.plan_sha256,
            plan_graph_id=self.graph_run_id,
            plan_node_id=run.id,
            feature_run_id=feature_run_id,
            run_dir=(self.run_root / feature_run_id).resolve(),
        )

    @staticmethod
    def _outcome_matches_reservation(outcome: FeatureRunOutcome, request: FeatureRunRequest) -> bool:
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
                if isinstance(node, dict) and node.get("status") in {"failed", "blocked"}
            ),
            None,
        )
        functionality = state.get("functionality_test", {})
        return PlanGraphResult(
            status=str(state.get("terminal_graph_status")),
            candidate_commit=state.get("current_candidate_commit") if isinstance(state.get("current_candidate_commit"), str) else None,
            completed=completed,
            failed_run_id=failed,
            functionality_failure=functionality.get("error") if isinstance(functionality, dict) and isinstance(functionality.get("error"), str) else None,
        )

    @staticmethod
    def _validate_completed_dependencies(
        ordered_runs: Sequence[PlanRun], completed: Mapping[str, str]
    ) -> None:
        seen_incomplete = False
        for run in ordered_runs:
            if run.id not in completed:
                seen_incomplete = True
            elif seen_incomplete:
                raise PlanGraphError(
                    f"PlanGraph state marks {run.id!r} complete outside sequential candidate lineage"
                )
            elif any(dependency not in completed for dependency in run.depends_on):
                raise PlanGraphError(
                    f"PlanGraph state marks {run.id!r} complete before its dependency"
                )


def _ordered_runs(plan: PlanGraphPlan) -> tuple[PlanRun, ...]:
    by_id = {run.id: run for run in plan.runs}
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

    for run in plan.runs:
        visit(run.id)
    return tuple(ordered)


def validate_plan_graph_plan(plan: PlanGraphPlan) -> None:
    if not plan.plan or not plan.base_commit:
        raise PlanGraphError("plan and base_commit must be non-empty")
    seen: set[str] = set()
    known_runs = {run.id for run in plan.runs}
    covered: set[str] = set()
    for run in plan.runs:
        if not run.id or run.id in seen:
            raise PlanGraphError(f"duplicate or empty run id: {run.id!r}")
        seen.add(run.id)
        if not run.objective.strip() or not run.plan_sections:
            raise PlanGraphError(f"run {run.id!r} has incomplete approved context")
        if any(not value for value in run.verification_argv):
            raise PlanGraphError(f"run {run.id!r} verification_argv contains an empty value")
        cited = "\n".join(plan.plan_sections.get(section, "") for section in run.plan_sections)
        for section in run.plan_sections:
            if section not in plan.plan_sections:
                raise PlanGraphError(f"run {run.id!r} references unknown plan section {section!r}")
        if run.objective not in cited:
            raise PlanGraphError(f"run {run.id!r} objective is absent from its cited plan sections")
        for criterion in run.criteria:
            if criterion not in plan.acceptance_criteria:
                raise PlanGraphError(f"run {run.id!r} references unknown criterion {criterion!r}")
            if criterion not in cited or plan.acceptance_criteria[criterion] not in cited:
                raise PlanGraphError(f"run {run.id!r} criterion {criterion!r} is absent from its cited plan sections")
            covered.add(criterion)
        for dependency in run.depends_on:
            if dependency not in known_runs:
                raise PlanGraphError(f"run {run.id!r} depends on missing run {dependency!r}")
            if dependency == run.id:
                raise PlanGraphError(f"run {run.id!r} depends on itself")
    missing = set(plan.acceptance_criteria) - covered
    if missing:
        raise PlanGraphError("acceptance criteria are not assigned to a FeatureRun: " + ", ".join(sorted(missing)))
    if any(not command.strip() for command in plan.functionality_tests):
        raise PlanGraphError("functionality_tests contains an empty command")
    _ordered_runs(plan)


def _run_functionality_test(repository: Path, command: str, candidate_commit: str) -> None:
    with tempfile.TemporaryDirectory(prefix="plan-graph-") as temporary:
        candidate_path = Path(temporary) / "candidate"
        clone = subprocess.run(
            ["git", "clone", "--shared", "--no-checkout", str(repository), str(candidate_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if clone.returncode:
            raise RuntimeError((clone.stdout + clone.stderr).strip() or "could not prepare candidate checkout")
        checkout = subprocess.run(
            ["git", "-C", str(candidate_path), "checkout", "--detach", candidate_commit],
            text=True,
            capture_output=True,
            check=False,
        )
        if checkout.returncode:
            raise RuntimeError((checkout.stdout + checkout.stderr).strip() or f"could not check out {candidate_commit}")
        completed = subprocess.run(
            command,
            shell=True,
            cwd=candidate_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError((completed.stdout + completed.stderr).strip() or f"exit status {completed.returncode}")


def plan_from_mapping(payload: Mapping[str, object]) -> PlanGraphPlan:
    try:
        runs = tuple(
            PlanRun(
                id=str(item["id"]),
                objective=str(item["objective"]),
                plan_sections=tuple(str(value) for value in item["plan_sections"]),
                criteria=tuple(str(value) for value in item["criteria"]),
                depends_on=tuple(str(value) for value in item.get("depends_on", ())),
                verification_argv=tuple(str(value) for value in item.get("verification_argv", ())),
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
            functionality_tests=tuple(str(value) for value in payload.get("functionality_tests", ())),
        )
    except (KeyError, TypeError) as exc:
        raise PlanGraphError(f"invalid PlanGraph decomposition: {exc}") from exc


__all__ = [
    "FEATURE_RUN_REQUEST_PROTOCOL",
    "REGISTRATION_PROTOCOL",
    "FeatureRunOutcome",
    "FeatureRunRequest",
    "PlanGraph",
    "PlanGraphError",
    "PlanGraphPlan",
    "PlanGraphRegistration",
    "PlanGraphResult",
    "PlanRun",
    "SubprocessFeatureRunLauncher",
    "canonical_definition",
    "load_registration",
    "persist_registration",
    "plan_from_mapping",
    "plan_from_registration",
    "register_plan_graph",
    "registration_bytes",
    "validate_plan_graph_plan",
    "verify_registration",
]
