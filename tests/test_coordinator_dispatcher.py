"""Schema-driven coordinator dispatcher tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.agent_sessions import (
    BackendCapabilities,
    BackendFailure,
    ModelRequest,
    ToolResult,
)
from harness_labs.core.coordinator_dispatcher import (
    COORDINATOR_RECOVERY_HARD_CAP,
    CoordinatorDispatcher,
    CoordinatorLaunch,
    resume_dispatched_controller,
    run_dispatched_controller,
)
from harness_labs.core.audit import AuditJournal
from harness_labs.core.attempts import TaskResult
from harness_labs.core.controller_commands import CommandActor, CommandEnvelope
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, RunContract
from harness_labs.core.controller_projection import (
    COORDINATOR_SESSION_HISTORY_LIMIT,
    bound_coordinator_sessions,
    project_run_view,
)
from harness_labs.core.controller_scheduler import CapabilityScheduler, RoleProfile
from harness_labs.core.controller_results import semantic_payload
from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema

from tests.controller_scenario_fixtures import ScriptedCoordinatorSession


class _UnusedExecutor:
    def execute(self, attempt):  # pragma: no cover - dispatcher tests spawn no tasks
        raise AssertionError("unused executor must not run")


class _FailingSession:
    capabilities = BackendCapabilities(True, True, True, True, True)

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.request: ModelRequest | None = None
        self.closed = False

    def open(self, request: ModelRequest) -> str:
        self.request = request
        return self.session_id

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        return BackendFailure("transient coordinator transport failure")

    def close(self, session_id: str) -> None:
        self.closed = True


class _RepeatingTransientFailureSession:
    """Every attempt fails the same infrastructure-shaped way, forever."""

    capabilities = BackendCapabilities(True, True, True, True, True)

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.closed = False

    def open(self, request: ModelRequest) -> str:
        return self.session_id

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        return BackendFailure("connection reset by peer")

    def close(self, session_id: str) -> None:
        self.closed = True


class _CapabilityAbsentSession:
    """First-turn final output naming a disabled capability -- no tool calls."""

    capabilities = BackendCapabilities(True, True, True, True, True)

    BLOCKED_MESSAGE = (
        "Blocked: the typed controller tool host is disabled "
        "(`code-mode host is disabled`), so I cannot inspect the frozen "
        "handoff, dispatch the implementation worker, record evidence, or "
        "request audited completion."
    )

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.closed = False

    def open(self, request: ModelRequest) -> str:
        return self.session_id

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        from harness_labs.core.agent_sessions import FinalOutput

        return FinalOutput(self.BLOCKED_MESSAGE, evidence=())

    def close(self, session_id: str) -> None:
        self.closed = True


class _InterruptingSession(_FailingSession):
    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        raise KeyboardInterrupt("simulated dispatcher process death")


def _scheduler() -> CapabilityScheduler:
    return CapabilityScheduler(_profiles())


def _profiles() -> tuple[RoleProfile, ...]:
    return (
        RoleProfile(
            "unused",
            "unused",
            frozenset(),
            lambda task: _UnusedExecutor(),
        ),
    )


def _schema(value: dict) -> CoordinatorDispatchSchema:
    return CoordinatorDispatchSchema.from_mapping(
        {
            "protocol": "coordinator-dispatch-schema/1",
            "schema_id": value.pop("schema_id", "test-schema/1"),
            "segments": value.pop("segments"),
            **value,
        }
    )


def _segment(
    segment_id: str,
    phases: list[str],
    *,
    max_attempts: int = 1,
    required: list[str] | None = None,
    exit_artifacts: list[str] | None = None,
) -> dict:
    return {
        "id": segment_id,
        "phases": phases,
        "instructions": f"Own {segment_id}.",
        "coordinator_profile": f"{segment_id}-profile",
        "context": {
            "artifact_kinds": required or [],
            "required_artifact_kinds": required or [],
        },
        "max_attempts": max_attempts,
        "max_tool_calls": 16,
        "exit_artifact_kinds": exit_artifacts or [],
    }


class CoordinatorDispatcherTests(unittest.TestCase):
    def test_missing_exit_artifact_blocks_after_phase_boundary(self) -> None:
        contract = RunContract(
            run_id="exit-artifact-gate",
            objective="Do not cross without the plan.",
            phases=("plan", "build"),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        schema = _schema(
            {
                "segments": [
                    _segment(
                        "plan",
                        ["plan"],
                        exit_artifacts=["engineering-plan"],
                    ),
                    _segment("build", ["build"]),
                ]
            }
        )
        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            lambda launch, catalog: ScriptedCoordinatorSession(
                [("phase_advance_request", {"target": "build"})],
                final="claimed complete",
            ),
        ).run()
        self.assertEqual(result.result.status, "blocked")
        self.assertEqual(kernel.snapshot()["phase"], "plan")
        self.assertNotEqual(kernel.snapshot()["status"], "succeeded")

    def test_segment_limits_are_unbounded_when_omitted(self) -> None:
        segment = _schema(
            {"segments": [{
                "id": "active",
                "phases": ["active"],
                "instructions": "Finish.",
                "coordinator_profile": "default",
                "context": {
                    "artifact_kinds": [],
                    "required_artifact_kinds": [],
                },
            }]}
        ).segments[0]
        self.assertIsNone(segment.max_attempts)
        self.assertIsNone(segment.max_tool_calls)

    def test_resume_closes_interrupted_session_and_continues_same_run(self) -> None:
        contract = RunContract(
            run_id="dispatcher-resume",
            objective="Complete after process death.",
            phases=("active",),
        )
        schema = _schema(
            {"segments": [_segment("active", ["active"], max_attempts=2)]}
        )
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            with self.assertRaises(KeyboardInterrupt):
                run_dispatched_controller(
                    contract,
                    schema=schema,
                    session_factory=lambda launch, evidence: _InterruptingSession(
                        "dead-provider-session"
                    ),
                    profile_builder=lambda evidence: _profiles(),
                    run_dir=run_dir,
                    evidence_classification="component",
                )

            resumed = resume_dispatched_controller(
                contract,
                schema=schema,
                session_factory=lambda launch, evidence: ScriptedCoordinatorSession(
                    [("run_complete_request", {})],
                    final="Recovered.",
                ),
                profile_builder=lambda evidence: _profiles(),
                run_dir=run_dir,
            )

            self.assertEqual(resumed.run_view["status"], "succeeded")
            sessions = resumed.run_view["coordinator_dispatch"]["sessions"]
            self.assertEqual(
                [item["outcome"] for item in sessions],
                ["interrupted", "terminal"],
            )
            self.assertEqual(sessions[1]["attempt"], 2)
            self.assertEqual(resumed.manifest["status"], "succeeded")
            AuditJournal.verify(run_dir)

    def _kernel_with_dead_session_running_task(
        self,
        schema: CoordinatorDispatchSchema,
    ) -> tuple[ControllerKernel, EvidenceCatalog]:
        """Checkpoint shape left by a coordinator process that died mid-task."""

        contract = RunContract(
            run_id="orphan-recovery",
            objective="Recover in-flight work after coordinator death.",
            phases=("active",),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)

        def dispatcher_command(kind: str, payload: dict, key: str):
            return CommandEnvelope(
                command_id=key,
                run_id=contract.run_id,
                type=kind,
                actor=CommandActor("dispatcher-1", "dispatcher"),
                expected_revision=kernel.revision,
                idempotency_key=key,
                payload=payload,
            )

        value = schema.as_dict()
        registered = kernel.handle(
            dispatcher_command(
                "coordinator.schema_register",
                {
                    "protocol": value["protocol"],
                    "schema_id": value["schema_id"],
                    "sha256": schema.sha256(),
                    "phases": ["active"],
                    "segments": [
                        {
                            "id": segment.id,
                            "phases": list(segment.phases),
                            "max_attempts": segment.max_attempts,
                        }
                        for segment in schema.segments
                    ],
                },
                "register-schema",
            )
        )
        assert registered.accepted, registered.message
        started = kernel.handle(
            dispatcher_command(
                "coordinator.session_start",
                {
                    "session_id": "active/attempt-1:dead-provider",
                    "segment_id": "active",
                    "attempt": 1,
                },
                "start-session",
            )
        )
        assert started.accepted, started.message
        dispatched = kernel.handle(
            CommandEnvelope(
                command_id="dispatch-implement",
                run_id=contract.run_id,
                type="task.dispatch",
                actor=CommandActor("coordinator-1", "run_coordinator"),
                expected_revision=kernel.revision,
                idempotency_key="dispatch-implement",
                payload={
                    "tasks": [
                        {
                            "id": "implement",
                            "role": "worker",
                            "objective": "Implement the change",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                        }
                    ]
                },
            )
        )
        assert dispatched.accepted, dispatched.message
        kernel.mark_tasks_running(("implement",))
        return kernel, evidence

    def test_recovery_orphans_dead_sessions_running_task_for_supersede(
        self,
    ) -> None:
        schema = _schema(
            {"segments": [_segment("active", ["active"], max_attempts=2)]}
        )
        kernel, evidence = self._kernel_with_dead_session_running_task(schema)
        dispatcher = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            lambda launch, catalog: self.fail("recovery must not spawn"),
        )

        self.assertTrue(dispatcher.recover_interrupted_state())

        state = kernel.snapshot()
        self.assertNotEqual(state["status"], "blocked")
        self.assertIsNone(state["coordinator_dispatch"]["active_session"])
        task = state["tasks"]["implement"]
        self.assertEqual(task["status"], "failed")
        self.assertTrue(task["orphaned"])
        self.assertEqual(
            state["coordinator_dispatch"]["sessions"][-1]["orphaned_task_ids"],
            ["implement"],
        )

        supersede = kernel.handle(
            CommandEnvelope(
                command_id="dispatch-implement-v2",
                run_id=kernel.contract.run_id,
                type="task.dispatch",
                actor=CommandActor("coordinator-2", "run_coordinator"),
                expected_revision=kernel.revision,
                idempotency_key="dispatch-implement-v2",
                payload={
                    "tasks": [
                        {
                            "id": "implement-v2",
                            "role": "worker",
                            "objective": "Implement the change",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                            "supersedes_task_id": "implement",
                        }
                    ]
                },
            )
        )
        self.assertTrue(supersede.accepted, supersede.message)

    def test_recovery_ingests_completed_unowned_worker_result(self) -> None:
        schema = _schema(
            {"segments": [_segment("active", ["active"], max_attempts=2)]}
        )
        kernel, evidence = self._kernel_with_dead_session_running_task(schema)
        recovered_tasks = []

        def recover(task) -> TaskResult | None:
            recovered_tasks.append(task["id"])
            return TaskResult(
                attempt_id=task["attempt_id"],
                status="succeeded",
                payload=semantic_payload(
                    summary="Reconstructed from the captured worker stdout.",
                    details_schema="work/1",
                    details={},
                ),
            )

        dispatcher = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            lambda launch, catalog: self.fail("recovery must not spawn"),
            result_recovery=recover,
        )

        self.assertTrue(dispatcher.recover_interrupted_state())

        self.assertEqual(recovered_tasks, ["implement"])
        state = kernel.snapshot()
        task = state["tasks"]["implement"]
        self.assertEqual(task["status"], "succeeded")
        self.assertFalse(task.get("orphaned", False))
        self.assertEqual(
            state["coordinator_dispatch"]["sessions"][-1]["orphaned_task_ids"],
            [],
        )

    def test_spawns_one_fresh_coordinator_per_schema_segment(self) -> None:
        contract = RunContract(
            run_id="segmented",
            objective="Advance plan, build, and review.",
            phases=("plan", "build", "review"),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        schema = _schema(
            {
                "segments": [
                    _segment("plan", ["plan"]),
                    _segment("build", ["build"]),
                    _segment("review", ["review"]),
                ]
            }
        )
        sessions: list[ScriptedCoordinatorSession] = []
        launches: list[CoordinatorLaunch] = []

        def factory(launch, _evidence):
            launches.append(launch)
            if launch.segment_id == "plan":
                calls = [("phase_advance_request", {"target": "build"})]
            elif launch.segment_id == "build":
                calls = [("phase_advance_request", {"target": "review"})]
            else:
                calls = [("run_complete_request", {})]
            session = ScriptedCoordinatorSession(
                calls,
                final=f"{launch.segment_id} complete",
            )
            sessions.append(session)
            return session

        dispatched = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            factory,
        ).run()

        self.assertEqual(dispatched.result.status, "succeeded")
        self.assertEqual(
            [launch.segment_id for launch in dispatched.launches],
            ["plan", "build", "review"],
        )
        self.assertEqual(launches, list(dispatched.launches))
        self.assertEqual(len({id(session) for session in sessions}), 3)
        self.assertTrue(all(session.closed for session in sessions))
        self.assertEqual(
            [
                item["outcome"]
                for item in kernel.snapshot()["coordinator_dispatch"]["sessions"]
            ],
            ["boundary", "boundary", "terminal"],
        )
        self.assertIsNone(
            kernel.snapshot()["coordinator_dispatch"]["active_session"]
        )
        self.assertEqual(
            sessions[1].request.context["coordinator_segment"]["segment"]["id"],
            "build",
        )

    def test_schema_may_group_arbitrary_phases_in_one_coordinator(self) -> None:
        contract = RunContract(
            run_id="generic",
            objective="Research, synthesize, and report.",
            phases=("research", "synthesize", "report"),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        schema = _schema(
            {
                "schema_id": "generic-analysis/1",
                "segments": [
                    _segment("analysis", ["research", "synthesize"]),
                    _segment("report", ["report"]),
                ],
            }
        )
        sessions = []

        def factory(launch, _evidence):
            calls = (
                [
                    ("phase_advance_request", {"target": "synthesize"}),
                    ("phase_advance_request", {"target": "report"}),
                ]
                if launch.segment_id == "analysis"
                else [("run_complete_request", {})]
            )
            session = ScriptedCoordinatorSession(
                calls,
                final=f"{launch.segment_id} complete",
            )
            sessions.append(session)
            return session

        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            factory,
        ).run()

        self.assertEqual(result.result.status, "succeeded")
        self.assertEqual(len(sessions), 2)
        self.assertEqual(
            [launch.phases for launch in result.launches],
            [("research", "synthesize"), ("report",)],
        )

    def test_retries_failed_coordinator_with_a_fresh_session(self) -> None:
        contract = RunContract(
            run_id="retry",
            objective="Complete after one transport failure.",
            phases=("active",),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        schema = _schema(
            {"segments": [_segment("active", ["active"], max_attempts=2)]}
        )
        sessions = []

        def factory(launch, _evidence):
            if launch.attempt == 1:
                session = _FailingSession("failed-provider-session")
            else:
                session = ScriptedCoordinatorSession(
                    [("run_complete_request", {})],
                    final="recovered",
                )
            sessions.append(session)
            return session

        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            factory,
        ).run()

        self.assertEqual(result.result.status, "succeeded")
        self.assertEqual([launch.attempt for launch in result.launches], [1, 2])
        self.assertTrue(all(session.closed for session in sessions))
        self.assertEqual(
            [
                item["outcome"]
                for item in kernel.snapshot()["coordinator_dispatch"]["sessions"]
            ],
            ["recoverable_failure", "terminal"],
        )

    def test_missing_required_handoff_blocks_before_spawn(self) -> None:
        contract = RunContract(
            run_id="missing-handoff",
            objective="Require a plan.",
            phases=("build",),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        schema = _schema(
            {
                "segments": [
                    _segment(
                        "build",
                        ["build"],
                        required=["implementation-plan"],
                    )
                ]
            }
        )
        calls = []

        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            lambda launch, catalog: calls.append(launch),
        ).run()

        self.assertEqual(result.result.status, "blocked")
        self.assertEqual(calls, [])
        self.assertIn(
            "implementation-plan",
            kernel.snapshot()["blocker"],
        )

    def test_context_passes_only_selected_artifact_descriptors(self) -> None:
        contract = RunContract(
            run_id="selected-context",
            objective="Use the selected handoff only.",
            phases=("active",),
        )
        evidence = EvidenceCatalog()
        selected = evidence.add(
            kind="implementation-plan",
            content="selected plan body",
            media_type="text/markdown",
            producer_task_id="planner",
        )
        unrelated = evidence.add(
            kind="unrelated-private-note",
            content="must not be supplied",
            media_type="text/plain",
            producer_task_id="planner",
        )
        kernel = ControllerKernel(contract, evidence=evidence)
        registered = kernel.handle(
            CommandEnvelope(
                command_id="register-plan",
                run_id=contract.run_id,
                type="task.dispatch",
                actor=CommandActor("seed", "run_coordinator"),
                expected_revision=kernel.revision,
                idempotency_key="register-plan",
                payload={
                    "tasks": [
                        {
                            "id": "planner",
                            "role": "planner",
                            "objective": "Prepare handoff artifacts.",
                            "details_schema": "plan/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
            )
        )
        self.assertTrue(registered.accepted)
        kernel.mark_tasks_running(("planner",))
        kernel.record_task_results(
            (
                (
                    "planner",
                    TaskResult(
                        attempt_id="planner/attempt-1",
                        status="succeeded",
                        payload=semantic_payload(
                            summary="Prepared context.",
                            details_schema="plan/1",
                            details={},
                            artifacts=(
                                selected.as_dict(),
                                unrelated.as_dict(),
                            ),
                        ),
                    ),
                ),
            )
        )
        segment = _segment(
            "active",
            ["active"],
            required=["implementation-plan"],
        )
        schema = _schema({"segments": [segment]})
        sessions = []

        def factory(launch, _evidence):
            session = ScriptedCoordinatorSession(
                [("run_complete_request", {})],
                final="complete",
            )
            sessions.append(session)
            return session

        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            factory,
        ).run()

        self.assertEqual(result.result.status, "succeeded")
        handoff = sessions[0].request.context["coordinator_segment"][
            "handoff_artifacts"
        ]
        self.assertEqual([item["ref"] for item in handoff], [selected.ref])
        self.assertNotIn("selected plan body", str(handoff))
        self.assertNotIn("must not be supplied", str(sessions[0].request.context))

    def test_implement_v13_shaped_schema_covers_feature_phases(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "schemas"
            / "examples"
            / "implement-v13-coordinators.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        schema = CoordinatorDispatchSchema.from_mapping(value)
        contract_schema = json.loads(
            (
                Path(__file__).parents[1]
                / "schemas"
                / "coordinator-dispatch.schema.json"
            ).read_text(encoding="utf-8")
        )

        schema.validate_phases(
            (
                "orient",
                "plan",
                "implement",
                "verify",
                "review",
                "integrate",
                "report",
            )
        )
        self.assertEqual(
            [segment.id for segment in schema.segments],
            ["plan-refute", "build", "verify", "review", "integrate-report"],
        )
        self.assertEqual(
            schema.as_dict()["protocol"],
            contract_schema["properties"]["protocol"]["const"],
        )
        self.assertTrue(
            set(contract_schema["required"]).issubset(schema.as_dict())
        )

    def test_production_entrypoint_finalizes_dispatch_audit(self) -> None:
        contract = RunContract(
            run_id="dispatched-entrypoint",
            objective="Complete through a dispatched coordinator.",
            phases=("active",),
        )
        schema = _schema(
            {"segments": [_segment("active", ["active"])]}
        )

        def factory(launch, _evidence):
            return ScriptedCoordinatorSession(
                [("run_complete_request", {})],
                final="complete",
            )

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / contract.run_id
            result = run_dispatched_controller(
                contract,
                schema=schema,
                session_factory=factory,
                profile_builder=lambda evidence: (
                    RoleProfile(
                        "unused",
                        "unused",
                        frozenset(),
                        lambda task: _UnusedExecutor(),
                    ),
                ),
                run_dir=run_dir,
                evidence_classification="component",
            )

            self.assertEqual(result.dispatch.result.status, "succeeded")
            self.assertEqual(result.run_view["status"], "succeeded")
            self.assertTrue((run_dir / "manifest.json").is_file())
            verification = AuditJournal.verify(run_dir)
            self.assertGreater(verification["event_count"], 5)

    def test_schema_rejects_gaps_reordering_and_duplicate_segments(self) -> None:
        schema = _schema(
            {
                "segments": [
                    _segment("first", ["plan"]),
                    _segment("second", ["review"]),
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            schema.validate_phases(("plan", "build", "review"))

        with self.assertRaisesRegex(ValueError, "ids must be unique"):
            _schema(
                {
                    "segments": [
                        _segment("same", ["plan"]),
                        _segment("same", ["review"]),
                    ]
                }
            )
        invalid_handoff = _segment("handoff", ["plan"])
        invalid_handoff["context"] = {
            "artifact_kinds": [],
            "required_artifact_kinds": ["plan"],
        }
        with self.assertRaisesRegex(ValueError, "also be included"):
            _schema({"segments": [invalid_handoff]})


    # --- Retry-storm guard regression tests ------------------------------
    #
    # Ground truth for these three tests is the flow-editor-uc2-authoring
    # attempt-7-UC-2F incident: a coordinator blocked on turn one because
    # "the typed controller tool host is disabled", the deterministic
    # dispatcher classified that as recoverable_failure and relaunched it
    # 967 times over ~35 minutes, and the un-deduplicated, unbounded session
    # history serialized into every relaunch's context eventually exceeded
    # the backend's 1,048,576-char input cap.

    def test_capability_absence_reason_blocks_without_retry(self) -> None:
        contract = RunContract(
            run_id="capability-absent",
            objective="Advance the frozen handoff.",
            phases=("active",),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        # A generous per-segment attempt budget: if the dispatcher merely
        # counted attempts instead of classifying the reason, it would keep
        # retrying well past the first failure.
        schema = _schema(
            {"segments": [_segment("active", ["active"], max_attempts=50)]}
        )
        launches: list = []

        def factory(launch, _evidence):
            launches.append(launch)
            return _CapabilityAbsentSession(f"session-{len(launches)}")

        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            factory,
        ).run()

        self.assertEqual(result.result.status, "blocked")
        self.assertEqual(len(launches), 1, "must not relaunch after a capability-absence failure")
        sessions = kernel.snapshot()["coordinator_dispatch"]["sessions"]
        self.assertEqual([item["outcome"] for item in sessions], ["blocked"])
        blocker = kernel.snapshot()["blocker"]
        self.assertIn("missing or disabled capability", blocker)
        self.assertIn("code-mode host is disabled", blocker)

    def test_hard_recovery_cap_stops_a_repeating_transient_failure(self) -> None:
        contract = RunContract(
            run_id="retry-storm-guard",
            objective="Never converge.",
            phases=("active",),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        # Unbounded at the schema level: only the module-level hard cap may
        # stop this loop. A misclassification (or an unrecognized failure
        # shape) must not be able to retry forever.
        schema = _schema(
            {"segments": [_segment("active", ["active"], max_attempts=None)]}
        )
        launches: list = []

        def factory(launch, _evidence):
            launches.append(launch)
            return _RepeatingTransientFailureSession(f"session-{len(launches)}")

        result = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            factory,
        ).run()

        self.assertEqual(result.result.status, "blocked")
        self.assertEqual(len(launches), COORDINATOR_RECOVERY_HARD_CAP)
        sessions = kernel.snapshot()["coordinator_dispatch"]["sessions"]
        self.assertEqual(len(sessions), COORDINATOR_RECOVERY_HARD_CAP)
        self.assertEqual(
            {item["outcome"] for item in sessions},
            {"recoverable_failure"},
        )
        blocker = kernel.snapshot()["blocker"]
        self.assertIn("hard coordinator recovery cap", blocker)
        self.assertIn(str(COORDINATOR_RECOVERY_HARD_CAP), blocker)

    def test_bound_coordinator_sessions_keeps_recent_plus_summary(self) -> None:
        sessions = [
            {"session_id": f"s{i}", "outcome": "recoverable_failure" if i % 2 else "boundary"}
            for i in range(COORDINATOR_SESSION_HISTORY_LIMIT + 12)
        ]
        bounded = bound_coordinator_sessions(sessions)

        self.assertEqual(len(bounded), COORDINATOR_SESSION_HISTORY_LIMIT + 1)
        summary, recent = bounded[0], bounded[1:]
        self.assertTrue(summary.get("summary"))
        self.assertEqual(summary["total_session_count"], len(sessions))
        self.assertEqual(summary["elided_session_count"], 12)
        self.assertEqual(sum(summary["elided_outcome_tally"].values()), 12)
        self.assertEqual(recent, sessions[-COORDINATOR_SESSION_HISTORY_LIMIT:])
        # Bounded serialization stays small regardless of how many sessions
        # accumulated -- this is what kept the storm's context from growing
        # without limit as the retry count climbed into the hundreds.
        self.assertLess(len(json.dumps(bounded)), len(json.dumps(sessions)))

        # Below the limit, nothing is elided or summarized.
        small = sessions[:5]
        self.assertEqual(bound_coordinator_sessions(small), small)

    def test_session_history_bounded_end_to_end_and_not_duplicated(self) -> None:
        contract = RunContract(
            run_id="session-history-bounded",
            objective="Accumulate a long coordinator history.",
            phases=("active",),
        )
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(contract, evidence=evidence)
        total_prior_sessions = COORDINATOR_SESSION_HISTORY_LIMIT + 8
        dispatcher_actor = CommandActor("dispatcher-1", "dispatcher")
        schema = _schema(
            {
                "schema_id": "history-schema/1",
                "segments": [
                    _segment(
                        "active",
                        ["active"],
                        max_attempts=total_prior_sessions + 5,
                    )
                ],
            }
        )

        def dispatcher_command(kind: str, payload: dict, *, command_id: str):
            return CommandEnvelope(
                command_id=command_id,
                run_id=contract.run_id,
                type=kind,
                actor=dispatcher_actor,
                expected_revision=kernel.revision,
                idempotency_key=command_id,
                payload=payload,
            )

        registered = kernel.handle(
            dispatcher_command(
                "coordinator.schema_register",
                {
                    "protocol": "coordinator-dispatch-schema/1",
                    "schema_id": schema.schema_id,
                    "sha256": schema.sha256(),
                    "phases": ["active"],
                    "segments": [{"id": "active", "phases": ["active"]}],
                },
                command_id="register-schema",
            )
        )
        self.assertTrue(registered.accepted, registered.message)
        # Seed a long prior-session ledger directly through the kernel --
        # bypassing the dispatcher's own hard cap, which caps a single
        # dispatcher run, not the durable ledger a resumed run inherits.
        for i in range(1, total_prior_sessions + 1):
            session_id = f"seed-{i}"
            started = kernel.handle(
                dispatcher_command(
                    "coordinator.session_start",
                    {"session_id": session_id, "segment_id": "active", "attempt": i},
                    command_id=f"start-{session_id}",
                )
            )
            self.assertTrue(started.accepted, started.message)
            ended = kernel.handle(
                dispatcher_command(
                    "coordinator.session_end",
                    {
                        "session_id": session_id,
                        "outcome": "recoverable_failure",
                        "result_status": "failed",
                        "reason": "seeded prior attempt " + session_id,
                    },
                    command_id=f"end-{session_id}",
                )
            )
            self.assertTrue(ended.accepted, ended.message)

        raw_sessions = kernel.snapshot()["coordinator_dispatch"]["sessions"]
        self.assertEqual(len(raw_sessions), total_prior_sessions)

        view = project_run_view(kernel)
        view_sessions = view["coordinator_dispatch"]["sessions"]
        self.assertEqual(len(view_sessions), COORDINATOR_SESSION_HISTORY_LIMIT + 1)
        self.assertTrue(view_sessions[0].get("summary"))
        self.assertEqual(view_sessions[0]["elided_session_count"], 8)

        # Build the next launch's segment context directly against this long
        # history (the dispatcher's own hard cap -- tested separately above
        # -- would otherwise refuse to relaunch a 28th attempt of one
        # segment, which is exactly the correct behavior but would get in
        # the way of inspecting the context-building step in isolation).
        dispatcher = CoordinatorDispatcher(
            kernel,
            evidence,
            _scheduler(),
            schema,
            lambda launch, _evidence: self.fail("session factory must not run"),
        )
        segment = schema.segment_for_phase("active")
        state = kernel.snapshot()
        launch = dispatcher._build_launch(segment, total_prior_sessions + 1, state)

        # The segment-local context must not carry its own copy of the
        # session ledger -- only a pointer to the one authoritative,
        # bounded copy that CoordinatorLoop attaches as run_view.
        self.assertNotIn("prior_coordinator_sessions", launch.context)
        self.assertEqual(
            launch.context.get("prior_coordinator_sessions_location"),
            "run_view.coordinator_dispatch.sessions",
        )
        # And that one authoritative copy, wherever it is composed into the
        # coordinator's actual model context, is the same bounded view
        # already verified above -- never the raw, unbounded ledger.
        composed_run_view = project_run_view(kernel)
        run_view_sessions = composed_run_view["coordinator_dispatch"]["sessions"]
        self.assertEqual(len(run_view_sessions), COORDINATOR_SESSION_HISTORY_LIMIT + 1)
        self.assertTrue(run_view_sessions[0].get("summary"))


if __name__ == "__main__":
    unittest.main()
