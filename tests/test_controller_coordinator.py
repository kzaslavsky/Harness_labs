"""End-to-end resident coordinator loop test."""

from __future__ import annotations

import unittest

from harness_labs.attempts import TaskResult
from harness_labs.controller_coordinator import CoordinatorLoop
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import ControllerKernel, RunContract
from harness_labs.controller_projection import ControllerQueries
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_scheduler import CapabilityScheduler, RoleProfile

from tests.controller_scenario_fixtures import (
    FixtureExecutor,
    ScriptedCoordinatorSession,
    task,
)


class ControllerCoordinatorTests(unittest.TestCase):
    def test_incompatible_dispatch_leaves_no_orphaned_ready_task(self) -> None:
        evidence = EvidenceCatalog()
        kernel = ControllerKernel(
            RunContract(
                run_id="transactional-dispatch",
                objective="Write a report",
                phases=("active",),
                criteria=(),
            ),
            evidence=evidence,
        )
        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "writer",
                    "writer",
                    frozenset(),
                    lambda task_value: FixtureExecutor(
                        task_value,
                        lambda task_value, attempt: TaskResult(
                            attempt.attempt_id,
                            "succeeded",
                            semantic_payload(
                                summary="done",
                                details_schema=task_value["details_schema"],
                                details={},
                            ),
                        ),
                    ),
                    details_schemas=frozenset({"report-details/1"}),
                ),
            )
        )
        session = ScriptedCoordinatorSession(
            [
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task("ghost", "writer", "Bad", "unsupported/1")
                        ],
                        "max_parallelism": 1,
                    },
                ),
                ("run_complete_request", {}),
            ],
            final="done",
        )

        result = CoordinatorLoop(
            kernel,
            ControllerQueries(kernel, evidence),
            scheduler,
            session,
        ).run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(kernel.snapshot()["tasks"], {})
        self.assertFalse(session.results[1].success)

    def test_resident_session_drives_task_and_gated_completion(self) -> None:
        evidence = EvidenceCatalog()
        report = evidence.add(
            kind="report",
            content="done",
            media_type="text/plain",
            producer_task_id="writer",
        )
        kernel = ControllerKernel(
            RunContract(
                run_id="coordinator",
                objective="Write a report",
                phases=("active",),
                criteria=(
                    {
                        "id": "written",
                        "statement": "A report exists.",
                        "source": "operator",
                    },
                ),
                terminal_artifact_kinds=("report",),
            ),
            evidence=evidence,
        )

        def result_builder(task_value, attempt):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="succeeded",
                payload=semantic_payload(
                    summary="Wrote the report.",
                    details_schema=task_value["details_schema"],
                    details={},
                    artifacts=(report.as_dict(),),
                    criterion_coverage=(
                        {
                            "criterion_id": "written",
                            "status": "satisfied",
                            "evidence_refs": [report.ref],
                        },
                    ),
                ),
            )

        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "writer",
                    "writer",
                    frozenset(),
                    lambda task_value: FixtureExecutor(
                        task_value,
                        result_builder,
                    ),
                ),
            )
        )
        session = ScriptedCoordinatorSession(
            [
                (
                    "task_dispatch",
                    {
                        "tasks": [
                            task(
                                "writer",
                                "writer",
                                "Write",
                                "report-details/1",
                                criteria=("written",),
                            )
                        ],
                        "max_parallelism": 1,
                    },
                ),
                ("run_complete_request", {}),
            ],
            final="Report complete.",
        )

        result = CoordinatorLoop(
            kernel,
            ControllerQueries(kernel, evidence),
            scheduler,
            session,
        ).run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(kernel.snapshot()["status"], "succeeded")
        self.assertTrue(session.closed)
        self.assertEqual(session.request.context["run_view"]["revision"], 0)
        self.assertEqual(
            session.request.context["available_role_profiles"][0]["role"],
            "writer",
        )
        dispatch_tool = next(
            tool for tool in session.request.tools if tool.name == "task_dispatch"
        )
        self.assertIn("tasks", dispatch_tool.input_schema["required"])
        self.assertEqual(
            session.results[-1].payload["run_view"]["status"],
            "succeeded",
        )


if __name__ == "__main__":
    unittest.main()
