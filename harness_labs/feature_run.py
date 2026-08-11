"""Production FeatureRun entrypoint with controller-owned Git transactions."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic_ns
from typing import Callable, Mapping, Protocol

from .agent_sessions import AgentSession
from .attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
from .controller_evidence import EvidenceCatalog
from .controller_kernel import ControllerKernel, RunContract
from .controller_projection import project_run_view
from .controller_scheduler import CapabilityScheduler, RoleProfile
from .coordinator_dispatcher import (
    CoordinatorDispatchResult,
    CoordinatorDispatcher,
    CoordinatorLaunch,
)
from .coordinator_schema import CoordinatorDispatchSchema
from .git_transaction import (
    GitTransactionError,
    GitWorktreeTransaction,
    paths_outside_scope,
    workspace_snapshot,
)
from .review_fix import (
    ReviewFixExecutorFactory,
    ReviewFixLoop,
    ReviewFixPolicy,
    ReviewFixResult,
)


FeatureContractFactory = Callable[
    [Path, Mapping[str, object]],
    RunContract,
]
FeatureSessionFactory = Callable[
    [Path, CoordinatorLaunch, EvidenceCatalog],
    AgentSession,
]
FeatureProfileBuilder = Callable[
    [Path, EvidenceCatalog],
    tuple[RoleProfile, ...],
]


_NORMAL_FEATURE_PHASES = (
    "orient",
    "plan",
    "implement",
    "verify",
    "review",
    "integrate",
    "report",
)


@dataclass(frozen=True)
class FeatureRunHandoffArtifact:
    """One controller-owned artifact available before coordinator dispatch."""

    kind: str
    content: object
    media_type: str = "application/json"
    producer_task_id: str = "plan-graph"

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.kind, self.media_type, self.producer_task_id)
        ):
            raise ValueError("handoff artifact metadata must be non-empty")


@dataclass(frozen=True)
class PlanGraphFeatureRunBinding:
    """Approved PlanGraph handoff replacing only FeatureRun orient and plan."""

    plan_graph_id: str
    plan_node_id: str
    objective: str
    acceptance_criteria: tuple[Mapping[str, object], ...]
    approved_plan: Mapping[str, object]
    source_binding_report: Mapping[str, object]
    build_briefing: Mapping[str, object]

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.plan_graph_id, self.plan_node_id, self.objective)
        ):
            raise ValueError("PlanGraph FeatureRun binding identity must be non-empty")
        if not self.acceptance_criteria:
            raise ValueError("PlanGraph FeatureRun binding requires acceptance criteria")
        for name in ("approved_plan", "source_binding_report", "build_briefing"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or not value:
                raise ValueError(f"PlanGraph FeatureRun binding {name} must be non-empty")

    def handoff_artifacts(self) -> tuple[FeatureRunHandoffArtifact, ...]:
        def envelope(content: Mapping[str, object]) -> dict[str, object]:
            return {
                "protocol": "plan-graph-feature-handoff/1",
                "plan_graph_id": self.plan_graph_id,
                "plan_node_id": self.plan_node_id,
                "objective": self.objective,
                "acceptance_criteria": [dict(item) for item in self.acceptance_criteria],
                "content": dict(content),
            }

        return (
            FeatureRunHandoffArtifact(
                "engineering-plan", envelope(self.approved_plan)
            ),
            FeatureRunHandoffArtifact(
                "source-binding-report", envelope(self.source_binding_report)
            ),
            FeatureRunHandoffArtifact(
                "build-briefing", envelope(self.build_briefing)
            ),
        )


class VerificationRepairExecutorFactory(Protocol):
    """Construct the fixer for one failed deterministic verification attempt."""

    def __call__(self, attempt: TaskAttempt) -> Executor:
        """Return an executor that repairs the current candidate worktree."""


@dataclass(frozen=True)
class DeterministicVerificationResult:
    """Controller-observed command attempts and bounded recovery outcome."""

    status: str
    reason: str
    command_attempts: tuple[Mapping[str, object], ...]
    repair_attempts: int

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "command_attempts": [dict(item) for item in self.command_attempts],
            "repair_attempts": self.repair_attempts,
        }


@dataclass(frozen=True)
class FeatureRunResult:
    """Terminal semantic, Git, and audit outcome for one FeatureRun."""

    status: str
    contract: RunContract
    dispatch: CoordinatorDispatchResult
    run_view: Mapping[str, object]
    git_receipts: tuple[Mapping[str, object], ...]
    manifest: Mapping[str, object]
    run_dir: Path
    worktree_path: Path
    review_fix: ReviewFixResult | None = None
    verification: DeterministicVerificationResult | None = None


def run_feature_worktree(
    *,
    base_repository: Path,
    base_branch: str,
    feature_branch: str,
    worktree_path: Path,
    run_dir: Path,
    contract_factory: FeatureContractFactory,
    schema: CoordinatorDispatchSchema,
    session_factory: FeatureSessionFactory,
    profile_builder: FeatureProfileBuilder,
    allowed_paths: tuple[str, ...],
    commit_message: str,
    merge: bool = False,
    review_fix_executor_factory: ReviewFixExecutorFactory | None = None,
    review_fix_policy: ReviewFixPolicy = ReviewFixPolicy(enabled=False),
    review_finding_obligations: tuple[Mapping[str, object], ...] = (),
    review_finding_transfer_targets: Mapping[str, str] | None = None,
    review_origin_node_id: str = "",
    review_inherited_ledger_frozen: bool = False,
    verification_argv: tuple[str, ...] = (),
    verification_repair_executor_factory: (
        VerificationRepairExecutorFactory | None
    ) = None,
    verification_repair_limit: int = 1,
    verification_timeout_seconds: float | None = 1200,
    evidence_classification: str = "production_lifecycle",
    initial_evidence: tuple[FeatureRunHandoffArtifact, ...] = (),
) -> FeatureRunResult:
    """Create, execute, commit, and optionally merge one isolated FeatureRun."""

    if review_fix_policy.enabled and review_fix_executor_factory is None:
        raise ValueError("enabled review_fix_policy requires an executor factory")
    if verification_argv:
        if any("verify" in segment.phases for segment in schema.segments):
            raise ValueError(
                "controller-owned verification cannot be combined with a "
                "coordinator verify phase"
            )
        if any(not isinstance(value, str) or not value for value in verification_argv):
            raise ValueError("verification_argv must contain non-empty strings")
        if verification_repair_executor_factory is None:
            raise ValueError(
                "deterministic verification requires a repair executor factory"
            )
        if verification_repair_limit < 1:
            raise ValueError("verification_repair_limit must be positive")
        if (
            verification_timeout_seconds is not None
            and verification_timeout_seconds <= 0
        ):
            raise ValueError("verification_timeout_seconds must be positive or None")
    elif verification_repair_executor_factory is not None:
        raise ValueError("verification repair requires verification_argv")
    handoff_kinds = [artifact.kind for artifact in initial_evidence]
    if len(set(handoff_kinds)) != len(handoff_kinds):
        raise ValueError("handoff artifact kinds must be unique")
    transaction = GitWorktreeTransaction.create(
        base_repository=base_repository,
        base_branch=base_branch,
        feature_branch=feature_branch,
        worktree_path=worktree_path,
    )
    creation = transaction.creation_receipt()
    contract = contract_factory(transaction.worktree_path, creation)
    _validate_repository_binding(contract, creation)
    audit = AuditJournal(
        run_dir,
        contract.run_id,
        actor=AuditActor("kernel", "controller_kernel"),
        evidence_classification=evidence_classification,
    )
    evidence = EvidenceCatalog(audit=audit)
    handoff_records = []
    for handoff in initial_evidence:
        record = evidence.add(
            kind=handoff.kind,
            content=handoff.content,
            media_type=handoff.media_type,
            producer_task_id=handoff.producer_task_id,
        )
        audit.append(
            "feature_run_handoff_bound",
            status="succeeded",
            payload={"kind": handoff.kind, "evidence_ref": record.ref},
            actor=AuditActor("plan-graph", "parent_controller"),
        )
        handoff_records.append(record.as_dict())
    creation_artifact = evidence.add(
        kind="git-worktree-receipt",
        content=creation,
        media_type="application/json",
        producer_task_id="integration-owner",
    )
    audit.append(
        "git_worktree_created",
        status="succeeded",
        payload={**creation, "evidence_ref": creation_artifact.ref},
        actor=AuditActor("integration-owner", "integration_owner"),
    )
    kernel = ControllerKernel(
        contract,
        evidence=evidence,
        audit=audit,
        initial_artifacts=handoff_records,
    )
    scheduler = CapabilityScheduler(
        profile_builder(transaction.worktree_path, evidence)
    )
    dispatch = CoordinatorDispatcher(
        kernel,
        evidence,
        scheduler,
        schema,
        lambda launch, catalog: session_factory(
            transaction.worktree_path,
            launch,
            catalog,
        ),
    ).run()
    receipts: list[Mapping[str, object]] = [creation]
    status = dispatch.result.status
    verification_result = None
    review_fix_result = None
    pre_review_workspace = None
    if status == "succeeded" and project_run_view(kernel)["status"] == "succeeded":
        if verification_argv:
            assert verification_repair_executor_factory is not None
            verification_result = _verify_with_recovery(
                run_id=contract.run_id,
                objective=contract.objective,
                acceptance_criteria=contract.criteria,
                worktree_path=transaction.worktree_path,
                allowed_paths=allowed_paths,
                argv=verification_argv,
                repair_executor_factory=verification_repair_executor_factory,
                repair_limit=verification_repair_limit,
                timeout_seconds=verification_timeout_seconds,
                evidence=evidence,
                audit=audit,
            )
            status = verification_result.status
    if status == "succeeded" and project_run_view(kernel)["status"] == "succeeded":
        if review_fix_policy.enabled:
            assert review_fix_executor_factory is not None
            snapshot = workspace_snapshot(transaction.worktree_path)
            pre_review_workspace = snapshot
            review_fix_result = ReviewFixLoop(
                run_id=contract.run_id,
                objective=contract.objective,
                acceptance_criteria=contract.criteria,
                allowed_paths=allowed_paths,
                changed_paths=tuple(snapshot["changed_paths"]),
                executor_factory=review_fix_executor_factory,
                evidence=evidence,
                audit=audit,
                policy=review_fix_policy,
                inherited_findings=review_finding_obligations,
                finding_transfer_targets=review_finding_transfer_targets or {},
                origin_node_id=review_origin_node_id,
                inherited_ledger_frozen=review_inherited_ledger_frozen,
            ).run()
            status = review_fix_result.status
    if (
        status == "succeeded"
        and project_run_view(kernel)["status"] == "succeeded"
        and verification_argv
        and pre_review_workspace is not None
        and workspace_snapshot(transaction.worktree_path) != pre_review_workspace
    ):
        assert verification_repair_executor_factory is not None
        post_review = _verify_with_recovery(
            run_id=contract.run_id,
            objective=contract.objective,
            acceptance_criteria=contract.criteria,
            worktree_path=transaction.worktree_path,
            allowed_paths=allowed_paths,
            argv=verification_argv,
            repair_executor_factory=verification_repair_executor_factory,
            repair_limit=verification_repair_limit,
            timeout_seconds=verification_timeout_seconds,
            evidence=evidence,
            audit=audit,
            stage="post_review_repair",
        )
        verification_result = _combine_verification_results(
            verification_result,
            post_review,
        )
        status = post_review.status
    if status == "succeeded" and project_run_view(kernel)["status"] == "succeeded":
        try:
            commit = transaction.commit_candidate(
                allowed_paths=allowed_paths,
                message=commit_message,
            )
            receipts.append(commit)
            _record_git_receipt(audit, evidence, commit)
            integration = transaction.integrate(merge=merge)
            receipts.append(integration)
            _record_git_receipt(audit, evidence, integration)
        except GitTransactionError as exc:
            status = "failed"
            dispatch = CoordinatorDispatchResult(
                TaskResult(
                    attempt_id=f"{contract.run_id}/integration-owner",
                    status="failed",
                    payload={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                ),
                dispatch.launches,
            )
            audit.append(
                "git_transaction_failed",
                status="failed",
                payload={"error": str(exc)},
                actor=AuditActor("integration-owner", "integration_owner"),
            )

    view = project_run_view(kernel)
    terminal_status = (
        "succeeded"
        if status == "succeeded" and view["status"] == "succeeded"
        else "blocked"
        if status == "blocked" or view["status"] == "blocked"
        else "failed"
    )
    review_fix_payload = (
        review_fix_result.as_dict() if review_fix_result is not None else None
    )
    verification_payload = (
        verification_result.as_dict() if verification_result is not None else None
    )
    manifest = audit.finalize(
        terminal_status,
        result={
            "dispatcher_result": {
                "attempt_id": dispatch.result.attempt_id,
                "status": dispatch.result.status,
                "payload": dict(dispatch.result.payload),
            },
            "run_view": view,
            "state_digest": kernel.state_digest(),
            "git_receipts": list(receipts),
            "verification": verification_payload,
            "review_fix": review_fix_payload,
        },
        state={
            "controller": kernel.snapshot(),
            "verification": verification_payload,
            "review_fix": review_fix_payload,
        },
    )
    return FeatureRunResult(
        terminal_status,
        contract,
        dispatch,
        view,
        tuple(receipts),
        manifest,
        run_dir,
        transaction.worktree_path,
        review_fix_result,
        verification_result,
    )


def run_plan_graph_feature_worktree(
    *,
    binding: PlanGraphFeatureRunBinding,
    schema: CoordinatorDispatchSchema,
    contract_factory: FeatureContractFactory,
    review_fix_policy: ReviewFixPolicy,
    **feature_run_options: object,
) -> FeatureRunResult:
    """Run normal FeatureRun machinery with only orient and plan pre-satisfied.

    This is a launch profile over :func:`run_feature_worktree`, not a second
    lifecycle engine.  The approved PlanGraph packet becomes the normal planning
    handoff, while verification, ledger-backed review/fix, Git custody, recovery,
    and reporting continue through the existing FeatureRun implementation.
    """

    phases = tuple(phase for segment in schema.segments for phase in segment.phases)
    if phases != _NORMAL_FEATURE_PHASES:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires the normal seven-phase schema"
        )
    if schema.segments[0].phases != ("orient", "plan"):
        raise ValueError(
            "PlanGraph-bound FeatureRun may omit only the orient-plan segment"
        )
    required_review_guards = (
        review_fix_policy.enabled,
        review_fix_policy.ledger_enabled,
        review_fix_policy.scope_expansion_guard_enabled,
        review_fix_policy.targeted_verification_enabled,
        review_fix_policy.regression_review_enabled,
        review_fix_policy.cycle_limit_enabled,
    )
    if not all(required_review_guards):
        raise ValueError(
            "PlanGraph-bound FeatureRun requires the normal ledger-backed review guards"
        )
    reserved = {"schema", "contract_factory", "review_fix_policy", "initial_evidence"}
    overlap = sorted(reserved.intersection(feature_run_options))
    if overlap:
        raise ValueError(
            "PlanGraph-bound FeatureRun options override controller-owned values: "
            + ", ".join(overlap)
        )

    implementation_segments = tuple(
        segment for segment in schema.segments if segment.phases == ("implement",)
    )
    if len(implementation_segments) != 1:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires one normal implement segment"
        )
    verification_argv = feature_run_options.get("verification_argv")
    if not isinstance(verification_argv, tuple) or not verification_argv:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires controller-owned verification"
        )
    if feature_run_options.get("verification_repair_executor_factory") is None:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires normal verification recovery"
        )

    bound_implementation = replace(
        implementation_segments[0],
        instructions=(
            "Implement or repair the accepted PlanGraph node from its frozen "
            "handoff and produce the implementation summary. Dispatch only "
            "implementation or implementation-repair tasks. Do not dispatch a "
            "verification-only task or require a worker to run the declared "
            "deterministic verification command; the parent FeatureRun owns and "
            "runs that gate immediately after this segment completes."
        ),
    )
    bound_schema = CoordinatorDispatchSchema(
        schema_id=f"{schema.schema_id}/plan-graph-bound",
        segments=(bound_implementation,),
    )
    bound_phases = ("implement",)

    def bound_contract_factory(
        worktree: Path, creation: Mapping[str, object]
    ) -> RunContract:
        contract = contract_factory(worktree, creation)
        if contract.phases != _NORMAL_FEATURE_PHASES:
            raise ValueError(
                "PlanGraph-bound FeatureRun contract must start as a normal FeatureRun"
            )
        if contract.objective != binding.objective:
            raise ValueError("PlanGraph binding objective does not match FeatureRun")
        if contract.criteria != binding.acceptance_criteria:
            raise ValueError(
                "PlanGraph binding acceptance criteria do not match FeatureRun"
            )
        return replace(contract, phases=bound_phases)

    return run_feature_worktree(
        schema=bound_schema,
        contract_factory=bound_contract_factory,
        review_fix_policy=review_fix_policy,
        initial_evidence=binding.handoff_artifacts(),
        **feature_run_options,
    )


def _verify_with_recovery(
    *,
    run_id: str,
    objective: str,
    acceptance_criteria: tuple[Mapping[str, object], ...],
    worktree_path: Path,
    allowed_paths: tuple[str, ...],
    argv: tuple[str, ...],
    repair_executor_factory: VerificationRepairExecutorFactory,
    repair_limit: int,
    timeout_seconds: float | None,
    evidence: EvidenceCatalog,
    audit: AuditJournal,
    stage: str = "post_implementation",
) -> DeterministicVerificationResult:
    command_attempts: list[Mapping[str, object]] = []
    runner = AttemptRunner()
    actor = AuditActor("verification-owner", "verification_owner")

    for ordinal in range(1, repair_limit + 2):
        command = _run_verification_command(
            worktree_path,
            argv,
            timeout_seconds,
            ordinal,
            stage,
        )
        artifact = evidence.add(
            kind="deterministic-verification-output",
            content=command,
            media_type="application/json",
            producer_task_id="verification-owner",
        )
        recorded = {**command, "evidence_ref": artifact.ref}
        command_attempts.append(recorded)
        audit.append(
            "deterministic_verification_completed",
            status="succeeded" if command["exit_code"] == 0 else "failed",
            payload=recorded,
            actor=actor,
        )
        if command["exit_code"] == 0:
            return DeterministicVerificationResult(
                "succeeded",
                "declared verification command passed",
                tuple(command_attempts),
                ordinal - 1,
            )
        if ordinal > repair_limit:
            return DeterministicVerificationResult(
                "blocked",
                "declared verification command still fails after repair budget",
                tuple(command_attempts),
                repair_limit,
            )

        attempt = TaskAttempt(
            attempt_id=f"{run_id}/verification-repair/{ordinal}",
            task_ref="verification-repair",
            context_ref=artifact.ref,
            grant_ref="verification-repair-write-grant",
            context=json.dumps(
                {
                    "objective": objective,
                    "acceptance_criteria": list(acceptance_criteria),
                    "allowed_paths": list(allowed_paths),
                    "failed_verification": recorded,
                    "repair_attempt": ordinal,
                    "repair_limit": repair_limit,
                },
                sort_keys=True,
            ),
        )
        repair = runner.run(attempt, repair_executor_factory(attempt))
        repaired_workspace = workspace_snapshot(worktree_path)
        prior_workspace = command["workspace"]
        assert isinstance(prior_workspace, Mapping)
        outside_scope = paths_outside_scope(
            repaired_workspace["changed_paths"],
            allowed_paths,
        )
        identity_changed = any(
            repaired_workspace[key] != prior_workspace[key]
            for key in ("head", "branch")
        )
        repair_status = (
            "failed"
            if outside_scope or identity_changed
            else repair.status
        )
        audit.append(
            "deterministic_verification_repair_completed",
            status=repair_status,
            payload={
                "repair_attempt": ordinal,
                "result": dict(repair.payload),
                "evidence_refs": list(repair.evidence),
                "failed_command_evidence_ref": artifact.ref,
                "workspace": repaired_workspace,
                "outside_allowed_paths": list(outside_scope),
                "repository_identity_changed": identity_changed,
            },
            actor=actor,
            attempt_id=attempt.attempt_id,
        )
        if repair_status != "succeeded":
            return DeterministicVerificationResult(
                "blocked",
                (
                    "verification repair escaped its grant"
                    if outside_scope or identity_changed
                    else f"verification repair {repair.status}"
                ),
                tuple(command_attempts),
                ordinal,
            )

    raise AssertionError("verification loop did not terminate")


def _run_verification_command(
    worktree_path: Path,
    argv: tuple[str, ...],
    timeout_seconds: float | None,
    ordinal: int,
    stage: str,
) -> dict[str, object]:
    started = monotonic_ns()
    try:
        completed = subprocess.run(
            argv,
            cwd=worktree_path,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        timed_out = True
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
        timed_out = False
    return {
        "stage": stage,
        "attempt": ordinal,
        "argv": list(argv),
        "cwd": str(worktree_path),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "duration_ms": (monotonic_ns() - started) // 1_000_000,
        "workspace": workspace_snapshot(worktree_path),
    }


def _combine_verification_results(
    first: DeterministicVerificationResult | None,
    second: DeterministicVerificationResult,
) -> DeterministicVerificationResult:
    if first is None:
        return second
    return DeterministicVerificationResult(
        status=second.status,
        reason=second.reason,
        command_attempts=first.command_attempts + second.command_attempts,
        repair_attempts=first.repair_attempts + second.repair_attempts,
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else value
    )


def _validate_repository_binding(
    contract: RunContract,
    creation: Mapping[str, object],
) -> None:
    expected = {
        "path": creation["worktree_path"],
        "branch": creation["feature_branch"],
        "base_branch": creation["base_branch"],
        "base_commit": creation["base_commit"],
    }
    mismatches = [
        name
        for name, value in expected.items()
        if contract.repository.get(name) != value
    ]
    if mismatches:
        raise ValueError(
            "FeatureRun contract does not bind its Git transaction: "
            + ", ".join(mismatches)
        )


def _record_git_receipt(
    audit: AuditJournal,
    evidence: EvidenceCatalog,
    receipt: Mapping[str, object],
) -> None:
    artifact = evidence.add(
        kind=f"git-{receipt['operation']}-receipt",
        content=receipt,
        media_type="application/json",
        producer_task_id="integration-owner",
    )
    audit.append(
        f"git_{receipt['operation']}_completed",
        status="succeeded",
        payload={**receipt, "evidence_ref": artifact.ref},
        actor=AuditActor("integration-owner", "integration_owner"),
    )


__all__ = [
    "DeterministicVerificationResult",
    "FeatureContractFactory",
    "FeatureProfileBuilder",
    "FeatureRunResult",
    "FeatureSessionFactory",
    "ReviewFixPolicy",
    "VerificationRepairExecutorFactory",
    "run_feature_worktree",
]
