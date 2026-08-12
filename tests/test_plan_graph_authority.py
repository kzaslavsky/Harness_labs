from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_labs.plan_graph import PlanGraph, PlanGraphError, PlanGraphResult
from harness_labs.plan_graph_authority import AutomaticRecoveryAuthority, RecoveryAuthorityError, validate_plan_version_transition, validate_recovery_decision
from harness_labs.plan_graph_budget import BudgetError, RetryBudgetLedger


class RecoveryAuthorityTests(unittest.TestCase):
    def test_runtime_rejects_schema_invalid_digest(self) -> None:
        authority = AutomaticRecoveryAuthority.from_mapping({"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["resume"], "max_extra_node_launches": 0, "max_structural_decisions": 0})
        decision = {"protocol": "plan-graph-recovery-decision/1", "action": "resume", "target": "node", "expected_prior_digest": "g" * 64, "payload": {}}
        with self.assertRaisesRegex(RecoveryAuthorityError, "exceeds registration authority"):
            validate_recovery_decision(decision, authority)

    def test_registration_authority_is_immutable_and_bounds_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["extend_budget"], "max_extra_node_launches": 1, "max_structural_decisions": 0}
            ledger.register(plan_sha256="a" * 64, gates={"node": "gate"}, automatic_recovery=authority)
            ledger.apply_recovery_decision({"protocol": "plan-graph-recovery-decision/1", "action": "extend_budget", "target": "node", "expected_prior_digest": "b" * 64, "payload": {"launches": 1}}, prior_digest="b" * 64)
            with self.assertRaisesRegex(BudgetError, "allowance exhausted"):
                ledger.apply_recovery_decision({"protocol": "plan-graph-recovery-decision/1", "action": "extend_budget", "target": "node", "expected_prior_digest": "b" * 64, "payload": {"launches": 1}}, prior_digest="b" * 64)
            with self.assertRaisesRegex(BudgetError, "registration-immutable"):
                ledger.register(plan_sha256="a" * 64, gates={"node": "gate"}, automatic_recovery={**authority, "max_extra_node_launches": 2})

    def test_transition_requires_identical_node_mapping_and_authorized_action(self) -> None:
        authority = AutomaticRecoveryAuthority.from_mapping({"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["revise_acceptance"], "max_extra_node_launches": 0, "max_structural_decisions": 1})
        transition = {"protocol": "plan-graph-version-transition/1", "action": "revise_acceptance", "predecessor_plan_sha256": "a" * 64, "successor_plan_sha256": "b" * 64, "node_correspondence": {"one": "two"}, "budget_carryover": {}, "authorizing_decision": {"protocol": "plan-graph-recovery-decision/1", "action": "revise_acceptance", "target": "plan_version", "expected_prior_digest": "a" * 64, "payload": {}}}
        with self.assertRaisesRegex(RecoveryAuthorityError, "identical"):
            validate_plan_version_transition(transition, authority)

    def test_transition_is_bound_to_active_predecessor_and_applies_per_node_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["revise_acceptance"], "max_extra_node_launches": 0, "max_structural_decisions": 1}
            ledger.register(plan_sha256="a" * 64, gates={"one": "gate", "two": "gate"}, automatic_recovery=authority)
            ledger.reserve(node_id="one", gate="gate")
            transition = {
                "protocol": "plan-graph-version-transition/1", "action": "revise_acceptance",
                "predecessor_plan_sha256": "a" * 64, "successor_plan_sha256": "b" * 64,
                "node_correspondence": {"one": "one", "two": "two"},
                "budget_carryover": {"one": "reset", "two": "full"},
                "authorizing_decision": {"protocol": "plan-graph-recovery-decision/1", "action": "revise_acceptance", "target": "plan_version", "expected_prior_digest": "a" * 64, "payload": {}},
            }
            ledger.register(plan_sha256="b" * 64, gates={"one": "gate", "two": "gate"}, automatic_recovery=authority, transition=transition)
            ledger.reserve(node_id="one", gate="gate")  # reset erased the first launch
            with self.assertRaisesRegex(BudgetError, "active lineage"):
                ledger.register(plan_sha256="c" * 64, gates={"one": "gate", "two": "gate"}, automatic_recovery=authority, transition={**transition, "successor_plan_sha256": "c" * 64})

    def test_supplied_invalid_transition_cannot_fall_back_to_legacy_relief(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["revise_acceptance"], "max_extra_node_launches": 0, "max_structural_decisions": 1}
            ledger.register(plan_sha256="a" * 64, gates={"node": "gate"}, automatic_recovery=authority)
            ledger.reset(node_id="node", reason="operator relief", accept_plan_sha256="b" * 64)
            with self.assertRaisesRegex(BudgetError, "transition is invalid"):
                ledger.register(
                    plan_sha256="b" * 64, gates={"node": "gate"}, automatic_recovery=authority,
                    transition={"not": "a typed transition"},
                )

    def test_transition_cannot_be_silently_discarded_on_same_plan_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["revise_acceptance"], "max_extra_node_launches": 0, "max_structural_decisions": 1}
            ledger.register(plan_sha256="a" * 64, gates={"node": "gate"}, automatic_recovery=authority)
            transition = {
                "protocol": "plan-graph-version-transition/1", "action": "revise_acceptance",
                "predecessor_plan_sha256": "a" * 64, "successor_plan_sha256": "b" * 64,
                "node_correspondence": {"node": "node"}, "budget_carryover": {"node": "full"},
                "authorizing_decision": {"protocol": "plan-graph-recovery-decision/1", "action": "revise_acceptance", "target": "plan_version", "expected_prior_digest": "a" * 64, "payload": {}},
            }
            with self.assertRaisesRegex(BudgetError, "requires a changed plan digest"):
                ledger.register(plan_sha256="a" * 64, gates={"node": "gate"}, automatic_recovery=authority, transition=transition)

    def test_recovery_action_applicators_are_durable_and_structurally_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["resume", "transfer_ownership", "ratify_gate_change"], "max_extra_node_launches": 0, "max_structural_decisions": 2}
            ledger.register(plan_sha256="a" * 64, gates={"node": "gate", "receiver": "gate"}, automatic_recovery=authority)
            decision = {"protocol": "plan-graph-recovery-decision/1", "action": "resume", "target": "node", "expected_prior_digest": "a" * 64, "payload": {}}
            ledger.apply_recovery_decision(decision, prior_digest="a" * 64)
            ledger.apply_recovery_decision({**decision, "action": "transfer_ownership", "payload": {"receiving_node": "receiver"}}, prior_digest="a" * 64)
            ledger.apply_recovery_decision({**decision, "action": "ratify_gate_change", "payload": {"gate": "new-gate", "budget_carryover": "reset"}}, prior_digest="a" * 64)
            with ledger._locked(shared=True) as handle:
                state = ledger._fold(handle)
            self.assertTrue(state["nodes"]["node"]["reverification_required"])
            self.assertTrue(state["nodes"]["receiver"]["reverification_required"])
            self.assertEqual(state["gates"]["node"], "new-gate")
            self.assertIn("gate_lineage", ledger.path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(BudgetError, "structural recovery allowance exhausted"):
                ledger.apply_recovery_decision({**decision, "action": "transfer_ownership", "payload": {"receiving_node": "receiver"}}, prior_digest="a" * 64)

    def test_transfer_requires_a_distinct_registered_receiving_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["transfer_ownership"], "max_extra_node_launches": 0, "max_structural_decisions": 1}
            ledger.register(plan_sha256="a" * 64, gates={"node": "gate"}, automatic_recovery=authority)
            decision = {"protocol": "plan-graph-recovery-decision/1", "action": "transfer_ownership", "target": "node", "expected_prior_digest": "a" * 64, "payload": {"receiving_node": "missing"}}
            with self.assertRaisesRegex(BudgetError, "receiving node is not registered"):
                ledger.apply_recovery_decision(decision, prior_digest="a" * 64)

    def test_transition_allows_removed_node_without_erasing_historical_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = RetryBudgetLedger(Path(directory), "lineage")
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["revise_acceptance"], "max_extra_node_launches": 0, "max_structural_decisions": 1}
            ledger.register(plan_sha256="a" * 64, gates={"keep": "gate", "remove": "gate"}, automatic_recovery=authority)
            ledger.reserve(node_id="remove", gate="gate")
            transition = {"protocol": "plan-graph-version-transition/1", "action": "revise_acceptance", "predecessor_plan_sha256": "a" * 64, "successor_plan_sha256": "b" * 64, "node_correspondence": {"keep": "keep"}, "budget_carryover": {"keep": "full"}, "authorizing_decision": {"protocol": "plan-graph-recovery-decision/1", "action": "revise_acceptance", "target": "plan_version", "expected_prior_digest": "a" * 64, "payload": {}}}
            ledger.register(plan_sha256="b" * 64, gates={"keep": "gate"}, automatic_recovery=authority, transition=transition)
            with ledger._locked(shared=True) as handle:
                self.assertEqual(ledger._fold(handle)["nodes"]["remove"]["launches"], 1)
            with self.assertRaisesRegex(BudgetError, "not registered"):
                ledger.reserve(node_id="remove", gate="gate")

    def test_terminal_deviation_statuses_have_a_validated_result_summary(self) -> None:
        autonomous = ({"kind": "recovery_decision", "decision": {}},)
        revision = ({"kind": "plan_version_transition", "transition": {}},)
        self.assertEqual(PlanGraph._completion_status(autonomous), "completed_under_full_autonomy")
        self.assertEqual(PlanGraph._completion_status(revision), "completed_with_deviations")
        with self.assertRaisesRegex(PlanGraphError, "unsupported kind"):
            PlanGraph._completion_status(({"kind": "unrecognized"},))

        result = PlanGraphResult("completed_under_full_autonomy", None, {}, deviation_records=autonomous)
        payload = PlanGraph._result_payload(result)
        self.assertEqual(payload["deviation_summary"], {
            "record_count": 1,
            "terminal_status": "completed_under_full_autonomy",
            "records": [{"kind": "recovery_decision", "decision": {}}],
        })
        with self.assertRaisesRegex(PlanGraphError, "does not match"):
            PlanGraphResult("succeeded", None, {}, deviation_records=autonomous)
        with self.assertRaisesRegex(PlanGraphError, "does not match"):
            PlanGraphResult("completed_with_deviations", None, {})


if __name__ == "__main__":
    unittest.main()
