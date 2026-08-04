"""Tests for deterministic commands, projections, and completion gates."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


from harness_labs.attempts import TaskResult
from harness_labs.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import (
    ControllerKernel,
    KernelError,
    RunContract,
    RunLimits,
)
from harness_labs.controller_projection import ControllerQueries, project_run_view
from harness_labs.controller_results import semantic_payload


class ControllerKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceCatalog()
        self.contract = RunContract(
            run_id="run-1",
            objective="Produce a grounded report.",
            phases=("active",),
            criteria=(
                {
                    "id": "grounded",
                    "statement": "Ground the report in evidence.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("final-report",),
            limits=RunLimits(
                max_depth=2,
                max_fan_out=3,
                max_parallelism=2,
                max_tasks=6,
            ),
        )
        self.kernel = ControllerKernel(
            self.contract,
            evidence=self.evidence,
        )
        self.actor = CommandActor("coordinator-1", "run_coordinator")

    def command(
        self,
        kind: str,
        payload: dict,
        *,
        command_id: str,
        revision: int | None = None,
        idempotency_key: str | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=command_id,
            run_id="run-1",
            type=kind,
            actor=self.actor,
            expected_revision=(
                self.kernel.revision if revision is None else revision
            ),
            idempotency_key=idempotency_key or command_id,
            provenance=CommandProvenance(evidence_refs=evidence_refs),
            payload=payload,
        )

    def test_idempotency_and_stale_revision_are_distinct(self) -> None:
        first = self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "clear",
                    "statement": "The report is understandable.",
                    "source": "coordinator",
                },
                command_id="command-1",
            )
        )
        digest = self.kernel.state_digest()

        duplicate = self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "clear",
                    "statement": "The report is understandable.",
                    "source": "coordinator",
                },
                command_id="command-retry",
                revision=0,
                idempotency_key="command-1",
            )
        )
        stale = self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "another",
                    "statement": "Another criterion.",
                    "source": "coordinator",
                },
                command_id="command-stale",
                revision=0,
            )
        )

        self.assertEqual(first.status, "accepted")
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(stale.error_code, "stale_revision")
        self.assertEqual(self.kernel.state_digest(), digest)

    def test_task_result_promotes_evidence_and_satisfies_completion(self) -> None:
        report = self.evidence.add(
            kind="final-report",
            content="# Grounded report\n",
            media_type="text/markdown",
            producer_task_id="write-report",
        )
        dispatch = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "write-report",
                            "role": "writer",
                            "objective": "Write the report",
                            "details_schema": "report-details/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["grounded"],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-report",
            )
        )
        self.assertTrue(dispatch.accepted)
        self.kernel.mark_tasks_running(("write-report",))
        result = TaskResult(
            attempt_id="write-report/attempt-1",
            status="succeeded",
            payload=semantic_payload(
                summary="Produced a grounded report.",
                details_schema="report-details/1",
                details={"sections": 1},
                claims=(
                    {
                        "id": "claim-1",
                        "statement": "The report exists.",
                        "kind": "observed",
                        "evidence_refs": [report.ref],
                    },
                ),
                artifacts=(report.as_dict(),),
                criterion_coverage=(
                    {
                        "criterion_id": "grounded",
                        "status": "satisfied",
                        "evidence_refs": [report.ref],
                    },
                ),
            ),
        )
        self.kernel.record_task_results((("write-report", result),))

        view = project_run_view(self.kernel)
        self.assertEqual(view["criteria"][0]["status"], "satisfied")
        self.assertEqual(view["artifacts"][0]["kind"], "final-report")
        completion = self.kernel.handle(
            self.command(
                "run.complete_request",
                {},
                command_id="complete",
            )
        )
        self.assertTrue(completion.accepted)
        self.assertEqual(self.kernel.snapshot()["status"], "succeeded")

    def test_completion_rejects_open_review_finding(self) -> None:
        review = self.evidence.add(
            kind="review",
            content="Finding",
            media_type="text/plain",
            producer_task_id="review",
        )
        self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "review",
                            "role": "reviewer",
                            "objective": "Review",
                            "details_schema": "review-details/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                            "optional": True,
                        }
                    ]
                },
                command_id="dispatch-review",
            )
        )
        self.kernel.mark_tasks_running(("review",))
        self.kernel.record_task_results(
            (
                (
                    "review",
                    TaskResult(
                        attempt_id="review/attempt-1",
                        status="succeeded",
                        payload=semantic_payload(
                            summary="Found a problem.",
                            details_schema="review-details/1",
                            details={},
                            findings=(
                                {
                                    "id": "f1",
                                    "statement": "The report is unsupported.",
                                    "category": "evidence",
                                    "severity": "critical",
                                    "requires_disposition": True,
                                    "evidence_refs": [review.ref],
                                },
                            ),
                        ),
                    ),
                ),
            )
        )
        failures = self.kernel.completion_failures()
        self.assertIn("finding is unresolved: review/f1", failures)

    def test_invalid_semantic_promotion_changes_no_task_or_evidence_state(self) -> None:
        artifact = self.evidence.add(
            kind="invalid-report",
            content="invalid",
            media_type="text/plain",
            producer_task_id="invalid",
        )
        self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "invalid",
                            "role": "writer",
                            "objective": "Return invalid coverage",
                            "details_schema": "report-details/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                        }
                    ]
                },
                command_id="dispatch-invalid",
            )
        )
        self.kernel.mark_tasks_running(("invalid",))
        before = self.kernel.state_digest()

        with self.assertRaisesRegex(KernelError, "unknown criterion"):
            self.kernel.record_task_results(
                (
                    (
                        "invalid",
                        TaskResult(
                            attempt_id="invalid/attempt-1",
                            status="succeeded",
                            payload=semantic_payload(
                                summary="Invalid coverage.",
                                details_schema="report-details/1",
                                details={},
                                artifacts=(artifact.as_dict(),),
                                criterion_coverage=(
                                    {
                                        "criterion_id": "does-not-exist",
                                        "status": "satisfied",
                                        "evidence_refs": [artifact.ref],
                                    },
                                ),
                            ),
                        ),
                    ),
                )
            )

        self.assertEqual(self.kernel.state_digest(), before)
        self.assertEqual(self.kernel.task("invalid")["status"], "running")
        self.assertEqual(self.kernel.snapshot()["artifacts"], {})

    def test_worker_delegation_is_bounded_by_parent_authority_and_depth(self) -> None:
        self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "parent",
                            "role": "lead",
                            "objective": "Lead",
                            "details_schema": "lead/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                            "may_delegate": True,
                        }
                    ]
                },
                command_id="dispatch-parent",
            )
        )
        self.kernel.mark_tasks_running(("parent",))
        child_command = CommandEnvelope(
            command_id="child-command",
            run_id="run-1",
            type="task.dispatch",
            actor=CommandActor("parent", "worker"),
            expected_revision=self.kernel.revision,
            idempotency_key="child-command",
            payload={
                "tasks": [
                    {
                        "id": "child",
                        "parent_task_id": "parent",
                        "role": "specialist",
                        "objective": "Investigate",
                        "details_schema": "specialist/1",
                        "required_capabilities": [],
                        "acceptance_criteria": [],
                        "dependencies": [],
                    }
                ]
            },
        )
        accepted = self.kernel.handle(child_command)
        self.assertTrue(accepted.accepted)
        self.assertEqual(self.kernel.task("child")["depth"], 2)

        grandchild = CommandEnvelope(
            command_id="grandchild-command",
            run_id="run-1",
            type="task.dispatch",
            actor=CommandActor("child", "worker", "parent"),
            expected_revision=self.kernel.revision,
            idempotency_key="grandchild-command",
            payload={
                "tasks": [
                    {
                        "id": "grandchild",
                        "parent_task_id": "child",
                        "role": "specialist",
                        "objective": "Go deeper",
                        "details_schema": "specialist/1",
                        "required_capabilities": [],
                        "acceptance_criteria": [],
                        "dependencies": [],
                    }
                ]
            },
        )
        rejected = self.kernel.handle(grandchild)
        self.assertFalse(rejected.accepted)
        self.assertIn("may not delegate", rejected.message)

    def test_queries_open_only_requested_artifact(self) -> None:
        first = self.evidence.add(
            kind="one",
            content="first",
            media_type="text/plain",
            producer_task_id="task-1",
        )
        self.evidence.add(
            kind="two",
            content="second",
            media_type="text/plain",
            producer_task_id="task-2",
        )
        queries = ControllerQueries(self.kernel, self.evidence)

        opened = queries.execute("artifact.open", {"ref": first.ref})

        self.assertEqual(opened["content"], "first")
        self.assertNotIn("second", str(opened))

    def test_machine_readable_contracts_validate_runtime_values(self) -> None:
        schemas = Path(__file__).parents[1] / "schemas"
        command = self.command(
            "criterion.propose",
            {
                "id": "clear",
                "statement": "The report is clear.",
                "source": "coordinator",
            },
            command_id="schema-command",
        )
        receipt = self.kernel.handle(command)

        self.assert_schema_shape(
            schemas / "controller-command.schema.json",
            command.as_dict(),
        )
        self.assert_schema_shape(
            schemas / "controller-receipt.schema.json",
            receipt.as_dict(),
        )
        self.assert_schema_shape(
            schemas / "controller-run-view.schema.json",
            project_run_view(self.kernel),
        )
        self.assert_schema_shape(
            schemas / "controller-task-result.schema.json",
            semantic_payload(
                summary="Schema-valid result.",
                details_schema="generic/1",
                details={},
            ),
        )

    def assert_schema_shape(self, path: Path, value: dict) -> None:
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["type"], "object")
        self.assertTrue(set(schema["required"]).issubset(value))
        self.assertEqual(
            value["protocol"],
            schema["properties"]["protocol"]["const"],
        )


if __name__ == "__main__":
    unittest.main()
