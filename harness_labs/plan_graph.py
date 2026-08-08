"""A small sequential queue for dependent FeatureRuns.

PlanGraph deliberately owns scheduling only.  A caller supplies the approved
plan references, the FeatureRun launcher, and (optionally) the final test
runner; selecting or configuring a backend remains outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence


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


@dataclass(frozen=True)
class FeatureRunOutcome:
    """The only terminal information PlanGraph requires from FeatureRun."""

    status: str
    candidate_commit: str | None = None
    evidence: object | None = None


@dataclass(frozen=True)
class PlanGraphResult:
    """Terminal queue result, including completed candidate commits."""

    status: str
    candidate_commit: str | None
    completed: Mapping[str, str]
    failed_run_id: str | None = None
    functionality_failure: str | None = None


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
        )


class PlanGraph:
    """Execute an already approved FeatureRun decomposition in dependency order."""

    def __init__(
        self,
        plan: PlanGraphPlan,
        launcher: FeatureRunLauncher,
        *,
        state_path: Path | None = None,
        functionality_tests: Sequence[str] = (),
        functionality_test_runner: FunctionalityTestRunner | None = None,
    ) -> None:
        self.plan = plan
        self.launcher = launcher
        self.state_path = state_path
        self.functionality_tests = (
            tuple(plan.functionality_tests) + tuple(functionality_tests)
        )
        self.functionality_test_runner = (
            functionality_test_runner or _run_functionality_test
        )

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
        completed = self._load_completed()
        ordered_runs = self._ordered_runs()
        self._validate_completed_dependencies(ordered_runs, completed)
        candidate_commit = self.plan.base_commit
        for run in ordered_runs:
            if run.id in completed:
                candidate_commit = completed[run.id]
                continue
            outcome = self.launcher(
                FeatureRunRequest(
                    run=run, base_commit=candidate_commit, plan=self.plan.plan
                )
            )
            if outcome.status != "succeeded":
                return PlanGraphResult(
                    status=outcome.status if outcome.status == "blocked" else "failed",
                    candidate_commit=None,
                    completed=dict(completed),
                    failed_run_id=run.id,
                )
            if not outcome.candidate_commit:
                raise PlanGraphError(
                    f"successful FeatureRun {run.id!r} did not provide a candidate commit"
                )
            completed[run.id] = outcome.candidate_commit
            candidate_commit = outcome.candidate_commit
            self._save_completed(completed)

        for command in self.functionality_tests:
            try:
                self.functionality_test_runner(command, candidate_commit)
            except Exception as exc:  # final test failures are terminal evidence
                return PlanGraphResult(
                    status="failed",
                    candidate_commit=candidate_commit,
                    completed=dict(completed),
                    functionality_failure=f"{command}: {exc}",
                )
        return PlanGraphResult("succeeded", candidate_commit, dict(completed))

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

    def _load_completed(self) -> dict[str, str]:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            completed = payload["completed"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PlanGraphError(f"invalid PlanGraph state: {exc}") from exc
        if not isinstance(completed, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in completed.items()
        ):
            raise PlanGraphError("invalid PlanGraph state: completed must map ids to commits")
        unknown = set(completed) - {run.id for run in self.plan.runs}
        if unknown:
            raise PlanGraphError(
                "PlanGraph state contains unknown completed runs: "
                + ", ".join(sorted(unknown))
            )
        return dict(completed)

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

    def _save_completed(self, completed: Mapping[str, str]) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"completed": dict(completed)}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)


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
    "SubprocessFeatureRunLauncher",
    "plan_from_mapping",
]
