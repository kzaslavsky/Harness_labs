"""A small sequential queue for dependent FeatureRuns.

PlanGraph deliberately owns scheduling only.  A caller supplies the approved
plan references, the FeatureRun launcher, and (optionally) the final test
runner; selecting or configuring a backend remains outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from .audit import AuditError
from .plan_graph_audit import PlanGraphAudit


class PlanGraphError(ValueError):
    """Raised when a proposed plan decomposition cannot be executed safely."""


@dataclass(frozen=True)
class PlanRun:
    """One FeatureRun proposed from an approved plan."""

    id: str
    objective: str
    plan_sections: tuple[str, ...]
    criteria: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    verification_argv: tuple[str, ...] = ()


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
    """The approved plan references and its proposed sequential decomposition."""

    plan: str
    base_commit: str
    runs: tuple[PlanRun, ...]
    plan_sections: Mapping[str, str]
    acceptance_criteria: Mapping[str, str]
    functionality_tests: tuple[str, ...] = ()


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
class RepairResumeDirective:
    """Controller-authorized retry frontier for an immutable successor attempt."""

    logical_graph_id: str
    predecessor_attempt_id: str
    retry_frontier: tuple[str, ...]
    blocker_evidence_ref: str


FeatureRunLauncher = Callable[[FeatureRunRequest], FeatureRunOutcome]
FunctionalityTestRunner = Callable[[str, str], None]


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
        functionality_tests: Sequence[str] = (),
        functionality_test_runner: FunctionalityTestRunner | None = None,
        child_liveness_probe: Callable[[int], str | None] | None = None,
        force_reconcile_records: Sequence[Mapping[str, object]] = (),
        logical_graph_id: str | None = None,
        predecessor_attempt_id: str | None = None,
        resume_directive: RepairResumeDirective | None = None,
        reused_completed: Mapping[str, str] | None = None,
        predecessor_checkpoint: Mapping[str, object] | None = None,
    ) -> None:
        self.plan = plan
        self.launcher = launcher
        if run_root is None:
            raise PlanGraphError("run_root is required for audited PlanGraph execution")
        self.run_root = run_root
        self.graph_run_id = graph_run_id or f"plan-graph-{uuid4().hex}"
        self._audit: PlanGraphAudit | None = None
        self.functionality_tests = (
            tuple(plan.functionality_tests) + tuple(functionality_tests)
        )
        self.functionality_test_runner = (
            functionality_test_runner or _run_functionality_test
        )
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
        plan: PlanGraphPlan,
        launcher: FeatureRunLauncher,
        *,
        run_root: Path,
        directive: RepairResumeDirective,
        **kwargs: object,
    ) -> "PlanGraph":
        """Create a new repair attempt without mutating its predecessor."""

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
                supplied_tests = tuple(plan.functionality_tests) + tuple(kwargs.get("functionality_tests", ()))
                contract_nodes = cls._contract_nodes(plan)
                predecessor = PlanGraphAudit.open_repair_predecessor(
                    run_root=run_root,
                    graph_run_id=directive.predecessor_attempt_id,
                    plan=plan.plan,
                    base_commit=plan.base_commit,
                    logical_graph_id=directive.logical_graph_id,
                    plan_graph_digest=PlanGraphAudit.repair_contract_digest(
                        plan=plan.plan, base_commit=plan.base_commit,
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
                    plan, launcher, run_root=run_root, graph_run_id=attempt_id,
                    logical_graph_id=directive.logical_graph_id,
                    predecessor_attempt_id=directive.predecessor_attempt_id,
                    resume_directive=directive,
                    reused_completed=selection["reused_completed"],
                    predecessor_checkpoint=selection["predecessor_checkpoint"],
                    **kwargs,
                )
                graph.validate()
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
        """Reject invalid references and cycles before a launcher can run."""

        if not self.plan.plan:
            raise PlanGraphError("plan path must not be empty")
        if not self.plan.base_commit:
            raise PlanGraphError("base_commit must not be empty")
        seen: set[str] = set()
        known_runs = {run.id for run in self.plan.runs}
        covered: set[str] = set()
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
        if any(not command.strip() for command in self.plan.functionality_tests):
            raise PlanGraphError("functionality_tests contains an empty command")
        self._ordered_runs()

    def run(self) -> PlanGraphResult:
        """Validate, resume completed nodes, then launch runs sequentially."""

        self.validate()
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
        ordered_runs = self._ordered_runs()
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
                result = PlanGraphResult(
                    status="failed",
                    candidate_commit=candidate_commit,
                    completed=dict(completed),
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
            assert self.graph_run_id is not None
            nodes = {
                run.id: {
                    "status": "succeeded" if run.id in self.reused_completed else "queued",
                    "objective": run.objective,
                    "plan_sections": list(run.plan_sections),
                    "depends_on": list(run.depends_on),
                    "criteria": list(run.criteria),
                    "verification_argv": list(run.verification_argv),
                    "feature_run_id": self._feature_run_id(run.id),
                    "run_dir": str(
                        (self.run_root / self._feature_run_id(run.id)).resolve()
                    ),
                    "started_at": None,
                    "finished_at": "reused" if run.id in self.reused_completed else None,
                    "candidate_commit": self.reused_completed.get(run.id),
                    "reused_from_attempt": self.predecessor_attempt_id if run.id in self.reused_completed else None,
                }
                for run in self._ordered_runs()
            }
            try:
                self._audit = PlanGraphAudit(
                    run_root=self.run_root,
                    graph_run_id=self.graph_run_id,
                    plan=self.plan.plan,
                    base_commit=self.plan.base_commit,
                    objective="; ".join(run.objective for run in self.plan.runs),
                    nodes=nodes,
                    functionality_tests=self.functionality_tests,
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


def _run_functionality_test(command: str, candidate_commit: str) -> None:
    with tempfile.TemporaryDirectory(prefix="plan-graph-") as temporary:
        candidate_path = Path(temporary) / "candidate"
        clone = subprocess.run(
            ["git", "clone", "--shared", "--no-checkout", ".", str(candidate_path)],
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
                command,
                shell=True,
                cwd=candidate_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode:
                output = (completed.stdout + completed.stderr).strip()
                raise RuntimeError(output or f"exit status {completed.returncode}")
        except OSError as exc:
            raise RuntimeError(f"could not prepare {candidate_commit} for testing: {exc}") from exc


def plan_from_mapping(payload: Mapping[str, object]) -> PlanGraphPlan:
    """Build a PlanGraphPlan from the CLI's explicit decomposition payload."""

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


__all__ = [
    "FeatureRunOutcome",
    "FeatureRunRequest",
    "PlanGraph",
    "PlanGraphError",
    "PlanGraphPlan",
    "PlanGraphResult",
    "PlanRun",
    "ReadySetDispatch",
    "ReadySetScheduler",
    "SubprocessFeatureRunLauncher",
    "plan_from_mapping",
]
