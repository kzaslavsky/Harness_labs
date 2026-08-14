"""Deterministic dispatcher for schema-defined coordinator segments."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from harness_labs.core.agent_sessions import AgentSession, ModelRequest, ToolResult
from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_commands import CommandActor, CommandEnvelope
from harness_labs.core.controller_coordinator import CoordinatorLoop
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, RunContract
from harness_labs.core.controller_projection import ControllerQueries, project_run_view
from harness_labs.core.controller_run import restore_controller_checkpoint
from harness_labs.core.controller_scheduler import CapabilityScheduler, RoleProfile
from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment


@dataclass(frozen=True)
class CoordinatorLaunch:
    """Inputs supplied when constructing one fresh coordinator session."""

    schema_id: str
    schema_sha256: str
    segment_id: str
    phases: tuple[str, ...]
    attempt: int
    coordinator_profile: str
    instructions: str
    context: Mapping[str, Any]


CoordinatorSessionFactory = Callable[
    [CoordinatorLaunch, EvidenceCatalog],
    AgentSession,
]


@dataclass(frozen=True)
class CoordinatorDispatchResult:
    """Terminal dispatcher result and the launches it supervised."""

    result: TaskResult
    launches: tuple[CoordinatorLaunch, ...]


@dataclass(frozen=True)
class DispatchedControllerRunResult:
    """Finalized audit result for one schema-dispatched controller run."""

    dispatch: CoordinatorDispatchResult
    run_view: Mapping[str, Any]
    manifest: Mapping[str, Any]
    run_dir: Path


class CoordinatorDispatcher:
    """Spawn fresh coordinators while the kernel owns all durable state."""

    def __init__(
        self,
        kernel: ControllerKernel,
        evidence: EvidenceCatalog,
        scheduler: CapabilityScheduler,
        schema: CoordinatorDispatchSchema,
        session_factory: CoordinatorSessionFactory,
        *,
        actor_id: str = "dispatcher-1",
    ) -> None:
        schema.validate_phases(kernel.contract.phases)
        if not actor_id.strip():
            raise ValueError("dispatcher actor_id must be non-empty")
        self.kernel = kernel
        self.evidence = evidence
        self.scheduler = scheduler
        self.schema = schema
        self.session_factory = session_factory
        self.actor_id = actor_id
        self._launches: list[CoordinatorLaunch] = []

    def run(self) -> CoordinatorDispatchResult:
        self._register_schema()
        last_result: TaskResult | None = None
        while True:
            state = self.kernel.snapshot()
            if state["status"] != "running":
                return self._result(last_result)
            segment = self.schema.segment_for_phase(state["phase"])
            attempt = self._next_attempt(segment.id)
            if (
                segment.max_attempts is not None
                and attempt > segment.max_attempts
            ):
                self._block(
                    f"coordinator segment {segment.id} exhausted "
                    f"{segment.max_attempts} attempts"
                )
                return self._result(last_result)
            missing = self._missing_required_artifacts(segment, state)
            if missing:
                self._block(
                    f"coordinator segment {segment.id} missing required "
                    f"handoff artifacts: {', '.join(missing)}"
                )
                return self._result(last_result)

            launch = self._build_launch(segment, attempt, state)
            self._launches.append(launch)
            try:
                session = self.session_factory(launch, self.evidence)
            except InterruptedError:
                raise
            except Exception as exc:
                self._block(
                    f"coordinator segment {segment.id} session factory failed: "
                    f"{type(exc).__name__}"
                )
                last_result = TaskResult(
                    attempt_id=(
                        f"{self.kernel.contract.run_id}/"
                        f"coordinator:{segment.id}:attempt-{attempt}"
                    ),
                    status="failed",
                    payload={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                return self._result(last_result)
            tracked = _TrackedCoordinatorSession(
                session,
                on_open=lambda provider_session_id: self._session_started(
                    launch,
                    provider_session_id,
                ),
            )
            loop = CoordinatorLoop(
                self.kernel,
                ControllerQueries(self.kernel, self.evidence),
                self.scheduler,
                tracked,
                max_tool_calls=segment.max_tool_calls,
                actor_id=f"coordinator:{segment.id}:attempt-{attempt}",
                phase_scope=segment.phases,
                task_override=(
                    f"{self.kernel.contract.objective}\n\n"
                    f"Current coordinator segment: {segment.id}\n"
                    f"{segment.instructions}"
                ),
                initial_context=launch.context,
            )
            try:
                last_result = loop.run()
            except InterruptedError:
                raise
            except Exception as exc:
                last_result = TaskResult(
                    attempt_id=(
                        f"{self.kernel.contract.run_id}/"
                        f"coordinator:{segment.id}:attempt-{attempt}"
                    ),
                    status="failed",
                    payload={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            if tracked.audit_session_id is None:
                self._block(
                    f"coordinator segment {segment.id} failed before session start"
                )
                return self._result(last_result)

            outcome = self._classify_outcome(segment, last_result)
            if outcome == "boundary":
                missing_exit = self._missing_exit_artifacts(segment)
                if missing_exit:
                    self._block(
                        f"coordinator segment {segment.id} crossed its boundary "
                        "without required exit artifacts: "
                        + ", ".join(missing_exit)
                    )
                    outcome = "blocked"
            self._session_ended(
                tracked.audit_session_id,
                outcome,
                last_result,
            )
            if outcome in {"boundary", "recoverable_failure"}:
                continue
            return self._result(last_result)

    def _register_schema(self) -> None:
        existing = self.kernel.snapshot()["coordinator_dispatch"]["schema"]
        if existing is not None:
            if (
                existing.get("schema_id") != self.schema.schema_id
                or existing.get("sha256") != self.schema.sha256()
            ):
                raise ValueError(
                    "restored coordinator schema does not match the requested schema"
                )
            return
        value = self.schema.as_dict()
        receipt = self.kernel.handle(
            self._command(
                "coordinator.schema_register",
                {
                    "protocol": value["protocol"],
                    "schema_id": value["schema_id"],
                    "sha256": self.schema.sha256(),
                    "phases": list(self.kernel.contract.phases),
                    "segments": [
                        {
                            "id": segment.id,
                            "phases": list(segment.phases),
                            "max_attempts": segment.max_attempts,
                        }
                        for segment in self.schema.segments
                    ],
                },
                key=f"schema/{self.schema.sha256()}",
            )
        )
        if not receipt.accepted:
            raise ValueError(
                f"coordinator schema registration failed: {receipt.message}"
            )

    def _build_launch(
        self,
        segment: CoordinatorSegment,
        attempt: int,
        state: Mapping[str, Any],
    ) -> CoordinatorLaunch:
        selected = [
            copy.deepcopy(record)
            for _ref, record in sorted(state["artifacts"].items())
            if record["kind"] in segment.context_artifact_kinds
        ]
        history = [
            copy.deepcopy(item)
            for item in state["coordinator_dispatch"]["sessions"]
        ]
        context = {
            "protocol": "coordinator-segment-context/1",
            "schema_id": self.schema.schema_id,
            "schema_sha256": self.schema.sha256(),
            "segment": {
                "id": segment.id,
                "phases": list(segment.phases),
                "attempt": attempt,
                "instructions": segment.instructions,
                "coordinator_profile": segment.coordinator_profile,
            },
            "handoff_artifacts": selected,
            "prior_coordinator_sessions": history,
            "development_policy": (
                segment.development_policy.as_dict()
                if segment.development_policy is not None
                else None
            ),
            "required_exit_artifact_kinds": list(
                segment.exit_artifact_kinds
            ),
        }
        return CoordinatorLaunch(
            schema_id=self.schema.schema_id,
            schema_sha256=self.schema.sha256(),
            segment_id=segment.id,
            phases=segment.phases,
            attempt=attempt,
            coordinator_profile=segment.coordinator_profile,
            instructions=segment.instructions,
            context=context,
        )

    def _missing_required_artifacts(
        self,
        segment: CoordinatorSegment,
        state: Mapping[str, Any],
    ) -> tuple[str, ...]:
        kinds = {
            record["kind"] for record in state["artifacts"].values()
        }
        return tuple(
            kind
            for kind in segment.required_artifact_kinds
            if kind not in kinds
        )

    def _missing_exit_artifacts(
        self,
        segment: CoordinatorSegment,
    ) -> tuple[str, ...]:
        kinds = {
            record["kind"]
            for record in self.kernel.snapshot()["artifacts"].values()
        }
        return tuple(
            kind for kind in segment.exit_artifact_kinds if kind not in kinds
        )

    def _next_attempt(self, segment_id: str) -> int:
        sessions = self.kernel.snapshot()["coordinator_dispatch"]["sessions"]
        return (
            sum(1 for item in sessions if item["segment_id"] == segment_id) + 1
        )

    def _session_started(
        self,
        launch: CoordinatorLaunch,
        provider_session_id: str,
    ) -> str:
        audit_session_id = (
            f"{launch.segment_id}/attempt-{launch.attempt}:"
            f"{provider_session_id}"
        )
        receipt = self.kernel.handle(
            self._command(
                "coordinator.session_start",
                {
                    "session_id": audit_session_id,
                    "segment_id": launch.segment_id,
                    "attempt": launch.attempt,
                    "backend_id": launch.coordinator_profile,
                },
                key=(
                    f"session-start/{launch.segment_id}/"
                    f"attempt-{launch.attempt}"
                ),
            )
        )
        if not receipt.accepted:
            raise ValueError(
                f"coordinator session start failed: {receipt.message}"
            )
        return audit_session_id

    def _session_ended(
        self,
        session_id: str,
        outcome: str,
        result: TaskResult,
    ) -> None:
        reason = result.payload.get("error") or result.payload.get("text") or ""
        receipt = self.kernel.handle(
            self._command(
                "coordinator.session_end",
                {
                    "session_id": session_id,
                    "outcome": outcome,
                    "result_status": result.status,
                    "reason": str(reason),
                },
                key=f"session-end/{session_id}",
            )
        )
        if not receipt.accepted:
            raise ValueError(
                f"coordinator session end failed: {receipt.message}"
            )

    def recover_interrupted_session(self) -> None:
        """Close a checkpointed provider session whose process no longer exists."""

        active = self.kernel.snapshot()["coordinator_dispatch"]["active_session"]
        if not isinstance(active, Mapping):
            return
        receipt = self.kernel.handle(
            self._command(
                "coordinator.session_end",
                {
                    "session_id": active["session_id"],
                    "outcome": "interrupted",
                    "result_status": "interrupted",
                    "reason": (
                        "dispatcher process restarted; prior provider session "
                        "cannot be resumed safely"
                    ),
                },
                key=f"session-recovery/{active['session_id']}",
            )
        )
        if not receipt.accepted:
            raise ValueError(
                f"interrupted coordinator recovery failed: {receipt.message}"
            )

    def recover_interrupted_state(self) -> bool:
        """Reconcile safe checkpointed work before launching a new coordinator."""

        self.recover_interrupted_session()
        state = self.kernel.snapshot()
        running = sorted(
            task_id
            for task_id, task in state["tasks"].items()
            if task["status"] == "running"
        )
        if running:
            self._block(
                "dispatcher resumed with externally unproven running tasks: "
                + ", ".join(running)
            )
            return False
        ready = tuple(
            sorted(
                task_id
                for task_id, task in state["tasks"].items()
                if task["status"] == "ready"
            )
        )
        if ready:
            requested = max(
                int(state["tasks"][task_id].get("max_parallelism", 1))
                for task_id in ready
            )
            self.scheduler.dispatch(
                self.kernel,
                ready,
                max_parallelism=requested,
            )
        return True

    def _classify_outcome(
        self,
        segment: CoordinatorSegment,
        result: TaskResult,
    ) -> str:
        state = self.kernel.snapshot()
        if state["status"] == "succeeded":
            return "terminal"
        if state["status"] != "running":
            return "blocked"
        if state["phase"] not in segment.phases:
            return "boundary"
        return "recoverable_failure"

    def _block(self, reason: str) -> None:
        receipt = self.kernel.handle(
            self._command(
                "run.block_request",
                {"reason": reason},
                key=f"block/{self.kernel.revision}",
            )
        )
        if not receipt.accepted:
            raise ValueError(f"dispatcher could not block run: {receipt.message}")

    def _command(
        self,
        command_type: str,
        payload: Mapping[str, Any],
        *,
        key: str,
    ) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=(
                f"{self.kernel.contract.run_id}/{self.actor_id}/"
                f"{command_type}/{self.kernel.revision}"
            ),
            run_id=self.kernel.contract.run_id,
            type=command_type,
            actor=CommandActor(self.actor_id, "dispatcher"),
            expected_revision=self.kernel.revision,
            idempotency_key=(
                f"{self.kernel.contract.run_id}/{self.actor_id}/{key}"
            ),
            payload=dict(payload),
        )

    def _result(
        self,
        last_result: TaskResult | None,
    ) -> CoordinatorDispatchResult:
        view = project_run_view(self.kernel)
        status = (
            "succeeded"
            if view["status"] == "succeeded"
            else "blocked"
            if view["status"] == "blocked"
            else "failed"
        )
        result = TaskResult(
            attempt_id=f"{self.kernel.contract.run_id}/dispatcher",
            status=status,
            payload={
                "run_status": view["status"],
                "run_view": view,
                "launch_count": len(self._launches),
                "last_coordinator_result": (
                    {
                        "attempt_id": last_result.attempt_id,
                        "status": last_result.status,
                        "payload": dict(last_result.payload),
                    }
                    if last_result is not None
                    else None
                ),
            },
            evidence=last_result.evidence if last_result is not None else (),
        )
        return CoordinatorDispatchResult(result, tuple(self._launches))


class _TrackedCoordinatorSession:
    """Delegate provider calls while registering the provider session once."""

    def __init__(
        self,
        session: AgentSession,
        *,
        on_open: Callable[[str], str],
    ) -> None:
        self._session = session
        self._on_open = on_open
        self.audit_session_id: str | None = None

    @property
    def capabilities(self):
        return self._session.capabilities

    def open(self, request: ModelRequest) -> str:
        provider_session_id = self._session.open(request)
        try:
            self.audit_session_id = self._on_open(provider_session_id)
        except Exception:
            self._session.close(provider_session_id)
            raise
        return provider_session_id

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        return self._session.step(session_id, tool_result)

    def close(self, session_id: str) -> None:
        self._session.close(session_id)


def run_dispatched_controller(
    contract: RunContract,
    *,
    schema: CoordinatorDispatchSchema,
    session_factory: CoordinatorSessionFactory,
    profile_builder: Callable[[EvidenceCatalog], tuple[RoleProfile, ...]],
    run_dir: Path,
    evidence_classification: str = "production_lifecycle",
) -> DispatchedControllerRunResult:
    """Run and finalize one fresh schema-dispatched controller lifecycle."""

    audit = AuditJournal(
        run_dir,
        contract.run_id,
        actor=AuditActor("kernel", "controller_kernel"),
        evidence_classification=evidence_classification,
    )
    evidence = EvidenceCatalog(audit=audit)
    kernel = ControllerKernel(contract, evidence=evidence, audit=audit)
    scheduler = CapabilityScheduler(profile_builder(evidence))
    dispatch = CoordinatorDispatcher(
        kernel,
        evidence,
        scheduler,
        schema,
        session_factory,
    ).run()
    return _finalize_dispatched_run(audit, kernel, dispatch, run_dir)


def resume_dispatched_controller(
    contract: RunContract,
    *,
    schema: CoordinatorDispatchSchema,
    session_factory: CoordinatorSessionFactory,
    profile_builder: Callable[[EvidenceCatalog], tuple[RoleProfile, ...]],
    run_dir: Path,
) -> DispatchedControllerRunResult:
    """Resume a nonterminal schema-dispatched run from its verified checkpoint."""

    audit, evidence, kernel = restore_controller_checkpoint(contract, run_dir)
    scheduler = CapabilityScheduler(profile_builder(evidence))
    dispatcher = CoordinatorDispatcher(
        kernel,
        evidence,
        scheduler,
        schema,
        session_factory,
    )
    if dispatcher.recover_interrupted_state():
        dispatch = dispatcher.run()
    else:
        dispatch = dispatcher._result(None)
    return _finalize_dispatched_run(audit, kernel, dispatch, run_dir, resumed=True)


def _finalize_dispatched_run(
    audit: AuditJournal,
    kernel: ControllerKernel,
    dispatch: CoordinatorDispatchResult,
    run_dir: Path,
    *,
    resumed: bool = False,
) -> DispatchedControllerRunResult:
    view = project_run_view(kernel)
    terminal_status = (
        "succeeded"
        if dispatch.result.status == "succeeded" and view["status"] == "succeeded"
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
                "evidence": list(dispatch.result.evidence),
            },
            "coordinator_launches": [
                {
                    "schema_id": launch.schema_id,
                    "schema_sha256": launch.schema_sha256,
                    "segment_id": launch.segment_id,
                    "phases": list(launch.phases),
                    "attempt": launch.attempt,
                    "coordinator_profile": launch.coordinator_profile,
                }
                for launch in dispatch.launches
            ],
            "run_view": view,
            "state_digest": kernel.state_digest(),
            "resumed": resumed,
        },
        state={"controller": kernel.snapshot()},
    )
    return DispatchedControllerRunResult(dispatch, view, manifest, run_dir)


__all__ = [
    "CoordinatorDispatchResult",
    "CoordinatorDispatcher",
    "CoordinatorLaunch",
    "CoordinatorSessionFactory",
    "DispatchedControllerRunResult",
    "resume_dispatched_controller",
    "run_dispatched_controller",
]
