"""Finding tests for CB-06: gate-backed criteria adjudication.

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it imports nothing that does not already exist at the
base commit, and every assertion is a controlled ``assert*``/``self.fail`` so
a base-harness rejection surfaces as a pytest FAILED, never an ERROR.

The base harness requires every declared criterion to already be
"satisfied" before ``run.complete_request`` is accepted, and the only way a
criterion becomes "satisfied" is a task result claiming it. On the
plan-graph bound dispatch path the coordinator is explicitly forbidden from
dispatching a verification-only task, so a criterion whose statement is the
result of the declared deterministic verification command can be satisfied
before that command has run only by an untruthful claim -- the dead end this
node closes (pg88, diagnosis item 10).
"""

from __future__ import annotations

import unittest

from harness_labs.core.attempts import TaskResult
from harness_labs.core.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, KernelError, RunContract
from harness_labs.core.controller_results import semantic_payload


class RelaxGateCriteriaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceCatalog()
        self.contract = RunContract(
            run_id="gate-run",
            objective="Ship a change whose correctness is gated by a command.",
            phases=("implement",),
            criteria=(
                {
                    "id": "AC-IMPL",
                    "statement": "The implementation is complete.",
                    "source": "operator",
                },
                {
                    "id": "AC-GATE",
                    "statement": "The declared deterministic verification command passes.",
                    "source": "operator",
                    "adjudication": "deterministic_verification",
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
            run_id="gate-run",
            type=kind,
            actor=actor or self.actor,
            expected_revision=self.kernel.revision,
            idempotency_key=command_id,
            provenance=CommandProvenance(evidence_refs=evidence_refs),
            payload=payload,
        )

    def _satisfy_implementation_criterion(self) -> None:
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Implemented.",
            media_type="text/markdown",
            producer_task_id="implement",
        )
        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "implement",
                            "role": "builder",
                            "objective": "Implement the change",
                            "details_schema": "implement/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["AC-IMPL"],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-implement",
            )
        )
        self.assertTrue(dispatched.accepted, dispatched.message)
        self.kernel.mark_tasks_running(("implement",))
        self.kernel.record_task_results(
            (
                (
                    "implement",
                    TaskResult(
                        attempt_id="implement/attempt-1",
                        status="succeeded",
                        payload=semantic_payload(
                            summary="Implemented the change.",
                            details_schema="implement/1",
                            details={},
                            artifacts=(artifact.as_dict(),),
                            criterion_coverage=(
                                {
                                    "criterion_id": "AC-IMPL",
                                    "status": "satisfied",
                                    "evidence_refs": [artifact.ref],
                                },
                            ),
                        ),
                    ),
                ),
            )
        )

    # AC-CB06-2: the kernel accepts run.complete_request once every non-gate
    # criterion is satisfied, even while a gate-backed criterion is still
    # pending -- the coordinator's only legal move at the base harness is a
    # dead-end run.block_request (pg88), because the base harness demands the
    # gate criterion already be "satisfied" too, and nothing but an
    # untruthful claim could put it there before the gate has actually run.
    #
    # AC-CB06-1: acceptance must not itself satisfy the gate-backed
    # criterion -- it stays pending until the verification owner records its
    # own passing command evidence, which this kernel-only test never
    # supplies.
    def test_completion_is_accepted_with_a_pending_gate_backed_criterion(
        self,
    ) -> None:
        self._satisfy_implementation_criterion()

        completion = self.kernel.handle(
            self.command(
                "run.complete_request",
                {},
                command_id="complete",
            )
        )
        self.assertTrue(
            completion.accepted,
            "run.complete_request was rejected while only the gate-backed "
            f"criterion remained pending: {completion.message}",
        )
        self.assertEqual(
            self.kernel.snapshot()["criteria"]["AC-GATE"]["status"],
            "pending",
            "a gate-backed criterion must not be satisfied by anything other "
            "than the verification owner's own passing command evidence",
        )

    # AC-CB06-3: a coordinator/worker claim that attempts to satisfy a
    # gate-backed criterion directly (via task-result criterion coverage,
    # the only claim path the harness has) must be rejected outright, never
    # silently accepted the way an ordinary claimed criterion would be.
    def test_direct_claim_of_gate_backed_criterion_is_rejected(self) -> None:
        artifact = self.evidence.add(
            kind="verification-claim",
            content="I ran the tests myself.",
            media_type="text/plain",
            producer_task_id="verify-claim",
        )
        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "verify-claim",
                            "role": "builder",
                            "objective": "Claim the gate passed",
                            "details_schema": "verify-claim/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["AC-GATE"],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-verify-claim",
            )
        )
        self.assertTrue(dispatched.accepted, dispatched.message)
        self.kernel.mark_tasks_running(("verify-claim",))

        with self.assertRaises(KernelError):
            self.kernel.record_task_results(
                (
                    (
                        "verify-claim",
                        TaskResult(
                            attempt_id="verify-claim/attempt-1",
                            status="succeeded",
                            payload=semantic_payload(
                                summary="The gate command passed.",
                                details_schema="verify-claim/1",
                                details={},
                                artifacts=(artifact.as_dict(),),
                                criterion_coverage=(
                                    {
                                        "criterion_id": "AC-GATE",
                                        "status": "satisfied",
                                        "evidence_refs": [artifact.ref],
                                    },
                                ),
                            ),
                        ),
                    ),
                )
            )

        self.assertEqual(
            self.kernel.snapshot()["criteria"]["AC-GATE"]["status"],
            "pending",
            "a rejected direct claim must not have mutated the gate-backed "
            "criterion's state",
        )


if __name__ == "__main__":
    unittest.main()
