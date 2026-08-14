"""Finding tests for CB3-01: provenance resolution reaches existing kernel
entities (Item 6 residual half).

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it imports nothing that does not already exist at the
base commit, and every assertion is a controlled ``assert*``/``self.fail`` so
a base-harness rejection surfaces as a pytest FAILED, never an ERROR.
"""

from __future__ import annotations

import unittest

from harness_labs.attempts import TaskResult
from harness_labs.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import ControllerKernel, RunContract


class RelaxRefResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceCatalog()
        self.contract = RunContract(
            run_id="relax-ref-run",
            objective="Exercise provenance resolution against kernel entities.",
            phases=("active",),
            criteria=(
                {
                    "id": "AC-1",
                    "statement": "The feature is implemented.",
                    "source": "operator",
                },
            ),
        )
        self.kernel = ControllerKernel(self.contract, evidence=self.evidence)
        self.actor = CommandActor("coordinator-1", "run_coordinator")

    def command(
        self,
        kind: str,
        payload: dict,
        *,
        command_id: str,
        evidence_refs: tuple[str, ...] = (),
        actor: CommandActor | None = None,
    ) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=command_id,
            run_id="relax-ref-run",
            type=kind,
            actor=actor or self.actor,
            expected_revision=self.kernel.revision,
            idempotency_key=command_id,
            provenance=CommandProvenance(evidence_refs=evidence_refs),
            payload=payload,
        )

    def _dispatch_and_fail(self, task_id: str) -> None:
        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": task_id,
                            "role": "worker",
                            "objective": "Repair the implementation",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                        }
                    ]
                },
                command_id=f"dispatch-{task_id}",
            )
        )
        self.assertTrue(
            dispatched.accepted,
            f"setup dispatch of {task_id} was rejected: {dispatched.message}",
        )
        self.kernel.mark_tasks_running((task_id,))

    # AC-CB301-4 / AC-CB301-1: the exact CB2-08 live specimen shape — a
    # retry.request citing the failed task itself via "task:<id>" — must
    # resolve as provenance instead of dying on "unknown provenance
    # reference". This is the motivating specimen and must fail at the
    # frozen base harness.
    def test_retry_request_cites_failed_task_as_provenance(self) -> None:
        task_id = "impl-orbit-physics-repair"
        self._dispatch_and_fail(task_id)
        self.kernel.record_task_results(
            (
                (
                    task_id,
                    TaskResult(
                        attempt_id=f"{task_id}/attempt-1",
                        status="failed",
                        payload={"error": "physics regression persists"},
                    ),
                ),
            )
        )

        retry = self.kernel.handle(
            self.command(
                "retry.request",
                {"reason": "Retrying the failed physics repair."},
                command_id="retry-citing-failed-task",
                evidence_refs=(f"task:{task_id}",),
            )
        )
        self.assertTrue(
            retry.accepted,
            f"retry.request citing the failed task as provenance was "
            f"rejected: {retry.message}",
        )

    # AC-CB301-1: a recorded system result is citable via
    # "system-result:<id>:<n>".
    def test_recorded_system_result_is_citable_provenance(self) -> None:
        task_id = "impl-system-result-repair"
        self._dispatch_and_fail(task_id)
        events = self.kernel.record_task_results(
            (
                (
                    task_id,
                    TaskResult(
                        attempt_id=f"{task_id}/attempt-1",
                        status="failed",
                        payload={"error": "regression persists"},
                    ),
                ),
            )
        )
        self.assertEqual(len(events), 1)
        result_ref = events[0].command_id
        self.assertTrue(result_ref.startswith(f"system-result:{task_id}:"))

        citing = self.kernel.handle(
            self.command(
                "decision.record",
                {
                    "id": "cite-system-result",
                    "question": "Why retry?",
                    "choice": "Cite the recorded system result.",
                    "alternatives": [],
                    "rationale": "The system result is already in the hash chain.",
                },
                command_id="decision-citing-system-result",
                evidence_refs=(result_ref,),
            )
        )
        self.assertTrue(
            citing.accepted,
            f"recorded system result was not citable as provenance: "
            f"{citing.message}",
        )

    # AC-CB301-1: a recorded decision is citable via "decision:<id>".
    def test_recorded_decision_is_citable_provenance(self) -> None:
        recorded = self.kernel.handle(
            self.command(
                "decision.record",
                {
                    "id": "original-decision",
                    "question": "Which approach?",
                    "choice": "Approach A.",
                    "alternatives": ["Approach B."],
                    "rationale": "Approach A is simpler.",
                },
                command_id="decision-original",
            )
        )
        self.assertTrue(recorded.accepted)

        citing = self.kernel.handle(
            self.command(
                "decision.record",
                {
                    "id": "follow-up-decision",
                    "question": "Should we revisit the approach?",
                    "choice": "No, keep the original decision.",
                    "alternatives": [],
                    "rationale": "The original decision still holds.",
                },
                command_id="decision-follow-up",
                evidence_refs=("decision:original-decision",),
            )
        )
        self.assertTrue(
            citing.accepted,
            f"recorded decision was not citable as provenance: "
            f"{citing.message}",
        )

    # AC-CB301-1: resolution is strictly read-only — citing an existing task
    # as provenance must not register a new evidence record or task.
    def test_task_ref_resolution_is_read_only(self) -> None:
        task_id = "impl-read-only-check"
        self._dispatch_and_fail(task_id)
        self.kernel.record_task_results(
            (
                (
                    task_id,
                    TaskResult(
                        attempt_id=f"{task_id}/attempt-1",
                        status="failed",
                        payload={"error": "still broken"},
                    ),
                ),
            )
        )
        tasks_before = set(self.kernel.snapshot()["tasks"])
        evidence_before = set(self.evidence.list())

        retry = self.kernel.handle(
            self.command(
                "retry.request",
                {"reason": "Retrying."},
                command_id="retry-read-only",
                evidence_refs=(f"task:{task_id}",),
            )
        )
        self.assertTrue(retry.accepted, retry.message)
        self.assertEqual(set(self.kernel.snapshot()["tasks"]), tasks_before)
        self.assertEqual(set(self.evidence.list()), evidence_before)
        self.assertFalse(self.evidence.contains(f"task:{task_id}"))

    # AC-CB301-2: an unresolvable ref is still rejected, and the rejection
    # detail enumerates every accepted ref shape so a wrong guess teaches
    # the correct spelling.
    def test_unresolvable_ref_rejection_enumerates_accepted_shapes(
        self,
    ) -> None:
        rejected = self.kernel.handle(
            self.command(
                "decision.record",
                {
                    "id": "cite-nonexistent",
                    "question": "Why retry?",
                    "choice": "Cite something that does not exist.",
                    "alternatives": [],
                    "rationale": "n/a",
                },
                command_id="decision-citing-nonexistent",
                evidence_refs=("task:does-not-exist",),
            )
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.error_code, "unknown_evidence")
        self.assertIn("unknown provenance reference", rejected.message)
        for shape in (
            "artifact:sha256:<hex>",
            "command:<id>",
            "task:<id>",
            "system-result:<id>:<n>",
            "decision:<id>",
        ):
            self.assertIn(
                shape,
                rejected.message,
                f"rejection detail did not enumerate accepted shape {shape!r}: "
                f"{rejected.message}",
            )

    # AC-CB301-3: an id merely resembling a task/decision id but never
    # dispatched/recorded must still be rejected — resolution is against
    # actual kernel state, not string shape alone.
    def test_ref_naming_an_unregistered_entity_is_still_rejected(self) -> None:
        rejected = self.kernel.handle(
            self.command(
                "retry.request",
                {"reason": "Retrying a task that was never dispatched."},
                command_id="retry-unregistered",
                evidence_refs=("task:never-dispatched",),
            )
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.error_code, "unknown_evidence")


if __name__ == "__main__":
    unittest.main()
