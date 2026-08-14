"""Finding tests for CB-01: four mechanical dispatch-time relaxations.

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it imports nothing that does not already exist at the
base commit, and every assertion is a controlled ``assert*``/``self.fail`` so
a base-harness rejection surfaces as a pytest FAILED, never an ERROR.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, RunContract


class RelaxKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceCatalog()
        self.contract = RunContract(
            run_id="relax-run",
            objective="Exercise the relaxed dispatch-time gates.",
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
            run_id="relax-run",
            type=kind,
            actor=actor or self.actor,
            expected_revision=self.kernel.revision,
            idempotency_key=command_id,
            provenance=CommandProvenance(evidence_refs=evidence_refs),
            payload=payload,
        )

    # AC-CB01-1: "<id>" or "<id>: <text>" acceptance_criteria entries.
    def test_task_dispatch_accepts_id_colon_statement_and_rejects_unknown_id(
        self,
    ) -> None:
        annotated = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "worker-1",
                            "role": "worker",
                            "objective": "Do the work",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [
                                "AC-1: The feature is implemented."
                            ],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-annotated",
            )
        )
        self.assertTrue(
            annotated.accepted,
            f"annotated criterion form was rejected: {annotated.message}",
        )
        self.assertEqual(
            self.kernel.task("worker-1")["acceptance_criteria"],
            ["AC-1"],
        )

        unknown = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "worker-2",
                            "role": "worker",
                            "objective": "Do more work",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [
                                "AC-UNKNOWN: not a declared criterion"
                            ],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-unknown",
            )
        )
        self.assertFalse(unknown.accepted)
        self.assertIn("unknown task criterion", unknown.message)

    # AC-CB01-1: a criterion id that itself contains a colon must not be
    # truncated by the "<id>: <text>" parser.
    def test_task_dispatch_bare_id_with_colon_is_not_truncated(self) -> None:
        self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "svc:AC-1",
                    "statement": "The service-scoped criterion is satisfied.",
                    "source": "operator",
                },
                command_id="propose-colon-id",
            )
        )

        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "worker-colon",
                            "role": "worker",
                            "objective": "Do the work",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["svc:AC-1"],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-colon-id",
            )
        )
        self.assertTrue(
            dispatched.accepted,
            f"bare criterion id containing a colon was rejected: {dispatched.message}",
        )
        self.assertEqual(
            self.kernel.task("worker-colon")["acceptance_criteria"],
            ["svc:AC-1"],
        )

    # AC-CB01-1: a criterion id containing ": " (colon-space) must resolve by
    # literal match before the "<id>: <text>" split is ever attempted.
    def test_task_dispatch_id_with_colon_space_is_not_truncated(self) -> None:
        self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "svc: AC-1",
                    "statement": "The service-scoped criterion is satisfied.",
                    "source": "operator",
                },
                command_id="propose-colon-space-id",
            )
        )

        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "worker-colon-space",
                            "role": "worker",
                            "objective": "Do the work",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["svc: AC-1"],
                            "dependencies": [],
                        }
                    ],
                    "max_parallelism": 1,
                },
                command_id="dispatch-colon-space-id",
            )
        )
        self.assertTrue(
            dispatched.accepted,
            "criterion id containing ': ' was rejected: "
            f"{dispatched.message}",
        )
        self.assertEqual(
            self.kernel.task("worker-colon-space")["acceptance_criteria"],
            ["svc: AC-1"],
        )

    # AC-CB01-2: capability narrowing on a superseding/repair dispatch.
    def test_superseding_dispatch_allows_capability_subset_but_not_widening(
        self,
    ) -> None:
        original = {
            "id": "salvage",
            "role": "worker",
            "objective": "Write and verify the change",
            "details_schema": "salvage/1",
            "required_capabilities": ["repo.read", "repo.write"],
            "acceptance_criteria": [],
            "dependencies": [],
        }
        dispatched = self.kernel.handle(
            self.command(
                "task.dispatch",
                {"tasks": [original]},
                command_id="dispatch-original",
            )
        )
        self.assertTrue(dispatched.accepted)
        self.kernel.mark_tasks_running(("salvage",))
        self.kernel.record_task_results(
            (
                (
                    "salvage",
                    TaskResult(
                        attempt_id="salvage/attempt-1",
                        status="failed",
                        payload={"error": "writable worker requires a clean baseline"},
                    ),
                ),
            )
        )

        narrowed = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "salvage-audit",
                            "required_capabilities": ["repo.read"],
                            "supersedes_task_id": "salvage",
                        }
                    ]
                },
                command_id="dispatch-narrowed",
            )
        )
        self.assertTrue(
            narrowed.accepted,
            f"strict-subset capability narrowing was rejected: {narrowed.message}",
        )

        widened = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "salvage-widened",
                            "required_capabilities": [
                                "repo.read",
                                "repo.write",
                                "network.fetch",
                            ],
                            "supersedes_task_id": "salvage",
                        }
                    ]
                },
                command_id="dispatch-widened",
            )
        )
        self.assertFalse(widened.accepted)
        self.assertIn("required_capabilities", widened.message)

        disjoint = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "salvage-disjoint",
                            "required_capabilities": ["network.fetch"],
                            "supersedes_task_id": "salvage",
                        }
                    ]
                },
                command_id="dispatch-disjoint",
            )
        )
        self.assertFalse(disjoint.accepted)
        self.assertIn("required_capabilities", disjoint.message)

        schema_changed = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            **original,
                            "id": "salvage-schema",
                            "details_schema": "salvage/2",
                            "supersedes_task_id": "salvage",
                        }
                    ]
                },
                command_id="dispatch-schema-changed",
            )
        )
        self.assertFalse(schema_changed.accepted)
        self.assertIn("details_schema", schema_changed.message)

    # AC-CB01-3: criterion source "plan" accepted by the kernel.
    def test_criterion_propose_accepts_plan_source(self) -> None:
        receipt = self.kernel.handle(
            self.command(
                "criterion.propose",
                {
                    "id": "AC-PLAN",
                    "statement": "The approved plan's scope is delivered.",
                    "source": "plan",
                },
                command_id="propose-plan-source",
            )
        )
        self.assertTrue(
            receipt.accepted,
            f"criterion source 'plan' was rejected: {receipt.message}",
        )
        self.assertEqual(
            self.kernel.snapshot()["criteria"]["AC-PLAN"]["source"],
            "plan",
        )

    def test_coordinator_criterion_schema_offers_plan_source(self) -> None:
        from harness_labs.core.controller_coordinator import _tool_specs

        spec = next(
            tool for tool in _tool_specs() if tool.name == "criterion_propose"
        )
        enum = spec.input_schema["properties"]["source"]["enum"]
        self.assertIn("plan", enum)

    # AC-CB01-4: a rejected task.dispatch is citable audit-journal provenance.
    def test_rejected_dispatch_becomes_citable_provenance(self) -> None:
        rejected = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "impl-repair",
                            "role": "worker",
                            "objective": "Repair the implementation",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["AC-DOES-NOT-EXIST"],
                            "dependencies": [],
                        }
                    ]
                },
                command_id="dispatch-doomed-repair",
            )
        )
        self.assertFalse(rejected.accepted)

        citing = self.kernel.handle(
            self.command(
                "decision.record",
                {
                    "id": "cite-rejected-repair",
                    "question": "Why did the repair not land?",
                    "choice": "Escalate with the rejected dispatch as evidence.",
                    "alternatives": [],
                    "rationale": "The prior repair dispatch was rejected.",
                },
                command_id="decision-citing-rejection",
                evidence_refs=(f"command:{rejected.command_id}",),
            )
        )
        self.assertTrue(
            citing.accepted,
            f"rejected dispatch was not citable as provenance: {citing.message}",
        )

    # AC-CB01-4: citability must be a property of persisted kernel state, not
    # a volatile registry that resume drops.
    def test_rejected_dispatch_provenance_survives_resume(self) -> None:
        rejected = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "impl-repair-2",
                            "role": "worker",
                            "objective": "Repair the implementation",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": ["AC-DOES-NOT-EXIST"],
                            "dependencies": [],
                        }
                    ]
                },
                command_id="dispatch-doomed-repair-2",
            )
        )
        self.assertFalse(rejected.accepted)

        resumed = ControllerKernel.from_snapshot(
            self.contract,
            evidence=self.evidence,
            snapshot=self.kernel.snapshot(),
            events=self.kernel.events,
        )

        citing = resumed.handle(
            self.command(
                "decision.record",
                {
                    "id": "cite-rejected-repair-2",
                    "question": "Why did the repair not land?",
                    "choice": "Escalate with the rejected dispatch as evidence.",
                    "alternatives": [],
                    "rationale": "The prior repair dispatch was rejected.",
                },
                command_id="decision-citing-rejection-2",
                evidence_refs=(f"command:{rejected.command_id}",),
            )
        )
        self.assertTrue(
            citing.accepted,
            f"rejected dispatch provenance did not survive resume: {citing.message}",
        )

    # AC-CB01-4: the citable ref must be durable in the on-disk audit
    # checkpoint the instant the rejection is journaled, not only once a
    # later successful command happens to re-checkpoint the full state.
    def test_rejected_dispatch_provenance_is_checkpointed_immediately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            audit = AuditJournal(
                run_dir,
                "relax-run-audit",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            contract = RunContract(
                run_id="relax-run-audit",
                objective="Exercise the relaxed dispatch-time gates.",
                phases=("active",),
                criteria=(
                    {
                        "id": "AC-1",
                        "statement": "The feature is implemented.",
                        "source": "operator",
                    },
                ),
            )
            kernel = ControllerKernel(
                contract, evidence=self.evidence, audit=audit
            )
            actor = CommandActor("coordinator-1", "run_coordinator")
            rejected = kernel.handle(
                CommandEnvelope(
                    command_id="dispatch-doomed-repair-audited",
                    run_id="relax-run-audit",
                    type="task.dispatch",
                    actor=actor,
                    expected_revision=kernel.revision,
                    idempotency_key="dispatch-doomed-repair-audited",
                    provenance=CommandProvenance(evidence_refs=()),
                    payload={
                        "tasks": [
                            {
                                "id": "impl-repair-audited",
                                "role": "worker",
                                "objective": "Repair the implementation",
                                "details_schema": "work/1",
                                "required_capabilities": [],
                                "acceptance_criteria": ["AC-DOES-NOT-EXIST"],
                                "dependencies": [],
                            }
                        ]
                    },
                )
            )
            self.assertFalse(rejected.accepted)

            on_disk_refs = audit.checkpoint_state()["controller"][
                "rejected_task_dispatch_refs"
            ]
            self.assertIn(
                f"command:{rejected.command_id}",
                on_disk_refs,
                "rejected task.dispatch ref was not durably checkpointed "
                "at rejection time",
            )

    # A rejection that never reaches task.dispatch evaluation (envelope
    # validation, e.g. an unauthorized actor) must not mint a citable ref:
    # only a rejected task.dispatch is a business-rule journal event.
    def test_envelope_level_rejection_is_not_citable(self) -> None:
        operator = CommandActor("operator-1", "operator")
        rejected = self.kernel.handle(
            self.command(
                "task.dispatch",
                {
                    "tasks": [
                        {
                            "id": "impl-repair-3",
                            "role": "worker",
                            "objective": "Repair the implementation",
                            "details_schema": "work/1",
                            "required_capabilities": [],
                            "acceptance_criteria": [],
                            "dependencies": [],
                        }
                    ]
                },
                command_id="dispatch-unauthorized",
                actor=operator,
            )
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.error_code, "unauthorized_command")

        citing = self.kernel.handle(
            self.command(
                "decision.record",
                {
                    "id": "cite-unauthorized-rejection",
                    "question": "Why did the repair not land?",
                    "choice": "Escalate with the rejected dispatch as evidence.",
                    "alternatives": [],
                    "rationale": "An unauthorized actor triggered the rejection.",
                },
                command_id="decision-citing-unauthorized",
                evidence_refs=(f"command:{rejected.command_id}",),
            )
        )
        self.assertFalse(
            citing.accepted,
            "an envelope-level rejection must not mint a citable reference",
        )


if __name__ == "__main__":
    unittest.main()
