"""Tests for deterministic commands, projections, and completion gates."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


from harness_labs.core.attempts import TaskResult
from harness_labs.core.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import (
    ControllerKernel,
    KernelError,
    RunContract,
    RunLimits,
)
from harness_labs.core.controller_projection import ControllerQueries, project_run_view
from harness_labs.core.controller_results import semantic_payload


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
                max_subagents=3,
                max_parallelism=2,
                max_tasks=6,
            ),
        )
        self.kernel = ControllerKernel(
            self.contract,
            evidence=self.evidence,
        )
        self.actor = CommandActor("coordinator-1", "run_coordinator")

    def test_run_limits_distinguish_subagent_count_from_parallelism(self) -> None:
        limits = self.contract.limits.as_dict()
        self.assertEqual(limits["max_subagents"], 3)
        self.assertEqual(limits["max_parallelism"], 2)
        self.assertNotIn("max_fan_out", limits)

    def test_default_limits_only_bound_depth_and_direct_subagents(self) -> None:
        limits = RunLimits().as_dict()
        self.assertEqual(limits["max_depth"], 5)
        self.assertEqual(limits["max_subagents"], 5)
        self.assertIsNone(limits["max_parallelism"])
        self.assertIsNone(limits["max_tasks"])

    def test_dispatch_rejects_more_than_max_subagents(self) -> None:
        tasks = [
            {
                "id": f"worker-{index}",
                "role": "worker",
                "objective": "Work",
                "details_schema": "work/1",
                "required_capabilities": [],
                "acceptance_criteria": [],
                "dependencies": [],
            }
            for index in range(4)
        ]
        receipt = self.kernel.handle(
            self.command(
                "task.dispatch",
                {"tasks": tasks, "max_parallelism": 1},
                command_id="too-many-subagents",
            )
        )
        self.assertFalse(receipt.accepted)
        self.assertIn("max_subagents", receipt.message)

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

    def test_successful_changed_method_successor_supersedes_failed_task(self) -> None:
        original = {
            "id": "verify",
            "role": "worker",
            "objective": "Verify the result",
            "details_schema": "verification/1",
            "required_capabilities": [],
            "acceptance_criteria": [],
            "dependencies": [],
        }
        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {"tasks": [original]},
                command_id="dispatch-verify",
            )
        )
        self.assertTrue(dispatched.accepted)
        self.kernel.mark_tasks_running(("verify",))
        self.kernel.record_task_results(
            (
                (
                    "verify",
                    TaskResult(
                        attempt_id="verify/attempt-1",
                        status="failed",
                        payload={"error": "changed method required"},
                    ),
                ),
            )
        )

        widened = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "verify-widened",
                            "role": "privileged-worker",
                            "supersedes_task_id": "verify",
                        }
                    ]
                },
                command_id="dispatch-widened-successor",
            )
        )
        self.assertFalse(widened.accepted)
        self.assertIn("changes frozen authority: role", widened.message)

        successor = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "verify-r2",
                            "objective": "Verify with a changed method",
                            "supersedes_task_id": "verify",
                        }
                    ]
                },
                command_id="dispatch-successor",
            )
        )
        self.assertTrue(successor.accepted)
        self.kernel.mark_tasks_running(("verify-r2",))
        self.kernel.record_task_results(
            (
                (
                    "verify-r2",
                    TaskResult(
                        attempt_id="verify-r2/attempt-1",
                        status="succeeded",
                        payload=semantic_payload(
                            summary="Verification passed with the changed method.",
                            details_schema="verification/1",
                            details={},
                        ),
                    ),
                ),
            )
        )

        self.assertNotIn(
            "required task did not succeed: verify",
            self.kernel.completion_failures(),
        )
        tasks = {task["id"]: task for task in project_run_view(self.kernel)["tasks"]}
        self.assertEqual(tasks["verify"]["status"], "failed")
        self.assertEqual(tasks["verify-r2"]["supersedes_task_id"], "verify")

    def dispatcher_command(
        self,
        kind: str,
        payload: dict,
        *,
        command_id: str,
    ) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=command_id,
            run_id="run-1",
            type=kind,
            actor=CommandActor("dispatcher-1", "dispatcher"),
            expected_revision=self.kernel.revision,
            idempotency_key=command_id,
            payload=payload,
        )

    def start_coordinator_session(self, session_id: str) -> None:
        registered = self.kernel.handle(
            self.dispatcher_command(
                "coordinator.schema_register",
                {
                    "protocol": "coordinator-dispatch-schema/1",
                    "schema_id": "orphan-schema/1",
                    "sha256": "a" * 64,
                    "phases": ["active"],
                    "segments": [{"id": "active", "phases": ["active"]}],
                },
                command_id="register-schema",
            )
        )
        self.assertTrue(registered.accepted, registered.message)
        started = self.kernel.handle(
            self.dispatcher_command(
                "coordinator.session_start",
                {
                    "session_id": session_id,
                    "segment_id": "active",
                    "attempt": 1,
                },
                command_id=f"start-{session_id}",
            )
        )
        self.assertTrue(started.accepted, started.message)

    # A dead coordinator session must not leave its in-flight tasks
    # permanently "running": no later session can record their results
    # (record_task_results is gone with the session), supersede only accepts
    # terminal tasks, and phases cannot advance over active tasks -- the
    # exact deadlock observed in flow-editor-uc1-coreloop-attempt-3-UC-1G.
    def test_session_end_orphans_running_tasks_preserving_worker_evidence(
        self,
    ) -> None:
        self.kernel.handle(
            self.command(
                "task.dispatch",
                {
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
                command_id="dispatch-implement",
            )
        )
        self.start_coordinator_session("session-1")
        self.kernel.mark_tasks_running(("implement",))
        worker_stdout = self.evidence.add(
            kind="live-worker-stdout",
            content='{"result": "completed"}',
            media_type="application/json",
            producer_task_id="implement",
        )

        ended = self.kernel.handle(
            self.dispatcher_command(
                "coordinator.session_end",
                {
                    "session_id": "session-1",
                    "outcome": "recoverable_failure",
                    "result_status": "failed",
                    "reason": "coordinator killed by the gate-claim fence",
                },
                command_id="end-session-1",
            )
        )

        self.assertTrue(ended.accepted, ended.message)
        self.assertIn("task:implement", ended.effect_refs)
        task = self.kernel.task("implement")
        self.assertEqual(task["status"], "failed")
        self.assertTrue(task["orphaned"])
        self.assertEqual(task["result"]["error_type"], "OrphanedTaskError")
        self.assertIn(worker_stdout.ref, task["evidence"])
        session = self.kernel.snapshot()["coordinator_dispatch"]["sessions"][-1]
        self.assertEqual(session["orphaned_task_ids"], ["implement"])
        view_tasks = {
            item["id"]: item for item in project_run_view(self.kernel)["tasks"]
        }
        self.assertTrue(view_tasks["implement"]["orphaned"])

        # A late worker result for the orphaned attempt is now rejected
        # instead of racing kernel state.
        with self.assertRaisesRegex(KernelError, "not running"):
            self.kernel.record_task_results(
                (
                    (
                        "implement",
                        TaskResult(
                            attempt_id="implement/attempt-1",
                            status="succeeded",
                            payload=semantic_payload(
                                summary="Too late.",
                                details_schema="work/1",
                                details={},
                            ),
                        ),
                    ),
                )
            )

    def test_fresh_attempt_supersedes_orphaned_task(self) -> None:
        original = {
            "id": "implement",
            "role": "worker",
            "objective": "Implement the change",
            "details_schema": "work/1",
            "required_capabilities": [],
            "acceptance_criteria": [],
            "dependencies": [],
        }
        self.kernel.handle(
            self.command(
                "task.dispatch",
                {"tasks": [original]},
                command_id="dispatch-implement",
            )
        )
        self.start_coordinator_session("session-1")
        self.kernel.mark_tasks_running(("implement",))
        self.kernel.handle(
            self.dispatcher_command(
                "coordinator.session_end",
                {"session_id": "session-1", "outcome": "recoverable_failure"},
                command_id="end-session-1",
            )
        )

        successor = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "implement-v2",
                            "supersedes_task_id": "implement",
                        }
                    ]
                },
                command_id="dispatch-successor",
            )
        )
        self.assertTrue(successor.accepted, successor.message)
        self.kernel.mark_tasks_running(("implement-v2",))
        self.kernel.record_task_results(
            (
                (
                    "implement-v2",
                    TaskResult(
                        attempt_id="implement-v2/attempt-1",
                        status="succeeded",
                        payload=semantic_payload(
                            summary="Implemented on the fresh attempt.",
                            details_schema="work/1",
                            details={},
                        ),
                    ),
                ),
            )
        )
        failures = self.kernel.completion_failures()
        self.assertNotIn("task is still active: implement", failures)
        self.assertNotIn("required task did not succeed: implement", failures)

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

    # AC-CB06-1: a run-contract criterion may declare deterministic-verification
    # adjudication, and it can only become satisfied from the verification
    # owner's own passing command evidence (record_gate_verification), never
    # from a coordinator claim.
    def test_gate_backed_criterion_satisfied_only_by_verification_owner(self) -> None:
        contract = RunContract(
            run_id="gate-kernel-run",
            objective="Ship a change whose correctness is gated by a command.",
            phases=("active",),
            criteria=(
                {
                    "id": "gate",
                    "statement": "The declared verification command passes.",
                    "source": "operator",
                    "adjudication": "deterministic_verification",
                },
            ),
        )
        kernel = ControllerKernel(contract, evidence=self.evidence)
        self.assertEqual(
            kernel.snapshot()["criteria"]["gate"]["adjudication"],
            "deterministic_verification",
        )
        self.assertEqual(
            self.kernel.snapshot()["criteria"]["grounded"]["adjudication"],
            "claimed",
        )

        completion = kernel.handle(
            CommandEnvelope(
                command_id="complete-with-gate-pending",
                run_id="gate-kernel-run",
                type="run.complete_request",
                actor=self.actor,
                expected_revision=kernel.revision,
                idempotency_key="complete-with-gate-pending",
                payload={},
            )
        )
        self.assertTrue(completion.accepted, completion.message)
        self.assertEqual(kernel.snapshot()["criteria"]["gate"]["status"], "pending")

        verification_artifact = self.evidence.add(
            kind="deterministic-verification-output",
            content={"exit_code": 0, "argv": ["python3", "-m", "unittest"]},
            media_type="application/json",
            producer_task_id="verification-owner",
        )
        events = kernel.record_gate_verification(
            criterion_ids=("gate",),
            evidence_ref=verification_artifact.ref,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "criterion.gate_verified")
        gate = kernel.snapshot()["criteria"]["gate"]
        self.assertEqual(gate["status"], "satisfied")
        self.assertIn("verification-owner", gate["satisfied_by"])
        self.assertIn(verification_artifact.ref, gate["evidence_refs"])

    def test_record_gate_verification_rejects_non_gate_criterion_and_unknown_evidence(
        self,
    ) -> None:
        verification_artifact = self.evidence.add(
            kind="deterministic-verification-output",
            content={"exit_code": 0},
            media_type="application/json",
            producer_task_id="verification-owner",
        )
        with self.assertRaisesRegex(KernelError, "not gate-backed"):
            self.kernel.record_gate_verification(
                criterion_ids=("grounded",),
                evidence_ref=verification_artifact.ref,
            )
        with self.assertRaisesRegex(KernelError, "unknown evidence"):
            self.kernel.record_gate_verification(
                criterion_ids=("grounded",),
                evidence_ref="artifact:sha256:" + "0" * 64,
            )

    # AC-CB06-1: kind and producer_task_id alone only prove a record came
    # from the verification stage, not which attempt of a (possibly several)
    # verification run it is. record_gate_verification must additionally
    # bind the receipt to a passing (exit_code == 0) attempt, and
    # record_gate_verification_failure to a failing one, or a failing
    # attempt's own evidence could satisfy a criterion it never passed for.
    def test_record_gate_verification_rejects_evidence_from_a_failing_attempt(
        self,
    ) -> None:
        contract = RunContract(
            run_id="gate-receipt-run",
            objective="Ship a change whose correctness is gated by a command.",
            phases=("active",),
            criteria=(
                {
                    "id": "gate",
                    "statement": "The declared verification command passes.",
                    "source": "operator",
                    "adjudication": "deterministic_verification",
                },
            ),
        )
        kernel = ControllerKernel(contract, evidence=self.evidence)
        failing_attempt = self.evidence.add(
            kind="deterministic-verification-output",
            content={"exit_code": 1, "argv": ["python3", "-m", "unittest"]},
            media_type="application/json",
            producer_task_id="verification-owner",
        )
        with self.assertRaisesRegex(KernelError, "not a passing command result"):
            kernel.record_gate_verification(
                criterion_ids=("gate",),
                evidence_ref=failing_attempt.ref,
            )
        self.assertEqual(kernel.snapshot()["criteria"]["gate"]["status"], "pending")

        passing_attempt = self.evidence.add(
            kind="deterministic-verification-output",
            content={"exit_code": 0, "argv": ["python3", "-m", "unittest"]},
            media_type="application/json",
            producer_task_id="verification-owner",
        )
        with self.assertRaisesRegex(KernelError, "not a failing command result"):
            kernel.record_gate_verification_failure(
                criterion_ids=("gate",),
                evidence_ref=passing_attempt.ref,
            )

    # AC-CB06-1: a coordinator can never mint its own gate -- only a
    # run-contract criterion (constructed at kernel initialization) may
    # declare deterministic-verification adjudication.
    def test_criterion_propose_rejects_deterministic_verification_adjudication(
        self,
    ) -> None:
        receipt = self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "coordinator-gate",
                    "statement": "The coordinator declares its own gate.",
                    "source": "coordinator",
                    "adjudication": "deterministic_verification",
                },
                command_id="propose-gate",
            )
        )
        self.assertFalse(receipt.accepted)
        self.assertIn("deterministic-verification adjudication", receipt.message)

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
