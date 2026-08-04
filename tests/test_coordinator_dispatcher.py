"""Schema-driven coordinator dispatcher tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs.agent_sessions import (
    BackendCapabilities,
    BackendFailure,
    ModelRequest,
    ToolResult,
)
from harness_labs.coordinator_dispatcher import (
    CoordinatorDispatcher,
    CoordinatorLaunch,
    resume_dispatched_controller,
    run_dispatched_controller,
)
from harness_labs.audit import AuditJournal
from harness_labs.attempts import TaskResult
from harness_labs.controller_commands import CommandActor, CommandEnvelope
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import ControllerKernel, RunContract
from harness_labs.controller_scheduler import CapabilityScheduler, RoleProfile
from harness_labs.controller_results import semantic_payload
from harness_labs.coordinator_schema import CoordinatorDispatchSchema

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
    }


class CoordinatorDispatcherTests(unittest.TestCase):
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
            ["plan-refute", "build", "verify-review", "integrate-report"],
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


if __name__ == "__main__":
    unittest.main()
