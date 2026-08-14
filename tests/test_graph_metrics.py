from __future__ import annotations

import unittest

from harness_labs.observability import graph_metrics


def _metrics(
    *,
    input_tokens: int,
    cached_input_tokens: int = 0,
    output_tokens: int,
    calls: int,
    wall_ms: int | None,
    busy_ms: int | None,
    peak: int | None,
    cost_state: str = "available",
    cost_usd: float | None,
    usage_records: int,
    review_cycles: int = 0,
    verification_repairs: int = 0,
    findings_total: int = 0,
    by_model: list | None = None,
) -> dict:
    total_tokens = input_tokens + output_tokens
    return {
        "protocol": "harness-run-detail-metrics/1",
        "totals": {
            "calls": calls,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "duration_ms": wall_ms or 0,
            "wall_clock_ms": wall_ms,
            "busy_ms": busy_ms,
            "peak_input_tokens": peak,
            "cost": {
                "state": cost_state,
                "usd": cost_usd,
                "reason": None if cost_state == "available" else "cost is unavailable for this fixture",
                "sources": ["pricing"] if cost_usd is not None else [],
                "estimated_records": 1 if cost_state == "estimated" else 0,
                "long_context_records": 0,
            },
        },
        "quality": {
            "criteria_total": 1,
            "criteria_satisfied": 1,
            "findings_total": findings_total,
            "open_findings": 0,
            "review_cycles": review_cycles,
            "verification_repairs": verification_repairs,
        },
        "by_phase": [], "by_agent": [], "by_agent_type": [], "by_model": by_model or [], "by_effort": [], "by_backend": [],
        "stages": [],
        "provenance": {
            "usage_records": usage_records,
            "collection_method": "verified backend_transport journal events",
            "peak_context_definition": "peak",
            "busy_time_definition": "busy",
        },
    }


def _node(node_id: str, feature_run_id: str | None, status: str = "succeeded", **extra) -> dict:
    node = {"node_id": node_id, "status": status, "feature_run_id": feature_run_id}
    node.update(extra)
    return node


def _correlated(run_id: str, graph_run_id: str, node_id: str) -> dict:
    return {"run_id": run_id, "correlation": {"plan_graph_id": graph_run_id, "plan_node_id": node_id, "parent_run_id": graph_run_id}}


class MergeDetailMetricsTests(unittest.TestCase):
    """AC-DM01-5: the merged document carries a labelled cumulative-quality
    block alongside latest-try quality and per-try rows."""

    def test_cumulative_quality_sums_across_tries_while_latest_quality_is_retained(self) -> None:
        first = _metrics(input_tokens=10, output_tokens=5, calls=1, wall_ms=100, busy_ms=90, peak=10, cost_usd=0.1, usage_records=1, review_cycles=2, verification_repairs=1, findings_total=3)
        second = _metrics(input_tokens=4, output_tokens=1, calls=1, wall_ms=50, busy_ms=40, peak=4, cost_usd=0.05, usage_records=1, review_cycles=1, verification_repairs=0, findings_total=1)
        merged = graph_metrics.merge_detail_metrics([first, second], ["try-1", "try-2"])

        # Latest-try quality (criteria/open findings are current-state) is retained verbatim.
        self.assertEqual(merged["quality"], second["quality"])
        # The labelled cumulative block sums review cycles / repairs / findings across tries.
        cumulative = merged["cumulative_quality"]
        self.assertEqual(cumulative["review_cycles"], 3)
        self.assertEqual(cumulative["verification_repairs"], 1)
        self.assertEqual(cumulative["findings_total"], 4)
        self.assertEqual(cumulative["try_count"], 2)
        self.assertEqual(cumulative["reason"], "cumulative across 2 tries")
        self.assertEqual(
            cumulative["by_try"],
            [
                {"label": "try-1", "review_cycles": 2, "verification_repairs": 1, "findings_total": 3},
                {"label": "try-2", "review_cycles": 1, "verification_repairs": 0, "findings_total": 1},
            ],
        )
        # Per-try rows (pre-existing behaviour) are preserved unchanged.
        self.assertEqual([row["label"] for row in merged["by_try"]], ["try-1", "try-2"])
        self.assertEqual(merged["totals"]["total_tokens"], 20)

    def test_single_try_reason_uses_singular_grammar(self) -> None:
        only = _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=1, busy_ms=1, peak=1, cost_usd=0.0, usage_records=1)
        merged = graph_metrics.merge_detail_metrics([only], ["try-1"])
        self.assertEqual(merged["cumulative_quality"]["reason"], "cumulative across 1 try")


class DashboardServerDelegationTests(unittest.TestCase):
    """AC-DM01-5: dashboard_server serves the merge through graph_metrics's public functions."""

    def test_dashboard_server_shim_delegates_to_graph_metrics(self) -> None:
        from harness_labs.observability import dashboard_server

        catalog = {"plan_graphs": [{"run_id": "graph-1", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "plan", "nodes": [_node("N1", "try-1")]}]}
        details = {"try-1": {"metrics": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=1, busy_ms=1, peak=1, cost_usd=0.0, usage_records=1)}}
        calls: list[tuple] = []
        original = graph_metrics.apply_cumulative_node_metrics
        graph_metrics.apply_cumulative_node_metrics = lambda *args: (calls.append(args), original(*args))[1]
        try:
            dashboard_server._apply_cumulative_node_metrics(catalog, details)
        finally:
            graph_metrics.apply_cumulative_node_metrics = original
        self.assertEqual(len(calls), 1)
        self.assertIn("cumulative_quality", details["try-1"]["metrics"])

    def test_dashboard_server_no_longer_defines_the_moved_merge_internals(self) -> None:
        from harness_labs.observability import dashboard_server

        for name in ("_merge_detail_metrics", "_merge_metric_totals", "_merge_metric_breakdown", "_node_history_run_ids"):
            self.assertFalse(hasattr(dashboard_server, name), f"{name} should have moved to graph_metrics")


class ComputeGraphMetricsFullDataTests(unittest.TestCase):
    """AC-DM01-1: exact attempt-scoped totals over synthetic children with full data."""

    def _graph(self) -> dict:
        return {
            "run_id": "graph-1",
            "status": "succeeded",
            "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "plan-x",
            "logical_graph_id": "graph-1",
            "nodes": [
                _node("N1", "graph-1-N1"),
                _node("N2", "graph-1-N2"),
                _node("N3", "graph-1-N3"),
            ],
            "execution": {
                "recovery": {
                    "dispositions": [{"node_id": "N-other", "disposition": "sealed", "reason": None, "forced": False, "evidence_refs": []}],
                    "attempt_lineage": [{"attempt_id": "a-1"}, {"attempt_id": "a-2"}],
                    "retry_state": {"invalidations": [{"attempt_id": "a-1"}], "reuse": []},
                },
            },
        }

    def _node_details(self) -> dict:
        return {
            "graph-1-N1": _metrics(input_tokens=100, cached_input_tokens=10, output_tokens=20, calls=3, wall_ms=1000, busy_ms=900, peak=80, cost_usd=0.50, usage_records=3),
            "graph-1-N2": _metrics(input_tokens=40, output_tokens=10, calls=2, wall_ms=500, busy_ms=400, peak=60, cost_usd=0.20, usage_records=2),
            "graph-1-N3": _metrics(input_tokens=60, cached_input_tokens=5, output_tokens=15, calls=2, wall_ms=700, busy_ms=650, peak=70, cost_usd=0.30, usage_records=2),
        }

    def test_exact_attempt_scoped_rollup(self) -> None:
        graph = self._graph()
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = self._node_details()
        budget_ledger = {"state": "available", "reason": None, "graph_launches": 1, "gate_invocations": 3, "repair_dispatches": 1, "structural_decisions": 0}
        own_summary = {"usage": {"wall_clock_ms": 5000}}

        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details, own_summary=own_summary, budget_ledger=budget_ledger)

        self.assertEqual(result["protocol"], "harness-plan-graph-metrics/1")
        totals = result["totals"]
        self.assertEqual(totals["tokens"], {"state": "available", "reason": None, "input_tokens": 200, "cached_input_tokens": 15, "output_tokens": 45, "total_tokens": 245})
        self.assertEqual(totals["cost"], {"state": "available", "usd": 1.0, "reason": None})
        self.assertEqual(totals["calls"], {"state": "available", "value": 7, "reason": None})
        self.assertEqual(totals["agent_busy_ms"], {"state": "available", "value": 1950, "reason": None})
        self.assertEqual(totals["peak_input_tokens"], {"state": "available", "value": 80, "reason": None})
        self.assertEqual(totals["parallelism"], {"state": "available", "value": round(1950 / 5000, 3), "reason": None})

        self.assertEqual(result["timing"]["started_at"], "2026-08-09T00:00:00Z")
        self.assertEqual(result["timing"]["wall_clock_ms"], {"state": "available", "value": 5000, "reason": None})

        self.assertEqual(result["retries"]["budget_ledger"], budget_ledger)
        self.assertEqual(result["retries"]["node_retries"], 0)
        self.assertEqual(result["retries"]["graph_attempts"], 1)

        self.assertEqual(result["recovery"]["dispositions"], graph["execution"]["recovery"]["dispositions"])
        self.assertEqual(result["recovery"]["attempt_lineage_count"], 2)
        self.assertEqual(result["recovery"]["invalidations_count"], 1)

        self.assertEqual(result["blockers"], {"count": 0, "nodes": []})
        self.assertEqual(result["counts"], {"logical_nodes": 3, "feature_run_tries": 3})

        per_node = result["per_feature_run"]
        self.assertEqual(per_node["wall_ms"]["state"], "available")
        self.assertAlmostEqual(per_node["wall_ms"]["mean"], (1000 + 500 + 700) / 3, places=3)
        self.assertEqual(per_node["wall_ms"]["median"], 700)
        self.assertEqual(per_node["wall_ms"]["max"], 1000)
        self.assertEqual(per_node["tokens"]["max"], 120)
        self.assertEqual(per_node["cost_usd"]["max"], 0.50)

        node_ids = [row["node_id"] for row in result["nodes"]]
        self.assertEqual(node_ids, ["N1", "N3", "N2"])  # sorted by cost descending: 0.50, 0.30, 0.20
        self.assertEqual([row["tries"] for row in result["nodes"]], [1, 1, 1])
        # No node reports a per-node wait_ms source yet; the field is present and honestly unavailable.
        self.assertTrue(all(row["wait_ms"]["state"] == "unavailable" for row in result["nodes"]))

        # No dependency edges between these three nodes: critical path is the single longest node.
        self.assertEqual(result["scheduling"]["critical_path_ms"], {"state": "available", "value": 1000, "reason": None})
        # No by_model breakdown data in this fixture: cache savings has nothing to price.
        self.assertEqual(result["cache"]["savings_usd"]["state"], "unavailable")

        # A single attempt with no repair history: lineage totals equal attempt totals.
        self.assertEqual(result["lineage_totals"]["tokens"]["total_tokens"], 245)
        self.assertEqual(result["lineage_totals"]["cost"]["usd"], 1.0)


class ComputeGraphMetricsRetryTests(unittest.TestCase):
    """feature_run_tries counts attempt-scoped retries beyond one try per node."""

    def test_feature_run_tries_exceeds_logical_nodes_when_a_node_retried_within_the_attempt(self) -> None:
        graph = {
            "run_id": "graph-1",
            "status": "running",
            "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "plan-x",
            "logical_graph_id": "graph-1",
            "nodes": [_node("N1", "graph-1-N1-b")],
        }
        catalog = {
            "plan_graphs": [graph],
            "feature_runs": [_correlated("graph-1-N1-a", "graph-1", "N1"), _correlated("graph-1-N1-b", "graph-1", "N1")],
        }
        node_details = {
            "graph-1-N1-a": _metrics(input_tokens=40, output_tokens=10, calls=2, wall_ms=500, busy_ms=400, peak=60, cost_usd=0.20, usage_records=2),
            "graph-1-N1-b": _metrics(input_tokens=20, output_tokens=5, calls=1, wall_ms=300, busy_ms=250, peak=50, cost_usd=0.10, usage_records=1),
        }
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        self.assertEqual(result["counts"], {"logical_nodes": 1, "feature_run_tries": 2})
        self.assertEqual(result["retries"]["node_retries"], 1)
        self.assertEqual(result["nodes"][0]["tries"], 2)
        # Both correlated tries are merged into the node's (and thus the graph's) totals.
        self.assertEqual(result["totals"]["tokens"]["total_tokens"], 75)
        # lineage_totals must fold in this-attempt retries discovered only via
        # correlation, not just the checkpoint-named try: it can never be
        # smaller than this attempt's own totals.
        self.assertEqual(result["lineage_totals"]["tokens"]["total_tokens"], 75)


class ComputeGraphMetricsDegradeTests(unittest.TestCase):
    """AC-DM01-2: degraded children yield the documented tri-state, never zero."""

    def test_mixed_availability_degrades_each_aggregate_independently(self) -> None:
        graph = {
            "run_id": "graph-1",
            "status": "running",
            "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "plan-x",
            "logical_graph_id": "graph-1",
            "nodes": [
                _node("A", "run-a"),
                _node("B", "run-b"),
                _node("C", "run-c"),
                _node("D", "run-d"),
            ],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = {
            # Fully available: baseline.
            "run-a": _metrics(input_tokens=10, output_tokens=5, calls=1, wall_ms=100, busy_ms=90, peak=15, cost_usd=0.10, usage_records=1),
            # Cost specifically unavailable; tokens/busy/peak still verified.
            "run-b": _metrics(input_tokens=8, output_tokens=2, calls=1, wall_ms=80, busy_ms=70, peak=10, cost_state="unavailable", cost_usd=None, usage_records=1),
            # Busy specifically missing; tokens/cost/peak still verified.
            "run-c": _metrics(input_tokens=6, output_tokens=1, calls=1, wall_ms=60, busy_ms=None, peak=8, cost_usd=0.05, usage_records=1),
            # Zero usage records: nothing this run reports may be summed as zero.
            "run-d": _metrics(input_tokens=0, output_tokens=0, calls=0, wall_ms=None, busy_ms=None, peak=None, cost_state="unavailable", cost_usd=None, usage_records=0),
        }
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        totals = result["totals"]

        # Token totals exclude the zero-record child and report a lower bound, never a fabricated zero contribution.
        self.assertEqual(totals["tokens"]["state"], "partial")
        self.assertIn("3 of 4", totals["tokens"]["reason"])
        self.assertEqual(totals["tokens"]["total_tokens"], 10 + 5 + 8 + 2 + 6 + 1)

        # Any unavailable (or missing) child cost degrades the whole aggregate to unavailable, never partial.
        self.assertEqual(totals["cost"]["state"], "unavailable")
        self.assertIsNone(totals["cost"]["usd"])
        self.assertIn("2 of 4", totals["cost"]["reason"])

        # Calls share tokens' coverage set: the zero-record child cannot be summed as a verified zero.
        self.assertEqual(totals["calls"]["state"], "partial")
        self.assertIn("3 of 4", totals["calls"]["reason"])
        self.assertEqual(totals["calls"]["value"], 3)

        # Any missing busy degrades agent-busy to unavailable (no partial state for busy).
        self.assertEqual(totals["agent_busy_ms"], {"state": "unavailable", "value": None, "reason": "agent-busy time is unavailable: one or more FeatureRuns lack verified busy timing"})

        # A mixed peak population yields the partial lower-bound state.
        self.assertEqual(totals["peak_input_tokens"]["state"], "partial")
        self.assertEqual(totals["peak_input_tokens"]["value"], 15)
        self.assertIn("3 of 4", totals["peak_input_tokens"]["reason"])

        # Parallelism cannot be computed without a graph wall time (this graph is live).
        self.assertEqual(totals["parallelism"]["state"], "unavailable")

    def test_no_budget_ledger_supplied_is_unavailable_not_zero(self) -> None:
        graph = {"run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "p", "logical_graph_id": "graph-1", "nodes": []}
        result = graph_metrics.compute_graph_metrics(graph, {"plan_graphs": [graph], "feature_runs": []}, {})
        ledger = result["retries"]["budget_ledger"]
        self.assertEqual(ledger["state"], "unavailable")
        for key in ("graph_launches", "gate_invocations", "repair_dispatches", "structural_decisions"):
            self.assertIsNone(ledger[key])

    def test_empty_node_population_degrades_cost_and_calls_not_zero(self) -> None:
        graph = {"run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "p", "logical_graph_id": "graph-1", "nodes": []}
        result = graph_metrics.compute_graph_metrics(graph, {"plan_graphs": [graph], "feature_runs": []}, {})
        totals = result["totals"]
        self.assertEqual(totals["tokens"]["state"], "unavailable")
        self.assertEqual(totals["cost"], {"state": "unavailable", "usd": None, "reason": "no logical nodes in this graph"})
        self.assertEqual(totals["calls"]["state"], "unavailable")


class ComputeGraphMetricsTimingTests(unittest.TestCase):
    """AC-DM01-3: live graphs expose started_at; terminal graphs report summary wall clock."""

    def test_terminal_graph_reports_summary_wall_clock(self) -> None:
        graph = {"run_id": "graph-1", "status": "succeeded", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "p", "logical_graph_id": "graph-1", "nodes": []}
        result = graph_metrics.compute_graph_metrics(graph, {"plan_graphs": [graph], "feature_runs": []}, {}, own_summary={"usage": {"wall_clock_ms": 4242}})
        self.assertEqual(result["timing"]["wall_clock_ms"], {"state": "available", "value": 4242, "reason": None})
        self.assertEqual(result["timing"]["started_at"], "2026-08-09T00:00:00Z")

    def test_live_graph_exposes_started_at_without_a_server_side_wall_clock(self) -> None:
        graph = {"run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "p", "logical_graph_id": "graph-1", "nodes": []}
        result = graph_metrics.compute_graph_metrics(graph, {"plan_graphs": [graph], "feature_runs": []}, {}, own_summary=None)
        self.assertEqual(result["timing"]["started_at"], "2026-08-09T00:00:00Z")
        self.assertEqual(result["timing"]["wall_clock_ms"]["state"], "unavailable")
        self.assertEqual(result["timing"]["wall_clock_ms"]["reason"], "graph is live; elapsed is derived client-side from started_at")

    def test_terminal_graph_without_a_verified_summary_is_unavailable_not_zero(self) -> None:
        graph = {"run_id": "graph-1", "status": "failed", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "p", "logical_graph_id": "graph-1", "nodes": []}
        result = graph_metrics.compute_graph_metrics(graph, {"plan_graphs": [graph], "feature_runs": []}, {}, own_summary=None)
        self.assertEqual(result["timing"]["wall_clock_ms"], {"state": "unavailable", "value": None, "reason": "summary is unavailable"})


class ComputeGraphMetricsAttemptScopingTests(unittest.TestCase):
    """AC-DM01-4: a node's try history spanning two graph attempts excludes the
    predecessor attempt's usage from this attempt's totals, reporting it only
    under lineage_totals."""

    def test_second_attempt_excludes_first_attempts_usage_from_totals(self) -> None:
        first_attempt = {
            "run_id": "graph-1", "status": "failed", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "shared-plan", "logical_graph_id": "graph-1", "graph_attempt_id": "graph-1",
            "nodes": [_node("N1", "try-1", status="failed")],
        }
        second_attempt = {
            "run_id": "graph-2", "status": "running", "created_at": "2026-08-09T01:00:00Z",
            "plan_digest": "shared-plan", "logical_graph_id": "graph-1", "graph_attempt_id": "graph-2",
            "predecessor_attempt_id": "graph-1",
            "nodes": [_node("N1", "try-2")],
        }
        catalog = {"plan_graphs": [first_attempt, second_attempt], "feature_runs": []}
        node_details = {
            "try-1": _metrics(input_tokens=1000, output_tokens=100, calls=10, wall_ms=9000, busy_ms=8000, peak=500, cost_usd=5.0, usage_records=10),
            "try-2": _metrics(input_tokens=40, output_tokens=10, calls=2, wall_ms=500, busy_ms=400, peak=60, cost_usd=0.20, usage_records=2),
        }

        result = graph_metrics.compute_graph_metrics(second_attempt, catalog, node_details)

        # This attempt's own totals see only its own try.
        self.assertEqual(result["totals"]["tokens"]["total_tokens"], 50)
        self.assertEqual(result["totals"]["cost"]["usd"], 0.20)
        self.assertEqual(result["retries"]["graph_attempts"], 2)

        # The full lineage (both attempts) is reported only under lineage_totals.
        self.assertEqual(result["lineage_totals"]["tokens"]["total_tokens"], 1150)
        self.assertEqual(result["lineage_totals"]["cost"]["usd"], 5.20)

    def test_first_attempt_alone_is_unaffected_by_a_later_attempt_not_yet_recorded(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "failed", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "shared-plan", "logical_graph_id": "graph-1", "graph_attempt_id": "graph-1",
            "nodes": [_node("N1", "try-1", status="failed")],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = {"try-1": _metrics(input_tokens=1000, output_tokens=100, calls=10, wall_ms=9000, busy_ms=8000, peak=500, cost_usd=5.0, usage_records=10)}

        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        self.assertEqual(result["totals"]["tokens"]["total_tokens"], 1100)
        self.assertEqual(result["lineage_totals"]["tokens"]["total_tokens"], 1100)


class PerFeatureRunCostAvailabilityTests(unittest.TestCase):
    """AC-DM01-2: an estimated per-run cost must not be reported as 'available'."""

    def test_estimated_child_cost_is_not_labelled_available(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [_node("A", "run-a"), _node("B", "run-b")],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = {
            "run-a": _metrics(input_tokens=10, output_tokens=5, calls=1, wall_ms=100, busy_ms=90, peak=15, cost_usd=0.10, usage_records=1),
            "run-b": _metrics(input_tokens=8, output_tokens=2, calls=1, wall_ms=80, busy_ms=70, peak=10, cost_state="estimated", cost_usd=0.05, usage_records=1),
        }
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        cost_usd = result["per_feature_run"]["cost_usd"]
        self.assertEqual(cost_usd["state"], "estimated")
        self.assertEqual(cost_usd["sample_size"], 2)
        self.assertAlmostEqual(cost_usd["max"], 0.10)


class ReusedNodeReasonTests(unittest.TestCase):
    """AC-DM01-2: a reused node's excluded-from-attempt reason must not read as missing evidence."""

    def test_reused_node_reports_reuse_reason_not_unverified(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [
                _node(
                    "A", "graph-1-A-planned", status="succeeded",
                    correlation={
                        "state": "reused",
                        "origin_attempt_id": "graph-0",
                        "origin_feature_run_id": "graph-0-A",
                        "reused_from_attempt": "graph-0",
                        "reason": "node was reused from attempt graph-0",
                    },
                ),
            ],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        result = graph_metrics.compute_graph_metrics(graph, catalog, {})
        row = result["nodes"][0]
        self.assertEqual(row["detail"]["state"], "unavailable")
        self.assertIn("reused", row["detail"]["reason"])
        self.assertNotIn("unverified", row["detail"]["reason"])

    def test_reused_node_row_shows_lineage_cumulative_metrics_not_a_blank(self) -> None:
        """A reused node's per-node row reports the producing tries' cumulative
        metrics (tokens/cost/wall) with reuse provenance, while the graph's
        attempt-scoped totals still exclude that usage; the planned-but-never-
        executed feature_run_id of the reusing attempt must not poison the
        lineage merge as a phantom try."""
        predecessor = {
            "run_id": "graph-0", "status": "blocked", "created_at": "2026-08-08T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-0",
            "nodes": [_node("A", "graph-0-A", status="succeeded")],
        }
        successor = {
            "run_id": "graph-1", "status": "succeeded", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-0",
            "nodes": [
                _node(
                    "A", "graph-1-A", status="succeeded",
                    correlation={"state": "reused", "reused_from_attempt": "graph-0"},
                ),
            ],
        }
        catalog = {
            "plan_graphs": [predecessor, successor],
            "feature_runs": [_correlated("graph-0-A", "graph-0", "A")],
        }
        node_details = {
            "graph-0-A": _metrics(input_tokens=10, output_tokens=5, calls=1, wall_ms=100, busy_ms=90, peak=15, cost_usd=0.10, usage_records=1),
        }
        result = graph_metrics.compute_graph_metrics(successor, catalog, node_details)
        row = result["nodes"][0]
        self.assertEqual(row["detail"]["state"], "available")
        self.assertIn("reused", row["detail"]["reason"])
        self.assertEqual(row["totals"]["total_tokens"], 15)
        self.assertAlmostEqual(row["totals"]["cost"]["usd"], 0.10)
        self.assertEqual(row["tries"], 1)
        # The attempt-scoped graph totals still exclude the reused usage.
        self.assertEqual(result["totals"]["tokens"]["state"], "unavailable")
        # The lineage block carries it instead.
        self.assertEqual(result["lineage_totals"]["tokens"]["total_tokens"], 15)


class BlockersPopulationTests(unittest.TestCase):
    """AC-DM01-1: blockers include disposition-blocked nodes regardless of current checkpoint status."""

    def test_disposition_blocked_node_counts_even_when_status_is_not_blocked(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "failed", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [_node("A", None, status="failed")],
            "execution": {
                "recovery": {
                    "dispositions": [{"node_id": "A", "disposition": "blocked", "reason": "budget exhausted", "forced": False, "evidence_refs": []}],
                },
            },
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        result = graph_metrics.compute_graph_metrics(graph, catalog, {})
        self.assertEqual(result["blockers"], {"count": 1, "nodes": [{"node_id": "A", "reason": "budget exhausted"}]})


class SchedulingAndCacheTests(unittest.TestCase):
    """DM-01 node statement (plan:386-396): wait/critical-path and cache savings."""

    def test_critical_path_follows_the_longest_dependency_chain(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [
                _node("A", "run-a"),
                _node("B", "run-b", depends_on=["A"]),
                _node("C", "run-c", depends_on=["A", "B"]),
            ],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = {
            "run-a": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=100, busy_ms=100, peak=1, cost_usd=0.0, usage_records=1),
            "run-b": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=200, busy_ms=200, peak=1, cost_usd=0.0, usage_records=1),
            "run-c": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=50, busy_ms=50, peak=1, cost_usd=0.0, usage_records=1),
        }
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        # Longest chain is A -> B -> C: 100 + 200 + 50 = 350, not the 250 non-chain sum of A and C alone.
        self.assertEqual(result["scheduling"]["critical_path_ms"], {"state": "available", "value": 350, "reason": None})

    def test_critical_path_degrades_to_partial_when_a_chain_node_lacks_wall_time(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [_node("A", "run-a"), _node("B", "run-b", depends_on=["A"])],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = {
            "run-a": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=None, busy_ms=None, peak=1, cost_usd=0.0, usage_records=1),
            "run-b": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=200, busy_ms=200, peak=1, cost_usd=0.0, usage_records=1),
        }
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        self.assertEqual(result["scheduling"]["critical_path_ms"]["state"], "partial")

    def test_cache_savings_prices_cached_input_tokens_by_model_rate(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [_node("A", "run-a")],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        by_model = [{"label": "claude-sonnet-5", "calls": 1, "input_tokens": 1000, "cached_input_tokens": 1000, "output_tokens": 10, "duration_ms": 100}]
        node_details = {
            "run-a": _metrics(input_tokens=1000, cached_input_tokens=1000, output_tokens=10, calls=1, wall_ms=100, busy_ms=100, peak=1, cost_usd=1.0, usage_records=1, by_model=by_model),
        }
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        # claude-sonnet-5: input $2.00/M, cached_input $0.20/M -> savings = 1000 * (2.00 - 0.20) / 1e6.
        self.assertEqual(result["cache"]["savings_usd"], {"state": "available", "value": round(1000 * (2.00 - 0.20) / 1_000_000, 6), "reason": None})

    def test_wait_ms_field_is_present_and_honestly_unavailable(self) -> None:
        graph = {
            "run_id": "graph-1", "status": "running", "created_at": "2026-08-09T00:00:00Z",
            "plan_digest": "p", "logical_graph_id": "graph-1",
            "nodes": [_node("A", "run-a")],
        }
        catalog = {"plan_graphs": [graph], "feature_runs": []}
        node_details = {"run-a": _metrics(input_tokens=1, output_tokens=1, calls=1, wall_ms=1, busy_ms=1, peak=1, cost_usd=0.0, usage_records=1)}
        result = graph_metrics.compute_graph_metrics(graph, catalog, node_details)
        self.assertEqual(result["nodes"][0]["wait_ms"]["state"], "unavailable")
        self.assertIsNotNone(result["nodes"][0]["wait_ms"]["reason"])


class ReadBudgetLedgerTests(unittest.TestCase):
    def test_absent_file_is_unavailable(self) -> None:
        from pathlib import Path
        ledger = graph_metrics.read_budget_ledger(Path("/nonexistent/does-not-exist.jsonl"))
        self.assertEqual(ledger["state"], "unavailable")
        self.assertIsNone(ledger["graph_launches"])

    def test_last_well_formed_line_names_the_current_counters(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lineage.jsonl"
            path.write_text(
                '{"protocol":"retry-budget-ledger/1","lineage_id":"x","event":"reserved","attempt_counters":{"graph_launches":1,"gate_invocations":0,"repair_dispatches":0,"structural_decisions":0}}\n'
                'not json\n'
                '{"protocol":"retry-budget-ledger/1","lineage_id":"x","event":"gate_changed","attempt_counters":{"graph_launches":1,"gate_invocations":2,"repair_dispatches":1,"structural_decisions":0}}\n',
                encoding="utf-8",
            )
            ledger = graph_metrics.read_budget_ledger(path)
            self.assertEqual(ledger, {"state": "available", "reason": None, "graph_launches": 1, "gate_invocations": 2, "repair_dispatches": 1, "structural_decisions": 0})

    def test_distinct_counters_are_not_conflated(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lineage.jsonl"
            path.write_text('{"protocol":"retry-budget-ledger/1","lineage_id":"x","event":"reserved","attempt_counters":{"graph_launches":2,"gate_invocations":5,"repair_dispatches":3,"structural_decisions":1}}\n', encoding="utf-8")
            ledger = graph_metrics.read_budget_ledger(path)
            self.assertEqual((ledger["graph_launches"], ledger["gate_invocations"], ledger["repair_dispatches"], ledger["structural_decisions"]), (2, 5, 3, 1))


if __name__ == "__main__":
    unittest.main()
