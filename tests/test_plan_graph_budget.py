from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from harness_labs.plan_graph_budget import BudgetConfig, BudgetError, RetryBudgetLedger


class RetryBudgetLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RetryBudgetLedger(self.root, "shared", BudgetConfig(node_gate_limit=2))
        self.ledger.register(plan_sha256="a" * 64, gates={"node": "gate"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lineage_accumulates_reservations_and_exhaustion_is_relievable(self) -> None:
        first = self.ledger.reserve(node_id="node", gate="gate")
        RetryBudgetLedger(self.root, "shared", BudgetConfig(node_gate_limit=2)).reserve(node_id="node", gate="gate")
        with self.assertRaisesRegex(BudgetError, "exhausted"):
            self.ledger.reserve(node_id="node", gate="gate")
        self.ledger.extend(node_id="node", launches=1, reason="operator approved one retry")
        self.ledger.reserve(node_id="node", gate="gate")
        self.ledger.started(first)
        self.ledger.completed(first, "failed")

    def test_concurrent_reservations_are_atomic(self) -> None:
        def reserve() -> bool:
            try:
                RetryBudgetLedger(self.root, "shared", BudgetConfig(node_gate_limit=2)).reserve(node_id="node", gate="gate")
                return True
            except BudgetError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(lambda _: reserve(), range(4)))
        self.assertEqual(sum(outcomes), 2)

    def test_initial_publication_fsyncs_parent_directory(self) -> None:
        root = self.root / "durable-publication"
        ledger = RetryBudgetLedger(root, "lineage", BudgetConfig())
        root_directory_fd = 8675309
        ledger_directory_fd = 8675310
        with (
            patch(
                "harness_labs.plan_graph_budget.os.open",
                side_effect=(root_directory_fd, ledger_directory_fd),
            ) as open_directory,
            patch("harness_labs.plan_graph_budget.os.fsync") as fsync,
            patch("harness_labs.plan_graph_budget.os.close") as close_directory,
        ):
            ledger.register(plan_sha256="a" * 64, gates={"node": "gate"})
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        self.assertEqual(
            open_directory.call_args_list,
            [call(ledger.path.parent.parent, flags), call(ledger.path.parent, flags)],
        )
        # Creation of the ledger directory is published before the ledger
        # file is written, and the containing directory is published again
        # only after the file itself has reached stable storage.
        fsync_fds = [entry.args[0] for entry in fsync.call_args_list]
        self.assertEqual(fsync_fds[0], root_directory_fd)
        self.assertEqual(fsync_fds[-1], ledger_directory_fd)
        self.assertEqual(len(fsync_fds), 3)
        self.assertNotIn(fsync_fds[1], {root_directory_fd, ledger_directory_fd})
        self.assertEqual(
            close_directory.call_args_list,
            [call(root_directory_fd), call(ledger_directory_fd)],
        )

    def test_changed_plan_and_gate_fail_closed(self) -> None:
        with self.assertRaisesRegex(BudgetError, "changed-plan"):
            self.ledger.register(plan_sha256="b" * 64, gates={"node": "gate"})
        with self.assertRaisesRegex(BudgetError, "gate-change"):
            self.ledger.reserve(node_id="node", gate="other")

    def test_changed_plan_relief_is_digest_bound_and_consumed(self) -> None:
        changed_plan = "b" * 64
        self.ledger.reset(
            node_id="node", reason="operator approved revised plan",
            accept_plan_sha256=changed_plan,
        )
        with self.assertRaisesRegex(BudgetError, "changed-plan"):
            self.ledger.register(plan_sha256="c" * 64, gates={"node": "gate"})
        self.ledger.register(plan_sha256=changed_plan, gates={"node": "gate"})
        with self.assertRaisesRegex(BudgetError, "changed-plan"):
            self.ledger.register(plan_sha256="c" * 64, gates={"node": "gate"})

        events = [json.loads(line) for line in self.ledger.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "registered")
        self.assertTrue(events[-1]["consumes_plan_change_authorization"])

    def test_fold_rejects_event_from_a_different_lineage(self) -> None:
        event = json.loads(self.ledger.path.read_text(encoding="utf-8").splitlines()[0])
        event["lineage_id"] = "other-lineage"
        self.ledger.path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BudgetError, "ledger is corrupt"):
            self.ledger.reserve(node_id="node", gate="gate")

    def test_gate_change_relief_is_consumed_by_the_authorized_change(self) -> None:
        self.ledger.reset(
            node_id="node", reason="operator reviewed changed verification gate",
            accept_gate_change=True,
        )
        # Registration is the path PlanGraph takes before a resumed run, so it
        # must be able to consume the operator authorization itself.
        self.ledger.register(plan_sha256="a" * 64, gates={"node": "other"})
        self.ledger.reserve(node_id="node", gate="other")
        with self.assertRaisesRegex(BudgetError, "gate-change"):
            self.ledger.reserve(node_id="node", gate="third")

    def test_operator_can_authorize_one_unknown_node_during_reregistration(self) -> None:
        with self.assertRaisesRegex(BudgetError, "operator relief required"):
            self.ledger.register(plan_sha256="a" * 64, gates={"node": "gate", "added": "new-gate"})

        with self.assertRaisesRegex(BudgetError, "boolean gate-change authorization"):
            self.ledger.reset(
                node_id="added", reason="operator approval must be explicit",
                accept_gate_change="true",  # type: ignore[arg-type]
            )

        self.ledger.reset(
            node_id="added", reason="operator approved the newly planned node",
            accept_gate_change=True,
        )
        self.ledger.register(plan_sha256="a" * 64, gates={"node": "gate", "added": "new-gate"})
        self.ledger.reserve(node_id="added", gate="new-gate")

        self.ledger.reset(
            node_id="another", reason="operator must register a new node first",
            accept_gate_change=True,
        )
        with self.assertRaisesRegex(BudgetError, "not registered"):
            self.ledger.reserve(node_id="another", gate="another-gate")

    def test_rejected_unknown_node_does_not_consume_another_nodes_relief(self) -> None:
        self.ledger.reset(
            node_id="node", reason="operator approved this gate replacement",
            accept_gate_change=True,
        )
        with self.assertRaisesRegex(BudgetError, "operator relief required"):
            self.ledger.register(
                plan_sha256="a" * 64,
                gates={"node": "replacement-gate", "unapproved": "new-gate"},
            )

        # The rejected registration must not persist the first change or
        # consume its authorization; the explicit relief remains usable.
        self.ledger.register(plan_sha256="a" * 64, gates={"node": "replacement-gate"})
        self.ledger.reserve(node_id="node", gate="replacement-gate")

    def test_interrupted_reservations_become_abandoned_and_taxonomy_is_durable(self) -> None:
        reservation = self.ledger.reserve(
            node_id="node", gate="gate", failure_reason="launcher disappeared"
        )
        self.ledger.started(reservation)
        self.assertEqual(
            self.ledger.abandon(
                node_id="node", disposition="blocked", reason="audit reconciliation"
            ),
            (reservation,),
        )
        event_lines = self.ledger.path.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in event_lines]
        reserved = next(event for event in events if event["event"] == "reserved")
        abandoned = next(event for event in events if event["event"] == "abandoned")
        self.assertEqual(reserved["classification"], "indeterminate")
        self.assertTrue(reserved["failure_keys"][0].startswith("reason:"))
        self.assertEqual(abandoned["reservation_id"], reservation)
        self.assertEqual(abandoned["disposition"], "blocked")
        self.assertIsNone(abandoned["graph_attempt_id"])

    def test_reconcile_attempt_records_terminal_disposition_and_attempt(self) -> None:
        reservation = self.ledger.reserve(
            node_id="node", gate="gate", graph_attempt_id="attempt-1",
            classification="infrastructure_transient", failure_keys=("timeout",),
        )
        self.ledger.started(reservation)
        self.assertEqual(
            self.ledger.reconcile_attempt(
                graph_attempt_id="attempt-1", disposition="abandoned", reason="controller restart"
            ),
            (reservation,),
        )
        event = json.loads(self.ledger.path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["event"], "abandoned")
        self.assertEqual(event["graph_attempt_id"], "attempt-1")
        self.assertEqual(event["disposition"], "abandoned")

    def test_failure_key_limit_applies_to_all_retry_classifications(self) -> None:
        ledger = RetryBudgetLedger(self.root, "taxonomy", BudgetConfig(finding_key_limit=1, infra_limit=2))
        ledger.register(plan_sha256="a" * 64, gates={"node": "gate"})
        ledger.reserve(node_id="node", gate="gate", classification="infrastructure_transient", failure_keys=("network",))
        with self.assertRaisesRegex(BudgetError, "finding"):
            ledger.reserve(node_id="node", gate="gate", classification="infrastructure_transient", failure_keys=("network",))

    def test_mixed_classes_cannot_bypass_the_node_launch_budget(self) -> None:
        self.ledger.reserve(node_id="node", gate="gate", classification="product")
        self.ledger.reserve(node_id="node", gate="gate", classification="infrastructure_transient")
        with self.assertRaisesRegex(BudgetError, "exhausted"):
            self.ledger.reserve(node_id="node", gate="gate", classification="structural_decision")

    def test_reservation_persists_distinct_attempt_counters(self) -> None:
        self.ledger.reserve(node_id="node", gate="gate", classification="structural_decision")
        event = json.loads(self.ledger.path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(
            event["attempt_counters"],
            {
                "graph_launches": 1,
                "gate_invocations": 0,
                "repair_dispatches": 0,
                "structural_decisions": 1,
            },
        )
        # Folding a new ledger instance validates and reconstructs the durable
        # counter record rather than relying on the original in-memory object.
        reopened = RetryBudgetLedger(self.root, "shared", BudgetConfig(node_gate_limit=2))
        with reopened._locked(shared=True) as handle:
            state = reopened._fold(handle)
        self.assertEqual(state["nodes"]["node"]["attempt_counters"]["graph_launches"], 1)

    def test_reset_carryover_preserves_other_nodes_finding_budget(self) -> None:
        ledger = RetryBudgetLedger(self.root, "reset-scope", BudgetConfig(finding_key_limit=1))
        ledger.register(plan_sha256="a" * 64, gates={"one": "gate", "two": "gate"})
        ledger.reserve(node_id="one", gate="gate", failure_keys=("shared",))
        ledger.reset(node_id="two", reason="operator reset unrelated node", carryover="reset")
        with self.assertRaisesRegex(BudgetError, "finding"):
            ledger.reserve(node_id="two", gate="gate", failure_keys=("shared",))

    def test_reconcile_attempt_preserves_audited_live_nodes(self) -> None:
        ledger = RetryBudgetLedger(self.root, "reconciliation", BudgetConfig())
        ledger.register(plan_sha256="a" * 64, gates={"node": "gate", "other": "gate"})
        live = ledger.reserve(node_id="node", gate="gate", graph_attempt_id="attempt")
        ledger.started(live)
        stale = ledger.reserve(node_id="other", gate="gate", graph_attempt_id="attempt")
        self.assertEqual(
            ledger.reconcile_attempt(
                graph_attempt_id="attempt", disposition="abandoned", reason="recovery", live_node_ids=("node",)
            ),
            (stale,),
        )


if __name__ == "__main__":
    unittest.main()
