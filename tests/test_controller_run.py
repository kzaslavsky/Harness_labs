"""Production-shaped entrypoint and durable audit tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness_labs.attempts import TaskResult
from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.controller_coordinator import CoordinatorLoop
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import ControllerKernel, RunContract
from harness_labs.controller_projection import ControllerQueries
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_run import resume_controller
from harness_labs.controller_scheduler import CapabilityScheduler, RoleProfile

from tests.controller_scenario_fixtures import (
    FixtureExecutor,
    ScriptedCoordinatorSession,
    task,
)


class ControllerRunTests(unittest.TestCase):
    def test_fixture_cli_runs_real_controller_and_writes_manifest(self) -> None:
        fixture = {
            "contract": {
                "run_id": "fixture-run",
                "objective": "Produce a fixture report.",
                "phases": ["active"],
                "criteria": [
                    {
                        "id": "reported",
                        "statement": "The report exists.",
                        "source": "operator",
                    }
                ],
                "terminal_artifact_kinds": ["report"],
            },
            "artifacts": [
                {
                    "alias": "report",
                    "producer_task_id": "writer",
                    "kind": "report",
                    "content": "Fixture report",
                    "media_type": "text/plain",
                }
            ],
            "profiles": [
                {
                    "profile_id": "writer",
                    "role": "writer",
                    "capabilities": [],
                }
            ],
            "task_results": {
                "writer": {
                    "status": "succeeded",
                    "payload": {
                        "protocol": "semantic-task-result/1",
                        "summary": "Wrote the fixture report.",
                        "claims": [],
                        "findings": [],
                        "artifacts": [{"$artifact_descriptor": "report"}],
                        "criterion_coverage": [
                            {
                                "criterion_id": "reported",
                                "status": "satisfied",
                                "evidence_refs": ["$artifact:report"],
                            }
                        ],
                        "recommendations": [],
                        "unresolved_questions": [],
                        "delegation_requests": [],
                        "details_schema": "report-details/1",
                        "details": {},
                    },
                }
            },
            "coordinator_calls": [
                {
                    "name": "task_dispatch",
                    "arguments": {
                        "tasks": [
                            {
                                "id": "writer",
                                "role": "writer",
                                "objective": "Write",
                                "details_schema": "report-details/1",
                                "required_capabilities": [],
                                "acceptance_criteria": ["reported"],
                                "dependencies": [],
                            }
                        ],
                        "max_parallelism": 1,
                    },
                },
                {"name": "run_complete_request", "arguments": {}},
            ],
            "final": "Fixture complete.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_path = root / "fixture.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            run_dir = root / "run"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "harness_labs.controller_run",
                    "--fixture",
                    str(fixture_path),
                    "--run-dir",
                    str(run_dir),
                ],
                cwd=Path(__file__).parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output["status"], "succeeded")
            verification = AuditJournal.verify(run_dir)
            self.assertGreater(verification["event_count"], 1)
            checkpoint = json.loads(
                (run_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                checkpoint["state"]["controller"]["status"],
                "succeeded",
            )
            self.assertTrue((run_dir / "manifest.json").is_file())

    def test_resume_uses_checkpoint_and_does_not_repeat_completed_task(self) -> None:
        contract = RunContract(
            run_id="resume-run",
            objective="Produce a resumable report.",
            phases=("active",),
            criteria=(
                {
                    "id": "reported",
                    "statement": "The report exists.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("report",),
        )
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "resume-run"
            audit = AuditJournal(
                run_dir,
                contract.run_id,
                actor=AuditActor("kernel", "controller_kernel"),
                evidence_classification="component",
            )
            evidence = EvidenceCatalog(audit=audit)
            report = evidence.add(
                kind="report",
                content="resumable",
                media_type="text/plain",
                producer_task_id="writer",
            )
            kernel = ControllerKernel(contract, evidence=evidence, audit=audit)

            def build(task_value, attempt):
                calls.append(attempt.attempt_id)
                return TaskResult(
                    attempt_id=attempt.attempt_id,
                    status="succeeded",
                    payload=semantic_payload(
                        summary="Wrote once.",
                        details_schema=task_value["details_schema"],
                        details={},
                        artifacts=(report.as_dict(),),
                        criterion_coverage=(
                            {
                                "criterion_id": "reported",
                                "status": "satisfied",
                                "evidence_refs": [report.ref],
                            },
                        ),
                    ),
                )

            dispatch_arguments = {
                "idempotency_key": "writer-dispatch",
                "tasks": [
                    task(
                        "writer",
                        "writer",
                        "Write",
                        "report-details/1",
                        criteria=("reported",),
                    )
                ],
                "max_parallelism": 1,
            }
            first_session = ScriptedCoordinatorSession(
                [("task_dispatch", dispatch_arguments)],
                final="Process interrupted after verified work.",
            )
            first_scheduler = CapabilityScheduler(
                (
                    RoleProfile(
                        "writer",
                        "writer",
                        frozenset(),
                        lambda task_value: FixtureExecutor(task_value, build),
                    ),
                )
            )
            first = CoordinatorLoop(
                kernel,
                ControllerQueries(kernel, evidence),
                first_scheduler,
                first_session,
            ).run()
            self.assertEqual(first.status, "blocked")
            self.assertEqual(calls, ["writer/attempt-1"])

            def resumed_session_builder(restored_evidence):
                return ScriptedCoordinatorSession(
                    [
                        ("task_dispatch", dispatch_arguments),
                        ("run_complete_request", {}),
                    ],
                    final="Resumed without duplicate work.",
                )

            def resumed_profiles(restored_evidence):
                return (
                    RoleProfile(
                        "writer",
                        "writer",
                        frozenset(),
                        lambda task_value: FixtureExecutor(task_value, build),
                    ),
                )

            resumed = resume_controller(
                contract,
                session_builder=resumed_session_builder,
                profile_builder=resumed_profiles,
                run_dir=run_dir,
            )

            self.assertEqual(resumed.result.status, "succeeded")
            self.assertEqual(calls, ["writer/attempt-1"])
            self.assertEqual(resumed.run_view["status"], "succeeded")
            verification = AuditJournal.verify(run_dir)
            self.assertGreater(verification["event_count"], 5)

    def test_resume_reconciles_accepted_dispatch_ahead_of_checkpoint(self) -> None:
        contract = RunContract(
            run_id="journal-ahead",
            objective="Recover a reserved task.",
            phases=("active",),
            criteria=(
                {
                    "id": "reported",
                    "statement": "The report exists.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("report",),
        )
        dispatch_arguments = {
            "idempotency_key": "reserved-writer",
            "tasks": [
                task(
                    "writer",
                    "writer",
                    "Write after recovery",
                    "report-details/1",
                    criteria=("reported",),
                )
            ],
            "max_parallelism": 1,
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "journal-ahead"
            audit = AuditJournal(
                run_dir,
                contract.run_id,
                actor=AuditActor("kernel", "controller_kernel"),
                evidence_classification="component",
            )
            evidence = EvidenceCatalog(audit=audit)
            kernel = ControllerKernel(contract, evidence=evidence, audit=audit)
            original_merge = audit.merge_checkpoint

            def crash_before_checkpoint(*, status="running", updates):
                raise RuntimeError("simulated crash before checkpoint")

            audit.merge_checkpoint = crash_before_checkpoint  # type: ignore[method-assign]
            from harness_labs.controller_commands import (
                CommandActor,
                CommandEnvelope,
            )

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                kernel.handle(
                    CommandEnvelope(
                        command_id="reserve-writer",
                        run_id=contract.run_id,
                        type="task.dispatch",
                        actor=CommandActor("coordinator", "run_coordinator"),
                        expected_revision=kernel.revision,
                        idempotency_key="reserved-writer",
                        payload=dispatch_arguments,
                    )
                )
            audit.merge_checkpoint = original_merge  # type: ignore[method-assign]
            executions = []

            def session_builder(restored_evidence):
                return ScriptedCoordinatorSession(
                    [
                        ("task_dispatch", dispatch_arguments),
                        ("run_complete_request", {}),
                    ],
                    final="Recovered reserved work.",
                )

            def profiles(restored_evidence):
                report = restored_evidence.add(
                    kind="report",
                    content="recovered",
                    media_type="text/plain",
                    producer_task_id="writer",
                )

                def build(task_value, attempt):
                    executions.append(attempt.attempt_id)
                    return TaskResult(
                        attempt_id=attempt.attempt_id,
                        status="succeeded",
                        payload=semantic_payload(
                            summary="Recovered and wrote once.",
                            details_schema=task_value["details_schema"],
                            details={},
                            artifacts=(report.as_dict(),),
                            criterion_coverage=(
                                {
                                    "criterion_id": "reported",
                                    "status": "satisfied",
                                    "evidence_refs": [report.ref],
                                },
                            ),
                        ),
                    )

                return (
                    RoleProfile(
                        "writer",
                        "writer",
                        frozenset(),
                        lambda task_value: FixtureExecutor(task_value, build),
                    ),
                )

            resumed = resume_controller(
                contract,
                session_builder=session_builder,
                profile_builder=profiles,
                run_dir=run_dir,
            )

            self.assertEqual(resumed.result.status, "succeeded")
            self.assertEqual(executions, ["writer/attempt-1"])
            self.assertEqual(
                resumed.run_view["tasks"][0]["status"],
                "succeeded",
            )


if __name__ == "__main__":
    unittest.main()
