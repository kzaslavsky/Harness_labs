from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness_labs.observability import improvement_index
from harness_labs.observability.improvement_index import (
    AntiThrashDecision,
    cluster_observations,
    close_proposal,
    evaluate_anti_thrash,
    is_proposable,
    open_proposal,
    regress_proposal,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "improvement" / "index" / "observations.json"
NOW = "2026-08-21T00:00:00Z"


def _load_fixture() -> dict[str, list[dict]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _pattern_for_signature(patterns: list[dict], signature: str) -> dict:
    matches = [pattern for pattern in patterns if pattern["signature"] == signature]
    assert len(matches) == 1, f"expected exactly one pattern for {signature!r}, found {len(matches)}"
    return matches[0]


class ClusteringThresholdTests(unittest.TestCase):
    """AC-SI03-1: signature+classification clustering with anti-N=1 thresholds."""

    def setUp(self) -> None:
        self.fixture = _load_fixture()

    def test_single_run_multiattempt_stays_observed_forever(self) -> None:
        """Operator ruling: 3+ observations across attempts of ONE run_id
        must never cross the candidate gate, no matter how many attempts."""

        observations = self.fixture["single_run_multiattempt"]
        self.assertEqual(len({o["run_id"] for o in observations}), 1)
        self.assertGreaterEqual(len(observations), 4)

        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-single-run")
        self.assertEqual(pattern["support"]["observation_count"], 4)
        self.assertEqual(pattern["support"]["distinct_run_count"], 1)
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 1)
        self.assertEqual(pattern["status"], "observed")

    def test_two_distinct_runs_and_three_observations_becomes_candidate(self) -> None:
        observations = self.fixture["two_run_candidate"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-two-run")
        self.assertEqual(pattern["support"]["observation_count"], 3)
        self.assertEqual(pattern["support"]["distinct_run_count"], 2)
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 2)
        self.assertEqual(pattern["status"], "candidate")

    def test_two_distinct_runs_but_too_few_observations_stays_observed(self) -> None:
        observations = self.fixture["insufficient_observation_count"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-too-few")
        self.assertEqual(pattern["support"]["distinct_run_count"], 2)
        self.assertEqual(pattern["support"]["observation_count"], 2)
        self.assertEqual(pattern["status"], "observed")

    def test_grouping_is_by_signature_plus_classification_not_signature_alone(self) -> None:
        shared_signature_different_classification = [
            {**self.fixture["two_run_candidate"][0], "classification": "product"},
            self.fixture["two_run_candidate"][0],
        ]
        patterns = cluster_observations(shared_signature_different_classification, now=NOW)
        signatures_and_classes = {(p["signature"], p["classification"]) for p in patterns}
        self.assertEqual(
            signatures_and_classes,
            {("sig-two-run", "product"), ("sig-two-run", "policy_violation")},
        )

    def test_candidate_is_proposable_when_lineages_span_multiple_runs(self) -> None:
        observations = self.fixture["two_run_candidate"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-two-run")
        self.assertTrue(is_proposable(pattern))

    def test_proposable_gate_also_satisfied_by_distinct_task_suites(self) -> None:
        observations = self.fixture["proposable_task_suite_diverse"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-suite-diverse")
        self.assertEqual(pattern["status"], "candidate")
        self.assertGreaterEqual(pattern["support"]["distinct_task_suite_count"], 2)
        self.assertTrue(is_proposable(pattern))

    def test_proposable_task_suite_branch_is_load_bearing_on_its_own(self) -> None:
        """The fixture-driven test above cannot fail through the
        task-suite branch alone, since its lineages already satisfy the
        OR (see finding tests/test_improvement_index.py:
        task-suite-branch-untested). Call ``is_proposable`` directly on a
        synthetic pattern with a single lineage but two distinct task
        suites to prove that branch is independently sufficient."""

        pattern = {
            "status": "candidate",
            "support": {
                "observation_count": 3,
                "distinct_run_count": 2,
                "distinct_lineage_count": 1,
                "distinct_task_suite_count": 2,
            },
        }
        self.assertTrue(is_proposable(pattern))

    def test_proposable_gate_fails_when_neither_lineages_nor_suites_reach_two(self) -> None:
        pattern = {
            "status": "candidate",
            "support": {
                "observation_count": 3,
                "distinct_run_count": 2,
                "distinct_lineage_count": 1,
                "distinct_task_suite_count": 1,
            },
        }
        self.assertFalse(is_proposable(pattern))

    def test_non_candidate_pattern_is_never_proposable(self) -> None:
        observations = self.fixture["single_run_multiattempt"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-single-run")
        self.assertFalse(is_proposable(pattern))

    def test_clustering_is_deterministic_and_order_independent(self) -> None:
        observations = list(self.fixture["two_run_candidate"])
        forward = cluster_observations(observations, now=NOW)
        backward = cluster_observations(list(reversed(observations)), now=NOW)
        self.assertEqual(forward, backward)


class LineageKeyingTests(unittest.TestCase):
    """Incident-lineage identity: retries of one logical node across graph
    attempts are ONE lineage, not N independent observations.

    A PlanGraph node that retries mints a fresh ``run_id`` per graph
    attempt (``...-attempt-3-SI-06``, ``...-attempt-4-SI-06``,
    ``...-attempt-5-SI-06`` are three run ids for one logical node
    retrying one defect). Counting raw run ids promoted such a single
    defect to ``candidate`` off its own retry storm -- inflating exactly
    the anti-N=1 thresholds that gate proposal admission. The status gate
    therefore uses ``distinct_lineage_count`` (folded), while
    ``distinct_run_count`` stays the honest raw run-id stat.
    """

    def setUp(self) -> None:
        self.fixture = _load_fixture()

    def test_node_retry_chain_across_graph_attempts_is_one_lineage(self) -> None:
        observations = self.fixture["node_retry_chain_across_graph_attempts"]
        self.assertEqual(len({o["run_id"] for o in observations}), 3)

        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-node-retry")
        support = pattern["support"]
        self.assertEqual(support["observation_count"], 3)
        self.assertEqual(support["distinct_run_count"], 3, "raw run-id count stays honest")
        self.assertEqual(support["distinct_lineage_count"], 1)
        self.assertEqual(pattern["status"], "observed")
        self.assertFalse(is_proposable(pattern))

    def test_distinct_nodes_of_one_graph_are_distinct_lineages(self) -> None:
        """The counterpart ground truth: a pattern spanning genuinely
        different logical nodes still reaches ``candidate``."""

        observations = self.fixture["distinct_nodes_of_one_graph"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-cross-node")
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 3)
        self.assertEqual(pattern["status"], "candidate")

    def test_graph_level_retry_chain_is_one_lineage(self) -> None:
        observations = self.fixture["graph_level_retry_chain"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-graph-retry")
        self.assertEqual(pattern["support"]["distinct_run_count"], 3)
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 1)
        self.assertEqual(pattern["status"], "observed")

    def test_node_id_correlation_takes_precedence_over_the_run_id_suffix(self) -> None:
        """When the miner populates ``node_id`` (event correlation), it
        supplies the node component even for graph-level run ids -- so a
        node's retries fold whether or not the run id carries a suffix."""

        observations = self.fixture["node_id_enriched_retry_chain"]
        self.assertEqual({o["node_id"] for o in observations}, {"SI-06"})
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-enriched-retry")
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 1)
        self.assertEqual(pattern["status"], "observed")

    def test_node_id_correlation_keeps_distinct_nodes_apart(self) -> None:
        observations = self.fixture["node_id_enriched_distinct_nodes"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-enriched-cross")
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 3)
        self.assertEqual(pattern["status"], "candidate")

    def test_same_node_id_in_different_logical_graphs_never_folds(self) -> None:
        """The fold is scoped to one logical graph: node ``SI-06`` of
        ``graph-alpha`` and of ``graph-beta`` are independent incidents,
        while ``graph-beta``'s own two attempts are one."""

        observations = self.fixture["distinct_logical_graphs_same_node_id"]
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-cross-graph")
        self.assertEqual(pattern["support"]["distinct_run_count"], 3)
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 2)
        self.assertEqual(pattern["status"], "candidate")

    def test_unparseable_run_ids_fall_back_to_the_run_itself(self) -> None:
        """Fallback when no correlation is recoverable: attempts inside
        one ``run_id`` fold (they are one run by construction), and two
        different run ids never merge. Reconciles the prior
        ``(run_id, attempt_id)`` keying -- intra-run attempt ids stay
        distinct only when they belong to genuinely different runs."""

        observations = self.fixture["unparseable_run_ids_stay_distinct"]
        self.assertEqual(len({o["attempt_id"] for o in observations}), 3)
        patterns = cluster_observations(observations, now=NOW)
        pattern = _pattern_for_signature(patterns, "sig-unparseable")
        self.assertEqual(pattern["support"]["observation_count"], 3)
        self.assertEqual(pattern["support"]["distinct_run_count"], 2)
        self.assertEqual(pattern["support"]["distinct_lineage_count"], 2)
        self.assertEqual(pattern["status"], "candidate")

    def test_lineage_count_never_exceeds_the_raw_run_count(self) -> None:
        every_observation = [
            observation for group in self.fixture.values() for observation in group
        ]
        for pattern in cluster_observations(every_observation, now=NOW):
            support = pattern["support"]
            self.assertLessEqual(
                support["distinct_lineage_count"],
                support["distinct_run_count"],
                msg=f"lineage fold must never invent support: {pattern['signature']!r}",
            )

    def test_lineage_key_is_stable_and_folds_only_the_attempt_ordinal(self) -> None:
        from harness_labs.observability.improvement_index import _incident_lineage_key

        def key(run_id: str, node_id: str | None = None) -> tuple[str, ...]:
            return _incident_lineage_key({"run_id": run_id, "node_id": node_id})

        self.assertEqual(key("g-attempt-3-SI-06"), key("g-attempt-40-SI-06"))
        self.assertNotEqual(key("g-attempt-3-SI-06"), key("g-attempt-3-SI-05"))
        self.assertNotEqual(key("g-attempt-3-SI-06"), key("h-attempt-3-SI-06"))
        self.assertNotEqual(key("g-attempt-3-SI-06"), key("g-attempt-3"))
        self.assertNotEqual(key("legacy-a"), key("legacy-b"))


class SupportAndCostAggregateTests(unittest.TestCase):
    """AC-SI03-2: support counts and a median-plus-tail cost aggregate."""

    def setUp(self) -> None:
        self.fixture = _load_fixture()
        self.patterns = cluster_observations(self.fixture["cost_aggregate_group"], now=NOW)
        self.pattern = _pattern_for_signature(self.patterns, "sig-cost")

    def test_support_counts_are_correct(self) -> None:
        support = self.pattern["support"]
        self.assertEqual(support["observation_count"], 5)
        self.assertEqual(support["distinct_run_count"], 5)
        self.assertEqual(support["distinct_lineage_count"], 5)
        self.assertEqual(support["distinct_task_suite_count"], 2)

    def test_wall_clock_cost_aggregate_is_median_and_tail(self) -> None:
        wall_clock = self.pattern["cost_aggregate"]["wall_clock_ms"]
        self.assertEqual(wall_clock["median"], 300.0)
        self.assertEqual(wall_clock["tail"], 500.0)

    def test_diff_churn_cost_aggregate_is_median_and_tail(self) -> None:
        diff_churn = self.pattern["cost_aggregate"]["diff_churn_lines"]
        self.assertEqual(diff_churn["median"], 3.0)
        self.assertEqual(diff_churn["tail"], 5.0)

    def test_null_tokens_are_excluded_from_the_token_aggregate(self) -> None:
        """Two of the five fixture observations carry tokens: null; only the
        three non-null values (10, 20, 30) may feed the aggregate."""

        tokens = self.pattern["cost_aggregate"]["tokens"]
        self.assertEqual(tokens["median"], 20.0)
        self.assertEqual(tokens["tail"], 30.0)

    def test_empty_observation_group_cost_stat_is_zero_not_an_error(self) -> None:
        from harness_labs.observability.improvement_index import _cost_stat

        self.assertEqual(_cost_stat([]), {"median": 0.0, "tail": 0.0})


class ImportBoundaryTests(unittest.TestCase):
    """AC-SI03-2: the module imports only core, observability, and stdlib."""

    def test_module_has_no_plangraph_import(self) -> None:
        import ast

        source = Path(improvement_index.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        for module_name in imported_modules:
            self.assertFalse(
                module_name.startswith("harness_labs.plangraph")
                or module_name == "harness_labs.plangraph",
                msg=f"forbidden plangraph import: {module_name!r}",
            )

    def test_repository_import_boundary_checker_is_green(self) -> None:
        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "dev" / "check_import_boundaries.py")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stdout + completed.stderr)


class AntiThrashLedgerTests(unittest.TestCase):
    """AC-SI03-3: per-surface uniqueness, cooldown, hard cap, rejected-history bar."""

    def test_one_open_proposal_per_target_surface(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        decision = evaluate_anti_thrash(
            ledger, target_surface="docs/foo.md", pattern_id="pattern-b", observation_count=5, now=NOW
        )
        self.assertFalse(decision.allowed)
        self.assertIn("surface_already_open", decision.reasons)

    def test_distinct_surface_is_unaffected_by_another_surfaces_open_proposal(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        decision = evaluate_anti_thrash(
            ledger, target_surface="docs/bar.md", pattern_id="pattern-b", observation_count=3, now=NOW
        )
        self.assertTrue(decision.allowed)

    def test_cooldown_blocks_reproposal_before_window_elapses(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="closed",
            observation_count=3,
            now="2026-08-21T00:00:00Z",
        )
        soon_after = "2026-08-22T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now=soon_after,
            cooldown_days=14,
            cooldown_runs=5,
            new_runs_since_last_close=0,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("cooldown_active", decision.reasons)

    def test_cooldown_clears_after_the_day_window_elapses(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="closed",
            observation_count=3,
            now="2026-08-21T00:00:00Z",
        )
        much_later = "2026-09-10T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now=much_later,
            cooldown_days=14,
            cooldown_runs=5,
            new_runs_since_last_close=0,
        )
        self.assertTrue(decision.allowed)

    def test_cooldown_clears_early_via_enough_new_runs_even_before_the_day_window(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="closed",
            observation_count=3,
            now="2026-08-21T00:00:00Z",
        )
        soon_after = "2026-08-22T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now=soon_after,
            cooldown_days=14,
            cooldown_runs=5,
            new_runs_since_last_close=5,
        )
        self.assertTrue(decision.allowed)

    def test_hard_cap_on_open_proposals(self) -> None:
        ledger: list[dict] = []
        for index in range(3):
            ledger = open_proposal(
                ledger,
                target_surface=f"docs/surface-{index}.md",
                pattern_id=f"pattern-{index}",
                observation_count=3,
                now=NOW,
            )
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/surface-new.md",
            pattern_id="pattern-new",
            observation_count=3,
            now=NOW,
            max_open_proposals=3,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("open_proposal_cap_reached", decision.reasons)

    def test_hard_cap_does_not_block_when_under_the_limit(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/surface-0.md", pattern_id="pattern-0", observation_count=3, now=NOW
        )
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/surface-new.md",
            pattern_id="pattern-new",
            observation_count=3,
            now=NOW,
            max_open_proposals=3,
        )
        self.assertTrue(decision.allowed)

    def test_rejected_pattern_bar_blocks_reproposal_without_new_observations(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="rejected",
            observation_count=3,
            now="2026-01-01T00:00:00Z",
        )
        much_later = "2026-06-01T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now=much_later,
            cooldown_days=14,
            cooldown_runs=5,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("rejected_without_new_observations", decision.reasons)

    def test_rejected_pattern_bar_lifts_once_observation_count_grows(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="rejected",
            observation_count=3,
            now="2026-01-01T00:00:00Z",
        )
        much_later = "2026-06-01T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=4,
            now=much_later,
            cooldown_days=14,
            cooldown_runs=5,
        )
        self.assertTrue(decision.allowed)

    def test_regressed_proposal_bars_reproposal_without_new_observations(self) -> None:
        """A closed (accepted) proposal that later recurs in production is
        recorded via regress_proposal, and the resulting 'regressed' entry
        bars re-proposal exactly like an explicit rejection (plan SI-03
        'rejected/regressed history bar'; SI-05 lines 279-281)."""

        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="closed",
            observation_count=3,
            now="2026-01-01T00:00:00Z",
        )
        ledger = regress_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now="2026-02-01T00:00:00Z",
        )
        much_later = "2026-06-01T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now=much_later,
            cooldown_days=14,
            cooldown_runs=5,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("rejected_without_new_observations", decision.reasons)

    def test_regressed_proposal_bar_lifts_once_observation_count_grows(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        ledger = close_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            disposition="closed",
            observation_count=3,
            now="2026-01-01T00:00:00Z",
        )
        ledger = regress_proposal(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now="2026-02-01T00:00:00Z",
        )
        much_later = "2026-06-01T00:00:00Z"
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=4,
            now=much_later,
            cooldown_days=14,
            cooldown_runs=5,
        )
        self.assertTrue(decision.allowed)

    def test_regress_proposal_raises_when_no_matching_closed_entry(self) -> None:
        with self.assertRaises(ValueError):
            regress_proposal(
                [],
                target_surface="docs/missing.md",
                pattern_id="pattern-z",
                observation_count=1,
                now=NOW,
            )

    def test_open_proposal_raises_when_anti_thrash_gate_refuses(self) -> None:
        ledger = open_proposal(
            [], target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        with self.assertRaises(ValueError):
            open_proposal(
                ledger,
                target_surface="docs/foo.md",
                pattern_id="pattern-b",
                observation_count=5,
                now=NOW,
            )

    def test_close_proposal_raises_when_no_matching_open_entry(self) -> None:
        with self.assertRaises(ValueError):
            close_proposal(
                [],
                target_surface="docs/missing.md",
                pattern_id="pattern-z",
                disposition="closed",
                observation_count=1,
                now=NOW,
            )

    def test_evaluate_anti_thrash_returns_all_violated_reasons_at_once(self) -> None:
        ledger: list[dict] = []
        for index in range(2):
            ledger = open_proposal(
                ledger,
                target_surface=f"docs/surface-{index}.md",
                pattern_id=f"pattern-{index}",
                observation_count=3,
                now=NOW,
            )
        ledger = open_proposal(
            ledger, target_surface="docs/foo.md", pattern_id="pattern-a", observation_count=3, now=NOW
        )
        decision = evaluate_anti_thrash(
            ledger,
            target_surface="docs/foo.md",
            pattern_id="pattern-a",
            observation_count=3,
            now=NOW,
            max_open_proposals=3,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("surface_already_open", decision.reasons)
        self.assertIn("open_proposal_cap_reached", decision.reasons)


if __name__ == "__main__":
    unittest.main()
