"""Deterministic registration and sequential execution of approved PlanGraphs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import fcntl
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
from .plan_graph_contract import (
    PLAN_GRAPH_PROTOCOL,
    PlanGraphContractError,
    canonical_plan_graph_payload,
    path_is_allowed,
    plan_graph_identity,
    sha256_json,
)


REGISTRATION_PROTOCOL = "plan-graph-registration/1"
LEGACY_PLAN_PROTOCOL = "component-plan/0"
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
class PlanGraphError(ValueError):
    """Raised when a PlanGraph cannot be registered or executed safely."""


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
class ApprovalEvidence:
    """Revalidated approval identity supplied by the admission controller."""

    subject_sha256: str
    receipt_sha256: str
    plan_graph_digest: str
    audit_record: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PlanRun:
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
class ReadySetDispatch:
    """One stable controller-owned unit selected from the ready frontier.

    Barrier verification is deliberately a dispatch unit, rather than a
    callback on a child completion: it therefore consumes the same bounded
    capacity as a FeatureRun and has a durable scheduling boundary.
    """

    node_id: str
    kind: str


class ReadySetScheduler:
    """Deterministically select runnable nodes under one shared slot budget.

    This component only performs admission.  It never adopts child stdout or
    moves a staging ref; those actions remain controller-owned custody work.
    """

    _FEATURE_RUN = "feature_run"
    _BARRIER_VERIFICATION = "barrier_verification"

    def __init__(
        self,
        runs: Sequence[PlanRun],
        *,
        max_parallelism: int,
        barrier_node_ids: Sequence[str] = (),
    ) -> None:
        if (
            isinstance(max_parallelism, bool)
            or not isinstance(max_parallelism, int)
            or max_parallelism < 1
        ):
            raise PlanGraphError("max_parallelism must be a positive integer")
        self.runs = tuple(runs)
        self.max_parallelism = max_parallelism
        self._by_id = {run.id: run for run in self.runs}
        if len(self._by_id) != len(self.runs) or any(
            not run.id for run in self.runs
        ):
            raise PlanGraphError("ready-set scheduler requires unique non-empty run ids")
        unknown = {
            dependency
            for run in self.runs
            for dependency in run.depends_on
            if dependency not in self._by_id
        }
        if unknown:
            raise PlanGraphError(
                "ready-set scheduler has unknown dependencies: "
                + ", ".join(sorted(unknown))
            )
        if any(
            not isinstance(node_id, str) or not node_id
            for node_id in barrier_node_ids
        ):
            raise PlanGraphError(
                "ready-set scheduler barrier nodes must be non-empty strings"
            )
        self._barrier_node_ids = frozenset(barrier_node_ids)
        unknown_barriers = self._barrier_node_ids - set(self._by_id)
        if unknown_barriers:
            raise PlanGraphError(
                "ready-set scheduler has unknown barrier nodes: "
                + ", ".join(sorted(unknown_barriers))
            )

    def select(
        self,
        sealed: Mapping[str, str] | Sequence[str],
        *,
        active: Sequence[ReadySetDispatch] = (),
        verified_barriers: Sequence[str] = (),
    ) -> tuple[ReadySetDispatch, ...]:
        """Return a stable admission set after validating checkpoint identity."""

        sealed_ids = set(sealed)
        verified = set(verified_barriers)
        if any(not isinstance(node_id, str) for node_id in sealed_ids):
            raise PlanGraphError("sealed nodes must be strings")
        if any(not isinstance(node_id, str) for node_id in verified):
            raise PlanGraphError("verified barriers must be strings")
        unknown_sealed = sealed_ids - set(self._by_id)
        if unknown_sealed:
            raise PlanGraphError(
                "sealed nodes contain unknown ids: "
                + ", ".join(sorted(unknown_sealed))
            )
        incomplete_sealed = {
            node_id
            for node_id in sealed_ids
            if any(
                dependency not in sealed_ids
                for dependency in self._by_id[node_id].depends_on
            )
        }
        if incomplete_sealed:
            raise PlanGraphError(
                "sealed nodes have unsealed dependencies: "
                + ", ".join(sorted(incomplete_sealed))
            )
        unknown_verified = verified - set(self._by_id)
        if unknown_verified:
            raise PlanGraphError(
                "verified barriers contain unknown nodes: "
                + ", ".join(sorted(unknown_verified))
            )
        invalid_verified = {
            node_id
            for node_id in verified
            if node_id not in sealed_ids or node_id not in self._barrier_node_ids
        }
        if invalid_verified:
            raise PlanGraphError(
                "verified barriers are not sealed barrier nodes: "
                + ", ".join(sorted(invalid_verified))
            )

        active_nodes: set[str] = set()
        for unit in active:
            if not isinstance(unit, ReadySetDispatch):
                raise PlanGraphError("active ready-set unit has an invalid type")
            if unit.node_id not in self._by_id:
                raise PlanGraphError(
                    f"active ready-set unit has unknown node {unit.node_id!r}"
                )
            if unit.kind not in {self._FEATURE_RUN, self._BARRIER_VERIFICATION}:
                raise PlanGraphError(
                    f"active ready-set unit has invalid kind {unit.kind!r}"
                )
            if unit.node_id in active_nodes:
                raise PlanGraphError(
                    f"active ready-set units repeat node {unit.node_id!r}"
                )
            active_nodes.add(unit.node_id)
            run = self._by_id[unit.node_id]
            if unit.kind == self._FEATURE_RUN:
                if unit.node_id in sealed_ids:
                    raise PlanGraphError(
                        f"active feature run is already sealed: {unit.node_id!r}"
                    )
                if not all(dependency in sealed_ids for dependency in run.depends_on):
                    raise PlanGraphError(
                        f"active feature run has unsealed dependencies: {unit.node_id!r}"
                    )
                if any(
                    dependency in self._barrier_node_ids and dependency not in verified
                    for dependency in run.depends_on
                ):
                    raise PlanGraphError(
                        f"active feature run has unverified barriers: {unit.node_id!r}"
                    )
            else:
                if unit.node_id not in self._barrier_node_ids:
                    raise PlanGraphError(
                        "active barrier verification is not configured: "
                        f"{unit.node_id!r}"
                    )
                if unit.node_id not in sealed_ids:
                    raise PlanGraphError(
                        f"active barrier verification is not sealed: {unit.node_id!r}"
                    )
                if unit.node_id in verified:
                    raise PlanGraphError(
                        "active barrier verification is already verified: "
                        f"{unit.node_id!r}"
                    )
                if not any(
                    candidate.id not in sealed_ids
                    and unit.node_id in candidate.depends_on
                    and all(dependency in sealed_ids for dependency in candidate.depends_on)
                    for candidate in self.runs
                ):
                    raise PlanGraphError(
                        f"active barrier verification is not dependency-ready: {unit.node_id!r}"
                    )
        if len(active) > self.max_parallelism:
            raise PlanGraphError("active ready-set units exceed max_parallelism")

        selected: list[ReadySetDispatch] = []
        available = self.max_parallelism - len(active)
        for run in self.runs:
            if not available:
                break
            if run.id in sealed_ids or run.id in active_nodes:
                continue
            if not all(dependency in sealed_ids for dependency in run.depends_on):
                continue
            pending_barriers = [
                dependency
                for dependency in run.depends_on
                if dependency in self._barrier_node_ids and dependency not in verified
            ]
            if pending_barriers:
                for dependency in pending_barriers:
                    if dependency not in active_nodes and not any(
                        unit.node_id == dependency for unit in selected
                    ):
                        selected.append(
                            ReadySetDispatch(dependency, self._BARRIER_VERIFICATION)
                        )
                        available -= 1
                        if not available:
                            break
                continue
            selected.append(ReadySetDispatch(run.id, self._FEATURE_RUN))
            available -= 1
        return tuple(selected)


@dataclass(frozen=True)
class PlanGraphPlan:
    plan: str
    base_commit: str
    runs: tuple[PlanRun, ...]
    plan_sections: Mapping[str, str]
    acceptance_criteria: Mapping[str, str]
    functionality_tests: tuple[FunctionalityCommand | str, ...] = ()
    repository_id: str = "component-plan"
    protocol: str = LEGACY_PLAN_PROTOCOL
    plan_sha256: str | None = None
    canonical_decomposition: Mapping[str, object] | None = None


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
    finding_obligations: tuple[Mapping[str, object], ...] = ()
    finding_transfer_targets: Mapping[str, str] | None = None

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


@dataclass(frozen=True)
class RepairResumeDirective:
    """Controller-authorized retry frontier for an immutable successor attempt."""

    logical_graph_id: str
    predecessor_attempt_id: str
    retry_frontier: tuple[str, ...]
    blocker_evidence_ref: str


FeatureRunLauncher = Callable[[FeatureRunRequest], FeatureRunOutcome]
FunctionalityTestRunner = Callable[[FunctionalityCommand | str, str], None]
ApprovalValidator = Callable[[], ApprovalEvidence]


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
                "allowed_paths": list(request.run.allowed_paths),
                "path_intents": [
                    {"path": value.path, "action": value.action}
                    for value in request.run.path_intents
                ],
                "verification_timeout_seconds": (
                    request.run.verification_timeout_seconds
                ),
                "verification_required_paths": [
                    _required_path_mapping(value)
                    for value in request.run.verification_required_paths
                ],
            },
            "base_commit": request.base_commit,
            "plan": request.plan,
            "plan_base_commit": request.plan_base_commit,
            "plan_sha256": request.plan_sha256,
            "plan_graph_id": request.plan_graph_id,
            "plan_node_id": request.plan_node_id,
            "feature_run_id": request.feature_run_id,
            "run_dir": str(request.run_dir),
            "finding_obligations": [
                dict(item) for item in request.finding_obligations
            ],
            "finding_transfer_targets": dict(
                request.finding_transfer_targets or {}
            ),
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


def _functionality_test_payload(value: FunctionalityCommand | str) -> object:
    return _command_mapping(value) if isinstance(value, FunctionalityCommand) else value


def _run_mapping(value: PlanRun) -> dict[str, object]:
    return {
        "id": value.id,
        "objective": value.objective,
        "plan_sections": list(value.plan_sections),
        "criteria": list(value.criteria),
        "depends_on": list(value.depends_on),
        "verification_argv": list(value.verification_argv),
        "allowed_paths": list(value.allowed_paths),
        "path_intents": [
            {"path": item.path, "action": item.action}
            for item in value.path_intents
        ],
        "verification_timeout_seconds": value.verification_timeout_seconds,
        "verification_required_paths": [
            _required_path_mapping(item)
            for item in value.verification_required_paths
        ],
    }


def _normalize_functionality_command(
    value: FunctionalityCommand | str,
) -> FunctionalityCommand:
    if isinstance(value, FunctionalityCommand):
        return value
    return FunctionalityCommand(("sh", "-c", value))


def canonical_definition(plan: PlanGraphPlan) -> dict[str, object]:
    if plan.protocol == PLAN_GRAPH_PROTOCOL:
        if plan.canonical_decomposition is None:
            raise PlanGraphError(
                "approved PlanGraph plan is missing its canonical decomposition"
            )
        return {
            "protocol": PLAN_GRAPH_PROTOCOL,
            "repository_id": plan.repository_id,
            "canonical_decomposition": dict(plan.canonical_decomposition),
        }
    return {
        "runs": [_run_mapping(run) for run in plan.runs],
        "plan_sections": dict(plan.plan_sections),
        "acceptance_criteria": dict(plan.acceptance_criteria),
        "functionality_tests": [
            _functionality_test_payload(value) for value in plan.functionality_tests
        ],
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
    base_commit: str | None = None,
    repository_id: str | None = None,
) -> PlanGraphRegistration:
    repository = repository.resolve()
    validate_plan_graph_id(logical_graph_id)
    if decomposition.get("protocol") == PLAN_GRAPH_PROTOCOL:
        if base_commit is None or repository_id is None:
            raise PlanGraphError(
                "canonical PlanGraph registration requires approval-bound "
                "base_commit and repository_id"
            )
        base_commit = _verify_commit(repository, base_commit)
        try:
            canonical = canonical_plan_graph_payload(decomposition)
        except PlanGraphContractError as exc:
            raise PlanGraphError(f"invalid PlanGraph decomposition: {exc}") from exc
        plan_path = _normalize_plan_path(repository, str(canonical["plan"]))
        plan_sha256 = hashlib.sha256(
            _plan_bytes(repository, base_commit, plan_path)
        ).hexdigest()
        plan = plan_from_mapping(
            decomposition,
            base_commit=base_commit,
            repository_id=repository_id,
            plan_sha256=plan_sha256,
        )
    else:
        if base_commit is not None or repository_id is not None:
            raise PlanGraphError(
                "base_commit and repository_id are approval-bound and only "
                "accepted for canonical PlanGraph payloads"
            )
        plan = plan_from_mapping(decomposition)
        base_commit = _verify_commit(repository, plan.base_commit)
        plan_path = _normalize_plan_path(repository, plan.plan)
        plan_sha256 = hashlib.sha256(
            _plan_bytes(repository, base_commit, plan_path)
        ).hexdigest()
    validate_plan_graph_plan(plan)
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
    if definition.get("protocol") == PLAN_GRAPH_PROTOCOL:
        canonical = definition.get("canonical_decomposition")
        repository_id = definition.get("repository_id")
        if not isinstance(canonical, dict) or not isinstance(repository_id, str):
            raise PlanGraphError(
                "canonical registration definition must carry repository_id "
                "and canonical_decomposition"
            )
        plan = plan_from_mapping(
            canonical,
            base_commit=registration.base_commit,
            repository_id=repository_id,
            plan_sha256=registration.plan_sha256,
        )
        if plan.plan != registration.plan_path:
            raise PlanGraphError(
                "canonical registration plan path does not match its decomposition"
            )
        return plan
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
        approval_validator: ApprovalValidator | None = None,
        child_liveness_probe: Callable[[int], str | None] | None = None,
        force_reconcile_records: Sequence[Mapping[str, object]] = (),
        logical_graph_id: str | None = None,
        predecessor_attempt_id: str | None = None,
        resume_directive: RepairResumeDirective | None = None,
        reused_completed: Mapping[str, str] | None = None,
        predecessor_checkpoint: Mapping[str, object] | None = None,
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
        self.approval_validator = approval_validator
        self._approval: ApprovalEvidence | None = None
        if self.plan.protocol == PLAN_GRAPH_PROTOCOL and approval_validator is None:
            raise PlanGraphError("approved PlanGraph requires an approval receipt")
        self.child_liveness_probe = child_liveness_probe or _local_process_start_token
        self.force_reconcile_records = tuple(force_reconcile_records)
        self.logical_graph_id = logical_graph_id or self.graph_run_id
        self.predecessor_attempt_id = predecessor_attempt_id
        self.resume_directive = resume_directive
        self.reused_completed = dict(reused_completed or {})
        self.predecessor_checkpoint = (
            dict(predecessor_checkpoint) if predecessor_checkpoint is not None else None
        )

    @classmethod
    def resume(
        cls,
        repository: Path,
        registration: PlanGraphRegistration,
        launcher: FeatureRunLauncher,
        *,
        run_root: Path,
        directive: RepairResumeDirective,
        **kwargs: object,
    ) -> "PlanGraph":
        """Create a new repair attempt without mutating its predecessor."""
        repository = repository.resolve()
        plan = verify_registration(repository, registration)

        for value, label in (
            (directive.logical_graph_id, "logical_graph_id"),
            (directive.predecessor_attempt_id, "predecessor_attempt_id"),
        ):
            if not isinstance(value, str) or not value or value in {".", ".."} or "/" in value or "\\" in value:
                raise PlanGraphError(f"{label} must be a non-empty path-safe name")
        if not isinstance(directive.blocker_evidence_ref, str) or not directive.blocker_evidence_ref.startswith("artifact:sha256:"):
            raise PlanGraphError("repair resume requires a blocker evidence reference")
        run_root = run_root.resolve()
        lock_dir = run_root / ".plan-graph-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_id = hashlib.sha256(directive.logical_graph_id.encode("utf-8")).hexdigest()
        with (lock_dir / f"{lock_id}.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                supplied_tests = tuple(
                    _functionality_test_payload(command)
                    for command in plan.functionality_tests
                )
                contract_nodes = cls._contract_nodes(plan)
                predecessor = PlanGraphAudit.open_repair_predecessor(
                    run_root=run_root,
                    graph_run_id=directive.predecessor_attempt_id,
                    plan=plan.plan,
                    plan_sha256=registration.plan_sha256,
                    base_commit=plan.base_commit,
                    logical_graph_id=directive.logical_graph_id,
                    plan_graph_digest=PlanGraphAudit.repair_contract_digest(
                        plan=plan.plan, plan_sha256=registration.plan_sha256,
                        base_commit=plan.base_commit,
                        nodes=contract_nodes, functionality_tests=supplied_tests,
                        plan_sections=plan.plan_sections,
                        acceptance_criteria=plan.acceptance_criteria,
                    ),
                )
                selection = predecessor.repair_selection(
                    retry_frontier=directive.retry_frontier,
                    blocker_evidence_ref=directive.blocker_evidence_ref,
                )
                attempt_id = f"{directive.logical_graph_id}-attempt-{cls._next_repair_ordinal(run_root, directive.logical_graph_id)}"
                graph = cls(
                    repository, registration, launcher,
                    run_root=run_root, graph_run_id=attempt_id,
                    logical_graph_id=directive.logical_graph_id,
                    predecessor_attempt_id=directive.predecessor_attempt_id,
                    resume_directive=directive,
                    reused_completed=selection["reused_completed"],
                    predecessor_checkpoint=selection["predecessor_checkpoint"],
                    **kwargs,
                )
                graph.validate()
                graph._revalidate_approval()
                graph._audit_for_run()
                return graph
            except (AuditError, OSError, ValueError) as exc:
                raise PlanGraphError(f"could not allocate repair successor: {exc}") from exc
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _contract_nodes(plan: PlanGraphPlan) -> dict[str, dict[str, object]]:
        return {run.id: {"objective": run.objective, "plan_sections": list(run.plan_sections), "depends_on": list(run.depends_on), "criteria": list(run.criteria), "verification_argv": list(run.verification_argv)} for run in plan.runs}

    @staticmethod
    def _next_repair_ordinal(run_root: Path, logical_graph_id: str) -> int:
        prefix = f"{logical_graph_id}-attempt-"
        ordinals = [int(path.name.removeprefix(prefix)) for path in run_root.glob(f"{prefix}*") if path.is_dir() and path.name.removeprefix(prefix).isdigit()]
        return max(ordinals, default=0) + 1

    def validate(self) -> None:
        validate_plan_graph_plan(self.plan)


    def run(self) -> PlanGraphResult:
        self._revalidate_approval()
        audit = self._audit_for_run()
        if audit.terminal:
            return self._result_from_audit(audit)
        recovery = audit.reconcile_interrupted_attempts(
            process_probe=self.child_liveness_probe,
            force_records=self.force_reconcile_records,
        )
        completed = self._load_audit_completed(audit)
        if any(outcome == "running" for outcome in recovery.values()):
            # An observed live child continues to own its allocation.  This
            # synchronous compatibility runner has no authority to adopt or
            # redispatch it.
            return PlanGraphResult("running", None, dict(completed))
        blocked = next(
            (node_id for node_id, outcome in recovery.items() if outcome == "blocked"),
            None,
        )
        if blocked is not None:
            result = PlanGraphResult("blocked", None, dict(completed), blocked)
            audit.finalize("blocked", self._result_payload(result))
            return result
        finding_obligations = self._load_finding_obligations(audit)
        ordered_runs = _ordered_runs(self.plan)
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
            request = self._request_for_run(
                run,
                candidate_commit,
                tuple(finding_obligations.get(run.id, ())),
            )
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
                raise PlanGraphError(
                    f"successful FeatureRun {run.id!r} did not provide a candidate commit"
                )
            try:
                finding_obligations = self._advance_finding_obligations(
                    run,
                    request,
                    outcome,
                    finding_obligations,
                    completed,
                )
            except PlanGraphError as exc:
                result = PlanGraphResult(
                    status="failed",
                    candidate_commit=None,
                    completed=dict(completed),
                    failed_run_id=run.id,
                )
                audit.node_failed(run.id, "failed", {"error": str(exc)})
                audit.finalize("failed", self._result_payload(result))
                return result
            completed[run.id] = outcome.candidate_commit
            candidate_commit = outcome.candidate_commit
            audit.node_completed(
                run.id,
                candidate_commit,
                finding_obligations=finding_obligations,
            )

        for command in self.functionality_tests:
            recorded = _functionality_test_payload(command)
            rendered = (
                json.dumps(list(command.argv))
                if isinstance(command, FunctionalityCommand)
                else command
            )
            try:
                self.functionality_test_runner(command, candidate_commit)
            except Exception as exc:
                result = PlanGraphResult(
                    "failed",
                    candidate_commit,
                    dict(completed),
                    functionality_failure=f"{rendered}: {exc}",
                )
                audit.functionality_failed(recorded, candidate_commit, str(exc))
                audit.finalize("failed", self._result_payload(result))
                return result
            audit.functionality_completed(recorded, candidate_commit)
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
        self._approval = evidence
        return evidence

    def _identity_digest(self) -> str:
        if (
            self.plan.protocol == PLAN_GRAPH_PROTOCOL
            and self.plan.canonical_decomposition is not None
        ):
            try:
                return plan_graph_identity(
                    repository_id=self.plan.repository_id,
                    base_commit=self.registration.base_commit,
                    plan_sha256=self.registration.plan_sha256,
                    decomposition=self.plan.canonical_decomposition,
                )
            except PlanGraphContractError as exc:
                raise PlanGraphError(f"invalid canonical PlanGraph: {exc}") from exc
        return sha256_json(
            {
                "protocol": "component-plan-identity/0",
                "repository_id": self.plan.repository_id,
                "base_commit": self.registration.base_commit,
                "plan_sha256": self.registration.plan_sha256,
                "runs": [_run_mapping(run) for run in self.plan.runs],
                "functionality_tests": [
                    _functionality_test_payload(command)
                    for command in self.functionality_tests
                ],
            }
        )

    def _approval_record(self) -> dict[str, object] | None:
        if self._approval is None:
            return None
        if self._approval.audit_record is not None:
            return dict(self._approval.audit_record)
        return {
            "subject_sha256": self._approval.subject_sha256,
            "receipt_sha256": self._approval.receipt_sha256,
        }

    def _audit_for_run(self) -> PlanGraphAudit:
        if self._audit is None:
            nodes = {
                run.id: {
                    "status": "succeeded" if run.id in self.reused_completed else "queued",
                    **_run_mapping(run),
                    "feature_run_id": self._feature_run_id(run.id),
                    "run_dir": str((self.run_root / self._feature_run_id(run.id)).resolve()),
                    "started_at": None,
                    "finished_at": "reused" if run.id in self.reused_completed else None,
                    "candidate_commit": self.reused_completed.get(run.id),
                    "reused_from_attempt": self.predecessor_attempt_id if run.id in self.reused_completed else None,
                }
                for run in _ordered_runs(self.plan)
            }
            for node in nodes.values():
                node.pop("id", None)
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
                    repository_id=self.plan.repository_id,
                    approval=self._approval_record(),
                    objective="; ".join(run.objective for run in self.plan.runs),
                    nodes=nodes,
                    functionality_tests=tuple(
                        _functionality_test_payload(command)
                        for command in self.functionality_tests
                    ),
                    plan_sections=self.plan.plan_sections,
                    acceptance_criteria=self.plan.acceptance_criteria,
                    logical_graph_id=self.logical_graph_id,
                    graph_attempt_id=self.graph_run_id,
                    predecessor_attempt_id=self.predecessor_attempt_id,
                    resume_directive=self.resume_directive,
                    predecessor_checkpoint=self.predecessor_checkpoint,
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

    def _request_for_run(
        self,
        run: PlanRun,
        candidate_commit: str,
        finding_obligations: tuple[Mapping[str, object], ...] = (),
    ) -> FeatureRunRequest:
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
            finding_obligations=finding_obligations,
            finding_transfer_targets=self._transfer_targets_for(run),
        )

    @staticmethod
    def _load_finding_obligations(
        audit: PlanGraphAudit,
    ) -> dict[str, list[Mapping[str, object]]]:
        raw = audit.state.get("finding_obligations", {})
        if not isinstance(raw, dict):
            raise PlanGraphError(
                "invalid PlanGraph audit checkpoint: finding_obligations must be an object"
            )
        loaded: dict[str, list[Mapping[str, object]]] = {}
        for node_id, findings in raw.items():
            if not isinstance(node_id, str) or not isinstance(findings, list):
                raise PlanGraphError("invalid PlanGraph finding obligation checkpoint")
            if not all(isinstance(item, dict) for item in findings):
                raise PlanGraphError("invalid PlanGraph finding obligation record")
            loaded[node_id] = [dict(item) for item in findings]
        return loaded

    def _advance_finding_obligations(
        self,
        run: PlanRun,
        request: FeatureRunRequest,
        outcome: FeatureRunOutcome,
        current: Mapping[str, list[Mapping[str, object]]],
        completed: Mapping[str, str],
    ) -> dict[str, list[Mapping[str, object]]]:
        pending = {
            node_id: [dict(item) for item in findings]
            for node_id, findings in current.items()
            if node_id != run.id
        }
        transferred = self._transferred_findings(outcome)
        targets = request.finding_transfer_targets or {}
        for finding in transferred:
            key = finding.get("key")
            required_paths = finding.get("required_paths")
            target = finding.get("transferred_to")
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(target, str)
                or not target
                or not isinstance(required_paths, list)
                or not required_paths
                or not all(isinstance(path, str) and path for path in required_paths)
            ):
                raise PlanGraphError("child returned an invalid transferred finding")
            expected_owners = {
                _target_for_path(path, targets) for path in required_paths
            }
            if None in expected_owners or expected_owners != {target}:
                raise PlanGraphError(
                    f"finding {key} did not resolve uniquely to {target!r}"
                )
            if target in completed or target == run.id:
                raise PlanGraphError(
                    f"finding {key} was transferred to a completed or current node"
                )
            destination = pending.setdefault(target, [])
            if any(item.get("key") == key for item in destination):
                raise PlanGraphError(
                    f"duplicate transferred finding {key} for node {target}"
                )
            destination.append(dict(finding))
        return pending

    @staticmethod
    def _transferred_findings(
        outcome: FeatureRunOutcome,
    ) -> tuple[Mapping[str, object], ...]:
        if outcome.evidence is None:
            return ()
        if not isinstance(outcome.evidence, Mapping):
            raise PlanGraphError("FeatureRun evidence must be an object")
        raw = outcome.evidence.get("transferred_findings", ())
        if not isinstance(raw, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in raw
        ):
            raise PlanGraphError("transferred_findings must be a list of objects")
        return tuple(dict(item) for item in raw)

    def _transfer_targets_for(self, run: PlanRun) -> dict[str, str]:
        """Resolve each downstream path grant to its nearest unique owner."""

        by_id = {item.id: item for item in self.plan.runs}
        distances = {run.id: 0}
        frontier = [run.id]
        while frontier:
            predecessor = frontier.pop(0)
            for candidate in self.plan.runs:
                if predecessor not in candidate.depends_on:
                    continue
                distance = distances[predecessor] + 1
                if candidate.id not in distances or distance < distances[candidate.id]:
                    distances[candidate.id] = distance
                    frontier.append(candidate.id)
        candidates: dict[str, list[tuple[int, str]]] = {}
        for node_id, distance in distances.items():
            if node_id == run.id:
                continue
            for path in by_id[node_id].allowed_paths:
                candidates.setdefault(path, []).append((distance, node_id))
        targets: dict[str, str] = {}
        for path, owners in candidates.items():
            minimum = min(distance for distance, _ in owners)
            nearest = {node_id for distance, node_id in owners if distance == minimum}
            if len(nearest) == 1:
                targets[path] = next(iter(nearest))
        return targets

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
    strict = plan.protocol == PLAN_GRAPH_PROTOCOL
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
        if strict and not run.verification_argv:
            raise PlanGraphError(
                f"run {run.id!r} requires controller-owned verification"
            )
        if any(not value for value in run.allowed_paths):
            raise PlanGraphError(f"run {run.id!r} allowed_paths contains an empty value")
        if strict and not run.allowed_paths:
            raise PlanGraphError(f"run {run.id!r} has no allowed_paths")
        if run.verification_timeout_seconds <= 0:
            raise PlanGraphError(
                f"run {run.id!r} verification timeout must be positive"
            )
        for intent in run.path_intents:
            if intent.action not in {"create", "modify"}:
                raise PlanGraphError(
                    f"run {run.id!r} has invalid path intent {intent.action!r}"
                )
            if not path_is_allowed(intent.path, run.allowed_paths):
                raise PlanGraphError(
                    f"run {run.id!r} path intent {intent.path!r} is outside allowed_paths"
                )
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
    for command in plan.functionality_tests:
        if isinstance(command, FunctionalityCommand):
            if not command.argv or any(not value for value in command.argv):
                raise PlanGraphError("functionality_tests contains an empty command")
            if command.timeout_seconds <= 0:
                raise PlanGraphError("functionality test timeout must be positive")
        elif not command.strip():
            raise PlanGraphError("functionality_tests contains an empty command")
    _ordered_runs(plan)
    _validate_command_dependencies(plan)


def _validate_command_dependencies(plan: PlanGraphPlan) -> None:
    """Require every created-path consumer to name an ordered, declared producer."""

    by_id = {run.id: run for run in plan.runs}

    def ancestors(run_id: str) -> set[str]:
        result: set[str] = set()
        pending = list(by_id[run_id].depends_on)
        while pending:
            dependency = pending.pop()
            if dependency in result or dependency not in by_id:
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

    for run in plan.runs:
        for required in run.verification_required_paths:
            validate_required(required, run.id)
    for command in plan.functionality_tests:
        if isinstance(command, FunctionalityCommand):
            for required in command.required_paths:
                validate_required(required, None)


def _local_process_start_token(pid: int) -> str | None:
    """Return an immutable process-start token when the host can observe one."""

    try:
        return str(os.stat(f"/proc/{pid}").st_ctime_ns)
    except OSError:
        pass
    try:
        observed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return observed.stdout.strip() or None


def _run_functionality_test(
    repository: Path,
    command: FunctionalityCommand | str,
    candidate_commit: str,
) -> None:
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
        if isinstance(command, FunctionalityCommand):
            completed = subprocess.run(
                command.argv,
                cwd=candidate_path,
                text=True,
                capture_output=True,
                check=False,
                timeout=command.timeout_seconds,
            )
        else:
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


def plan_from_mapping(
    payload: Mapping[str, object],
    *,
    base_commit: str | None = None,
    repository_id: str | None = None,
    plan_sha256: str | None = None,
) -> PlanGraphPlan:
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
    if base_commit is not None or repository_id is not None or plan_sha256 is not None:
        raise PlanGraphError(
            "approval-bound repository metadata is only accepted for canonical "
            "PlanGraph payloads"
        )
    try:
        runs = tuple(
            PlanRun(
                id=str(item["id"]),
                objective=str(item["objective"]),
                plan_sections=tuple(str(value) for value in item["plan_sections"]),
                criteria=tuple(str(value) for value in item["criteria"]),
                depends_on=tuple(str(value) for value in item.get("depends_on", ())),
                verification_argv=tuple(str(value) for value in item.get("verification_argv", ())),
                allowed_paths=tuple(str(value) for value in item.get("allowed_paths", ())),
                path_intents=tuple(
                    PathIntent(str(intent["path"]), str(intent["action"]))
                    for intent in item.get("path_intents", ())
                ),
                verification_timeout_seconds=float(
                    item.get("verification_timeout_seconds", 1200.0)
                ),
                verification_required_paths=tuple(
                    _required_path_from_mapping(value)
                    for value in item.get("verification_required_paths", ())
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
                _command_from_mapping(value) if isinstance(value, Mapping) else str(value)
                for value in payload.get("functionality_tests", ())
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanGraphError(f"invalid PlanGraph decomposition: {exc}") from exc


def _run_from_mapping(value: Mapping[str, object]) -> PlanRun:
    return PlanRun(
        id=str(value["id"]),
        objective=str(value["objective"]),
        plan_sections=tuple(str(item) for item in value["plan_sections"]),  # type: ignore[union-attr]
        criteria=tuple(str(item) for item in value["criteria"]),  # type: ignore[union-attr]
        depends_on=tuple(str(item) for item in value["depends_on"]),  # type: ignore[union-attr]
        verification_argv=tuple(str(item) for item in value["verification_argv"]),  # type: ignore[union-attr]
        allowed_paths=tuple(str(item) for item in value["allowed_paths"]),  # type: ignore[union-attr]
        path_intents=tuple(
            PathIntent(str(item["path"]), str(item["action"]))
            for item in value["path_intents"]  # type: ignore[union-attr]
        ),
        verification_timeout_seconds=float(value["verification_timeout_seconds"]),  # type: ignore[arg-type]
        verification_required_paths=tuple(
            _required_path_from_mapping(item)
            for item in value["verification_required_paths"]  # type: ignore[union-attr]
        ),
    )


def _command_from_mapping(value: Mapping[str, object]) -> FunctionalityCommand:
    return FunctionalityCommand(
        argv=tuple(str(item) for item in value["argv"]),  # type: ignore[union-attr]
        timeout_seconds=float(value["timeout_seconds"]),  # type: ignore[arg-type]
        required_paths=tuple(
            _required_path_from_mapping(item)
            for item in value.get("required_paths", ())  # type: ignore[union-attr]
        ),
    )


def _required_path_from_mapping(value: Mapping[str, object]) -> RequiredPath:
    producer = value.get("producer_run_id")
    return RequiredPath(
        path=str(value["path"]),
        availability=str(value.get("availability", "base")),
        producer_run_id=str(producer) if producer is not None else None,
    )


def _target_for_path(path: str, targets: Mapping[str, str]) -> str | None:
    matches = []
    for grant, target in targets.items():
        normalized = grant.rstrip("/")
        if path == normalized or (grant.endswith("/") and path.startswith(grant)):
            matches.append((len(normalized), target))
    if not matches:
        return None
    longest = max(length for length, _ in matches)
    owners = {target for length, target in matches if length == longest}
    return next(iter(owners)) if len(owners) == 1 else None


__all__ = [
    "FEATURE_RUN_REQUEST_PROTOCOL",
    "PLAN_GRAPH_PROTOCOL",
    "REGISTRATION_PROTOCOL",
    "ApprovalEvidence",
    "FeatureRunOutcome",
    "FeatureRunRequest",
    "FunctionalityCommand",
    "PathIntent",
    "PlanGraph",
    "RequiredPath",
    "PlanGraphError",
    "PlanGraphPlan",
    "PlanGraphRegistration",
    "PlanGraphResult",
    "PlanRun",
    "ReadySetDispatch",
    "ReadySetScheduler",
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
