"""Tests for capability matching, repeated roles, and bounded delegation."""

from __future__ import annotations

import time
import unittest

from harness_labs.attempts import TaskAttempt, TaskResult
from harness_labs.controller_commands import CommandActor, CommandEnvelope
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import ControllerKernel, RunContract, RunLimits
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_scheduler import (
    CapabilityScheduler,
    RoleProfile,
    SchedulingError,
)


class ResultExecutor:
    def __init__(
        self,
        *,
        details_schema: str,
        payload_factory,
        delay: float = 0,
    ) -> None:
        self.details_schema = details_schema
        self.payload_factory = payload_factory
        self.delay = delay
        self.closed = False

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        if self.delay:
            time.sleep(self.delay)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=self.payload_factory(attempt),
        )

    def close(self) -> None:
        self.closed = True


class ControllerSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceCatalog()
        self.kernel = ControllerKernel(
            RunContract(
                run_id="schedule",
                objective="Inspect flexibly",
                phases=("active",),
                limits=RunLimits(
                    max_depth=3,
                    max_subagents=6,
                    max_parallelism=3,
                    max_tasks=12,
                ),
            ),
            evidence=self.evidence,
        )

    def dispatch_command(self, tasks: list[dict], *, key: str) -> tuple[str, ...]:
        receipt = self.kernel.handle(
            CommandEnvelope(
                command_id=key,
                run_id="schedule",
                type="task.dispatch",
                actor=CommandActor("coordinator", "run_coordinator"),
                expected_revision=self.kernel.revision,
                idempotency_key=key,
                payload={"tasks": tasks, "max_parallelism": 3},
            )
        )
        self.assertTrue(receipt.accepted, receipt.message)
        return tuple(ref.removeprefix("task:") for ref in receipt.effect_refs)

    def test_repeated_roles_get_fresh_parallel_executors(self) -> None:
        created = []

        def factory(task):
            executor = ResultExecutor(
                details_schema=task["details_schema"],
                delay=0.04,
                payload_factory=lambda attempt: semantic_payload(
                    summary=f"Inspected {attempt.attempt_id}",
                    details_schema=task["details_schema"],
                    details={"route": task["id"]},
                ),
            )
            created.append(executor)
            return executor

        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "browser-inspector",
                    "ui_inspector",
                    frozenset({"repo.read", "browser.inspect"}),
                    factory,
                    backend_id="fixture",
                ),
            )
        )
        task_ids = self.dispatch_command(
            [
                {
                    "id": f"inspect-{index}",
                    "role": "ui_inspector",
                    "objective": f"Inspect viewport {index}",
                    "details_schema": "visual-inspection-details/1",
                    "required_capabilities": [
                        "repo.read",
                        "browser.inspect",
                    ],
                    "acceptance_criteria": [],
                    "dependencies": [],
                }
                for index in range(3)
            ],
            key="inspect-batch",
        )

        outcomes = scheduler.dispatch(
            self.kernel,
            task_ids,
            max_parallelism=3,
        )

        self.assertEqual(len(outcomes), 3)
        self.assertEqual(len({id(executor) for executor in created}), 3)
        self.assertGreaterEqual(scheduler.maximum_active, 2)
        self.assertTrue(all(executor.closed for executor in created))

    def test_missing_capability_fails_before_any_task_starts(self) -> None:
        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "code-only",
                    "ui_inspector",
                    frozenset({"repo.read"}),
                    lambda task: ResultExecutor(
                        details_schema=task["details_schema"],
                        payload_factory=lambda attempt: semantic_payload(
                            summary="Code only",
                            details_schema=task["details_schema"],
                            details={},
                        ),
                    ),
                ),
            )
        )
        task_ids = self.dispatch_command(
            [
                {
                    "id": "visual",
                    "role": "ui_inspector",
                    "objective": "Inspect rendered UI",
                    "details_schema": "visual-inspection-details/1",
                    "required_capabilities": ["browser.inspect"],
                    "acceptance_criteria": [],
                    "dependencies": [],
                }
            ],
            key="visual-batch",
        )

        with self.assertRaisesRegex(SchedulingError, "no profile"):
            scheduler.dispatch(self.kernel, task_ids, max_parallelism=1)

        self.assertEqual(self.kernel.task("visual")["status"], "ready")

    def test_semantic_result_can_request_bounded_subchild(self) -> None:
        def lead_factory(task):
            return ResultExecutor(
                details_schema=task["details_schema"],
                payload_factory=lambda attempt: semantic_payload(
                    summary="Delegated one specialist appraisal.",
                    details_schema=task["details_schema"],
                    details={},
                    delegation_requests=(
                        {
                            "tasks": [
                                {
                                    "id": "architecture-subchild",
                                    "parent_task_id": "architecture-lead",
                                    "role": "architecture_specialist",
                                    "objective": "Inspect module boundaries",
                                    "details_schema": "repository-appraisal-details/1",
                                    "required_capabilities": ["repo.read"],
                                    "acceptance_criteria": [],
                                    "dependencies": [],
                                }
                            ],
                            "max_parallelism": 1,
                        },
                    ),
                ),
            )

        def specialist_factory(task):
            return ResultExecutor(
                details_schema=task["details_schema"],
                payload_factory=lambda attempt: semantic_payload(
                    summary="Inspected architecture.",
                    details_schema=task["details_schema"],
                    details={"layers": ["ui", "domain"]},
                ),
            )

        scheduler = CapabilityScheduler(
            (
                RoleProfile(
                    "lead",
                    "architecture_lead",
                    frozenset({"repo.read"}),
                    lead_factory,
                ),
                RoleProfile(
                    "specialist",
                    "architecture_specialist",
                    frozenset({"repo.read"}),
                    specialist_factory,
                ),
            )
        )
        parent_ids = self.dispatch_command(
            [
                {
                    "id": "architecture-lead",
                    "role": "architecture_lead",
                    "objective": "Lead architecture appraisal",
                    "details_schema": "repository-appraisal-details/1",
                    "required_capabilities": ["repo.read"],
                    "acceptance_criteria": [],
                    "dependencies": [],
                    "may_delegate": True,
                }
            ],
            key="lead-batch",
        )

        scheduler.dispatch(self.kernel, parent_ids, max_parallelism=1)

        child = self.kernel.task("architecture-subchild")
        self.assertEqual(child["parent_task_id"], "architecture-lead")
        self.assertEqual(child["depth"], 2)
        self.assertEqual(child["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
