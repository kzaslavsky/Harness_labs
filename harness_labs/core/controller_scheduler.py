"""Capability-matched, repeated-role task scheduling for the controller."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness_labs.core.attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_kernel import ControllerKernel, KernelError
from harness_labs.core.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.core.controller_live import select_dirty_baseline_receipt
from harness_labs.core.controller_results import SemanticResultError, validate_semantic_result
from harness_labs.core.git_transaction import workspace_snapshot


class SchedulingError(RuntimeError):
    """Raised before launch when no isolated executor can satisfy a task."""


ExecutorFactory = Callable[[dict], Executor]


@dataclass(frozen=True)
class RoleProfile:
    """A reusable role/capability profile backed by an executor factory.

    ``allow_dirty_baseline`` is this profile's dirty-baseline eligibility as
    seen by the dispatch chokepoint (:meth:`CapabilityScheduler.dispatch`):
    when true, a writable dispatch under this profile may receive an
    auto-minted adoption grant (see ``_mint_dirty_baseline_grant``) if the
    workspace is dirty and a receipt exactly covers it. It is independent of
    any eligibility a hand-wired ``executor_factory`` enforces on its own --
    both must agree for a grant to actually take effect, since the factory's
    constructed executor still runs its own preflight.
    """

    profile_id: str
    role: str
    capabilities: frozenset[str]
    executor_factory: ExecutorFactory
    backend_id: str = "unspecified"
    details_schemas: frozenset[str] = frozenset()
    allow_dirty_baseline: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.role.strip():
            raise ValueError("role profile identity and role must be non-empty")
        if not self.backend_id.strip():
            raise ValueError("role profile backend_id must be non-empty")
        if not isinstance(self.capabilities, frozenset) or not all(
            isinstance(item, str) and item.strip() for item in self.capabilities
        ):
            raise ValueError("role profile capabilities must be names")
        if not callable(self.executor_factory):
            raise ValueError("role profile executor_factory must be callable")
        if not isinstance(self.details_schemas, frozenset) or not all(
            isinstance(item, str) and item.strip() for item in self.details_schemas
        ):
            raise ValueError("role profile details_schemas must be names")


def _mint_dirty_baseline_grant(
    profile: RoleProfile,
    executor: Executor,
    attempt: TaskAttempt,
    *,
    audit: AuditJournal | None = None,
) -> None:
    """Auto-mint a dirty-baseline adoption grant at the dispatch chokepoint.

    Every writable dispatch a scheduler runs -- fresh, retry, or superseding,
    coordinator-initiated or stage-machine-initiated -- passes through here
    right after ``executor_factory`` builds the executor, so a program is no
    longer required to hand-wire its own executor factory around
    ``agent_mixture``'s per-role grant computation to benefit from adoption.

    A grant is only ever minted when ALL hold: the role profile declares
    ``allow_dirty_baseline`` (eligibility is unchanged from what role
    profiles already govern); the constructed executor exposes a settable
    ``dirty_baseline_grant`` attribute that is still unset (an
    executor_factory that already computed and attached its own grant -- the
    agent_mixture case -- is left untouched, never overwritten); the
    executor declares a ``workspace-write`` sandbox; and the workspace is
    actually dirty. The receipt is selected by
    :func:`~harness_labs.core.controller_live.select_dirty_baseline_receipt`,
    which resolves each candidate through the shared
    ``verify_dirty_baseline_grant`` -- content coverage, not recency, and no
    dependence on catalog ordering; receipts are never unioned. When no
    single receipt covers the dirty state, nothing is minted and the
    executor's own preflight refuses exactly as it does today. Either
    outcome is journaled -- preferring the scheduler's own ``audit`` (see
    :class:`CapabilityScheduler`), falling back to the executor's ``audit``
    attribute when the scheduler carries none -- with the receipt ref or the
    uncovered/mismatched paths, so the decision is diagnosable from the
    journal regardless of whether the constructed executor happens to expose
    one itself.
    """

    if not profile.allow_dirty_baseline:
        return
    if not hasattr(executor, "dirty_baseline_grant"):
        return
    if getattr(executor, "dirty_baseline_grant", None) is not None:
        return
    if getattr(executor, "sandbox", None) != "workspace-write":
        return
    repository = getattr(executor, "repository", None)
    evidence = getattr(executor, "evidence", None)
    if repository is None or evidence is None:
        return
    snapshot = workspace_snapshot(Path(repository))
    dirty_paths = list(snapshot["changed_paths"])
    if not dirty_paths:
        return
    journal = audit if audit is not None else getattr(executor, "audit", None)
    actor = AuditActor(profile.profile_id, "capability_scheduler")
    receipt_ref, failure = select_dirty_baseline_receipt(
        evidence=evidence, dirty_paths=dirty_paths, dirty_files=snapshot["files"]
    )
    if receipt_ref is None:
        if journal is not None and failure is not None:
            journal.append(
                "dirty_baseline_adoption_grant_supplied",
                status="refused",
                payload={
                    "dirty_paths": sorted(dirty_paths),
                    "uncovered_paths": list(failure.uncovered_paths),
                    "mismatched_paths": list(failure.mismatched_paths),
                },
                actor=actor,
                attempt_id=attempt.attempt_id,
            )
        return
    grant: dict[str, Any] = {"receipt_ref": receipt_ref}
    executor.dirty_baseline_grant = grant
    if journal is not None:
        journal.append(
            "dirty_baseline_adoption_grant_supplied",
            status="granted",
            payload={"receipt_ref": receipt_ref, "dirty_paths": sorted(dirty_paths)},
            actor=actor,
            attempt_id=attempt.attempt_id,
        )


@dataclass(frozen=True)
class ScheduledOutcome:
    task_id: str
    profile_id: str
    backend_id: str
    result: TaskResult


class CapabilityScheduler:
    """Allocate a fresh executor per attempt and run bounded parallel batches."""

    def __init__(
        self,
        profiles: tuple[RoleProfile, ...],
        *,
        runner: AttemptRunner | None = None,
        audit: AuditJournal | None = None,
    ) -> None:
        if not profiles:
            raise ValueError("scheduler requires at least one role profile")
        profile_ids = [profile.profile_id for profile in profiles]
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("scheduler role profile ids must be unique")
        self._profiles = profiles
        self._runner = runner or AttemptRunner()
        self._audit = audit
        self._mutex = threading.Lock()
        self._active = 0
        self._maximum_active = 0

    @property
    def maximum_active(self) -> int:
        with self._mutex:
            return self._maximum_active

    @property
    def profile_view(self) -> tuple[dict, ...]:
        return tuple(
            {
                "profile_id": profile.profile_id,
                "role": profile.role,
                "backend_id": profile.backend_id,
                "capabilities": sorted(profile.capabilities),
                "details_schemas": sorted(profile.details_schemas),
                "allow_dirty_baseline": profile.allow_dirty_baseline,
            }
            for profile in sorted(self._profiles, key=lambda item: item.profile_id)
        )

    def dispatch(
        self,
        kernel: ControllerKernel,
        task_ids: tuple[str, ...],
        *,
        max_parallelism: int,
    ) -> tuple[ScheduledOutcome, ...]:
        if not task_ids:
            raise SchedulingError("dispatch requires at least one task")
        if max_parallelism < 1:
            raise SchedulingError("max_parallelism must be positive")
        if (
            kernel.contract.limits.max_parallelism is not None
            and max_parallelism > kernel.contract.limits.max_parallelism
        ):
            raise SchedulingError("dispatch exceeds run max_parallelism")

        prepared = []
        executor_ids: set[int] = set()
        for task_id in task_ids:
            task = kernel.task(task_id)
            profile = self._select_profile(task)
            executor = profile.executor_factory(task)
            if not callable(getattr(executor, "execute", None)):
                raise SchedulingError(
                    f"executor factory returned an invalid executor: {profile.profile_id}"
                )
            identity = id(executor)
            if identity in executor_ids:
                raise SchedulingError(
                    "executor factory reused an instance within a parallel batch"
                )
            executor_ids.add(identity)
            attempt = TaskAttempt(
                attempt_id=task["attempt_id"],
                task_ref=f"task:{task_id}",
                context_ref=f"context:{task_id}",
                grant_ref=f"profile:{profile.profile_id}",
                parent_attempt_id=(
                    f"{task['parent_task_id']}/attempt-1"
                    if task["parent_task_id"] is not None
                    else None
                ),
                context=task["context"],
            )
            _mint_dirty_baseline_grant(profile, executor, attempt, audit=self._audit)
            prepared.append((task, profile, executor, attempt))

        kernel.mark_tasks_running(task_ids)
        worker_count = min(max_parallelism, len(prepared))
        ordered: list[ScheduledOutcome | None] = [None] * len(prepared)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="controller-task",
        ) as pool:
            futures = {
                pool.submit(
                    self._execute_one,
                    task,
                    profile,
                    executor,
                    attempt,
                ): index
                for index, (task, profile, executor, attempt) in enumerate(prepared)
            }
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()

        outcomes = tuple(
            outcome for outcome in ordered if isinstance(outcome, ScheduledOutcome)
        )
        if len(outcomes) != len(prepared):
            raise KernelError("scheduler lost a terminal task outcome")
        for outcome in outcomes:
            if outcome.result.status != "succeeded":
                continue
            task = kernel.task(outcome.task_id)
            try:
                semantic = validate_semantic_result(
                    outcome.result,
                    expected_details_schema=task["details_schema"],
                )
            except SemanticResultError:
                continue
            for index, delegation in enumerate(semantic.delegation_requests, start=1):
                command = CommandEnvelope(
                    command_id=(
                        f"{outcome.task_id}/delegation-{index}/"
                        f"revision-{kernel.revision}"
                    ),
                    run_id=kernel.contract.run_id,
                    type="task.dispatch",
                    actor=CommandActor(
                        outcome.task_id,
                        "worker",
                        task["parent_task_id"],
                    ),
                    expected_revision=kernel.revision,
                    idempotency_key=(
                        f"{kernel.contract.run_id}/{outcome.task_id}/"
                        f"delegation-{index}"
                    ),
                    provenance=CommandProvenance(),
                    payload=dict(delegation),
                )
                receipt = kernel.handle(command)
                if not receipt.accepted:
                    raise SchedulingError(
                        f"worker delegation rejected: {receipt.message}"
                    )
                child_ids = tuple(
                    ref.removeprefix("task:")
                    for ref in receipt.effect_refs
                    if ref.startswith("task:")
                )
                self.dispatch(
                    kernel,
                    child_ids,
                    max_parallelism=int(delegation.get("max_parallelism", 1)),
                )
        kernel.record_task_results(
            (outcome.task_id, outcome.result) for outcome in outcomes
        )
        return outcomes

    def _select_profile(self, task: dict) -> RoleProfile:
        required = frozenset(task["required_capabilities"])
        candidates = [
            profile
            for profile in self._profiles
            if profile.role == task["role"]
            and required.issubset(profile.capabilities)
            and (
                not profile.details_schemas
                or task["details_schema"] in profile.details_schemas
            )
        ]
        if not candidates:
            raise SchedulingError(
                f"no profile satisfies role/capabilities for task {task['id']}"
            )
        return sorted(candidates, key=lambda item: item.profile_id)[0]

    def validate_task_profiles(self, tasks: list[dict]) -> None:
        """Fail before kernel registration when any task has no runnable profile."""

        for task in tasks:
            self._select_profile(task)

    def _execute_one(
        self,
        task: dict,
        profile: RoleProfile,
        executor: Executor,
        attempt: TaskAttempt,
    ) -> ScheduledOutcome:
        with self._mutex:
            self._active += 1
            self._maximum_active = max(self._maximum_active, self._active)
        try:
            try:
                result = self._runner.run(attempt, executor)
            except Exception as exc:
                result = TaskResult(
                    attempt_id=attempt.attempt_id,
                    status="failed",
                    payload={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            return ScheduledOutcome(
                task_id=task["id"],
                profile_id=profile.profile_id,
                backend_id=profile.backend_id,
                result=result,
            )
        finally:
            close = getattr(executor, "close", None)
            if callable(close):
                close()
            with self._mutex:
                self._active -= 1
