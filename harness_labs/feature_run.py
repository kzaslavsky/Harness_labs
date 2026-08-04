"""Production FeatureRun entrypoint with controller-owned Git transactions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .agent_sessions import AgentSession
from .attempts import TaskResult
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
    evidence_classification: str = "production_lifecycle",
) -> FeatureRunResult:
    """Create, execute, commit, and optionally merge one isolated FeatureRun."""

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
    kernel = ControllerKernel(contract, evidence=evidence, audit=audit)
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
        if view["status"] == "blocked"
        else "failed"
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
        },
        state={"controller": kernel.snapshot()},
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
    "FeatureContractFactory",
    "FeatureProfileBuilder",
    "FeatureRunResult",
    "FeatureSessionFactory",
    "run_feature_worktree",
]
