"""Focused acceptance coverage for the external RB-06 coordinator."""
from __future__ import annotations

import tempfile
import json
from pathlib import Path
import subprocess
import sys
import unittest

from harness_labs.plangraph.plan_graph import FeatureRunOutcome, PlanGraph, persist_registration, register_plan_graph
from scripts.plan_graph_recover import RecoveryCoordinator
from scripts.run_plan_graph import _parser as _run_plan_graph_parser


class _CoordinatorFixture(unittest.TestCase):
    """One registered single-node lineage in a throwaway Git repository."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        subprocess.run(["git", "init"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Tests"], cwd=self.repository, check=True)
        (self.repository / "plan.md").write_text("AC-1", encoding="utf-8")
        subprocess.run(["git", "add", "plan.md"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-m", "plan"], cwd=self.repository, check=True, capture_output=True)
        base_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repository, check=True, capture_output=True, text=True).stdout.strip()
        self.run_root = self.root / "runs"
        decomposition = {"plan": "plan.md", "base_commit": base_commit, "runs": [{"id": "node", "objective": "AC-1", "plan_sections": ["1"], "criteria": ["AC-1"]}], "plan_sections": {"1": "AC-1"}, "acceptance_criteria": {"AC-1": "AC-1"}}
        authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["resume", "extend_budget"], "max_extra_node_launches": 1, "max_structural_decisions": 0}
        self.registration = register_plan_graph(repository=self.repository, logical_graph_id="logical", decomposition=decomposition, automatic_recovery=authority)
        self.registration_path = persist_registration(repository=self.repository, registration_root=self.root / "registrations", registration=self.registration)
        PlanGraph(self.repository, self.registration, lambda request: None, run_root=self.run_root, graph_run_id="attempt")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def escalation(self, classification="product", reason="assertion failed"):
        return {"protocol": "plan-graph-block-escalation/1", "graph_run_id": "attempt", "logical_graph_id": "logical", "blocked_node_id": "node", "status_flags": {}, "nodes": [{"node_id": "node", "classification": classification, "reason": reason}], "budget_state": {}, "significance_guidance": {"AC-1": "AC-1"}, "resume_directive_template": {"logical_graph_id": "logical", "predecessor_attempt_id": "attempt", "retry_frontier": ["node"]}, "_reference": "artifact:sha256:" + "a" * 64}

    def coordinator(self, calls):
        value = RecoveryCoordinator(repository=self.repository, registration=self.registration, run_root=self.run_root, runner=lambda argv: calls.append(tuple(argv)) or 0)
        value.set_registration_path(self.registration_path)
        return value


class RecoveryCoordinatorTests(_CoordinatorFixture):
    def test_tier_one_resume_is_durable_then_runs_a_fresh_cli_process(self) -> None:
        calls = []
        result = self.coordinator(calls).recover(self.escalation(), launcher_argv=["echo", "launch"])
        self.assertEqual(result.status, "resumed")
        self.assertEqual(result.decision["action"], "resume")
        self.assertEqual(len(calls), 1)
        self.assertIn("run_plan_graph.py", calls[0][1])
        self.assertIn("--resume", calls[0])

    def test_human_tier_and_repeat_decision_never_fabricate_success(self) -> None:
        calls = []
        coordinator = self.coordinator(calls)
        self.assertEqual(coordinator.recover(self.escalation("product", "permission denied"), launcher_argv=["echo"]).status, "requires_human")
        self.assertEqual(coordinator.recover(self.escalation(), launcher_argv=["echo"]).status, "resumed")
        self.assertEqual(coordinator.recover(self.escalation(), launcher_argv=["echo"]).decision["action"], "extend_budget")
        self.assertEqual(coordinator.recover(self.escalation(), launcher_argv=["echo"]).status, "externally_blocked")
        self.assertEqual(len(calls), 2)

    def test_recovery_refuses_guidance_not_bound_to_the_registered_target(self) -> None:
        calls = []
        escalation = self.escalation(classification="product", reason="assertion failed")
        escalation["significance_guidance"] = {"AC-1": "altered acceptance criterion"}

        result = self.coordinator(calls).recover(escalation, launcher_argv=["echo"])

        self.assertEqual(result.status, "externally_blocked")
        self.assertIn("does not match the registered acceptance criteria", result.reason)
        self.assertIsNone(result.decision)
        self.assertEqual(calls, [])

    def test_registered_guidance_and_failure_evidence_override_node_classification(self) -> None:
        calls = []

        result = self.coordinator(calls).recover(
            self.escalation("structural_decision", "assertion failed"),
            launcher_argv=["echo"],
        )

        self.assertEqual(result.status, "resumed")
        self.assertEqual(result.decision["action"], "resume")
        self.assertEqual(len(calls), 1)

    def test_external_cli_recovery_reports_a_fresh_process_block_truthfully(self) -> None:
        predecessor = PlanGraph(
            self.repository, self.registration,
            lambda request: FeatureRunOutcome("blocked", evidence={"error": "assertion failed"}),
            run_root=self.run_root, graph_run_id="attempt",
        )
        self.assertEqual(predecessor.run().status, "blocked")
        escalation_path = self.run_root / "attempt" / "escalation.json"
        launcher_path = self.root / "successful_launcher.py"
        launcher_path.write_text(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "print(json.dumps({'status': 'failed'}))\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "scripts" / "plan_graph_recover.py"),
             str(escalation_path), "--repository", str(self.repository), "--registration",
             str(self.registration_path), "--run-root", str(self.run_root), "--launcher-command",
             sys.executable, str(launcher_path)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        fresh_process, coordinator_result = completed.stdout.splitlines()
        self.assertEqual(json.loads(fresh_process)["status"], "failed")
        result = json.loads(coordinator_result)
        self.assertEqual(result["status"], "externally_blocked")
        self.assertEqual(result["resume_returncode"], 1)
        self.assertIn("--resume", result["resume_argv"])


class MultiNodeRetryFrontierTests(_CoordinatorFixture):
    """``continue_independent_after_block`` makes a multi-node frontier normal.

    The coordinator used to refuse any frontier that did not have exactly one
    element, so turning that flag on disabled automatic recovery outright.
    Behind that guard sat a second defect the guard made unreachable: the
    resume argv splatted the frontier after a single ``--retry-frontier``,
    which ``run_plan_graph.py`` declares ``action="append"``.
    """

    def setUp(self) -> None:
        super().setUp()
        base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repository, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        decomposition = {
            "plan": "plan.md", "base_commit": base_commit,
            "runs": [
                {"id": "node", "objective": "AC-1", "plan_sections": ["1"], "criteria": ["AC-1"]},
                {"id": "sibling", "objective": "AC-2", "plan_sections": ["1"], "criteria": ["AC-2"]},
            ],
            "plan_sections": {"1": "AC-1"},
            "acceptance_criteria": {"AC-1": "AC-1", "AC-2": "AC-2"},
        }
        authority = {
            "protocol": "plan-graph-automatic-recovery/1",
            "allowed_actions": ["resume", "extend_budget"],
            "max_extra_node_launches": 2, "max_structural_decisions": 0,
        }
        self.pair_run_root = self.root / "pair-runs"
        self.pair_registration = register_plan_graph(
            repository=self.repository, logical_graph_id="pair",
            decomposition=decomposition, plan_lineage_id="pair",
            automatic_recovery=authority,
        )
        self.pair_registration_path = persist_registration(
            repository=self.repository,
            registration_root=self.root / "pair-registrations",
            registration=self.pair_registration,
        )
        # Registers both nodes' gates in the lineage ledger.
        PlanGraph(self.repository, self.pair_registration, lambda request: None,
                  run_root=self.pair_run_root, graph_run_id="pair-attempt")

    def pair_escalation(self, frontier=("node", "sibling"), reason="assertion failed"):
        return {
            "protocol": "plan-graph-block-escalation/1", "graph_run_id": "pair-attempt",
            "logical_graph_id": "pair", "blocked_node_id": "node", "status_flags": {},
            "nodes": [
                {"node_id": "node", "classification": "product", "reason": reason},
                {"node_id": "sibling", "classification": "product", "reason": reason},
            ],
            "budget_state": {},
            "significance_guidance": {"AC-1": "AC-1", "AC-2": "AC-2"},
            "resume_directive_template": {
                "logical_graph_id": "pair", "predecessor_attempt_id": "pair-attempt",
                "retry_frontier": list(frontier),
            },
            "_reference": "artifact:sha256:" + "b" * 64,
        }

    def pair_coordinator(self, calls):
        value = RecoveryCoordinator(
            repository=self.repository, registration=self.pair_registration,
            run_root=self.pair_run_root,
            runner=lambda argv: calls.append(tuple(argv)) or 0,
        )
        value.set_registration_path(self.pair_registration_path)
        return value

    def test_a_two_node_frontier_is_recovered_instead_of_refused(self) -> None:
        calls = []

        result = self.pair_coordinator(calls).recover(
            self.pair_escalation(), launcher_argv=["echo", "launch"]
        )

        self.assertEqual(result.status, "resumed", result.reason)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [decision["target"] for decision in result.decisions], ["node", "sibling"]
        )
        self.assertEqual({decision["action"] for decision in result.decisions}, {"resume"})
        # The published single-decision field stays the primary blocker's.
        self.assertEqual(result.decision["target"], "node")

    def test_every_frontier_node_survives_the_resume_argv_round_trip(self) -> None:
        """The argv must parse back into the frontier it was built from."""
        calls = []

        self.pair_coordinator(calls).recover(
            self.pair_escalation(), launcher_argv=["echo", "launch"]
        )

        self.assertEqual(len(calls), 1)
        argv = list(calls[0])
        arguments = _run_plan_graph_parser().parse_args(argv[2:])
        self.assertEqual(arguments.retry_frontier, ["node", "sibling"])

    def test_a_frontier_node_absent_from_the_plan_blocks_the_whole_frontier(self) -> None:
        calls = []

        result = self.pair_coordinator(calls).recover(
            self.pair_escalation(frontier=("node", "ghost")), launcher_argv=["echo"]
        )

        self.assertEqual(result.status, "externally_blocked")
        self.assertIn("'ghost'", result.reason)
        self.assertEqual(calls, [])
        self.assertEqual(result.decisions, ())

    def test_a_human_tier_node_anywhere_in_the_frontier_stops_the_attempt(self) -> None:
        calls = []
        escalation = self.pair_escalation()
        escalation["nodes"][1]["reason"] = "permission denied"

        result = self.pair_coordinator(calls).recover(escalation, launcher_argv=["echo"])

        self.assertEqual(result.status, "requires_human")
        self.assertIn("sibling", result.reason)
        self.assertEqual(calls, [])

    def test_the_extend_budget_allowance_is_checked_for_the_whole_frontier(self) -> None:
        """An append-only ledger cannot roll back a half-applied frontier."""
        calls = []
        coordinator = self.pair_coordinator(calls)
        self.assertEqual(
            coordinator.recover(self.pair_escalation(), launcher_argv=["echo"]).status,
            "resumed",
        )
        # The allowance is 2 and the frontier is 2 wide, so one extension
        # round fits exactly and a second cannot be covered.
        second = coordinator.recover(self.pair_escalation(), launcher_argv=["echo"])
        self.assertEqual(second.status, "resumed", second.reason)
        self.assertEqual({item["action"] for item in second.decisions}, {"extend_budget"})
        third = coordinator.recover(self.pair_escalation(), launcher_argv=["echo"])
        self.assertEqual(third.status, "externally_blocked")


if __name__ == "__main__":
    unittest.main()
