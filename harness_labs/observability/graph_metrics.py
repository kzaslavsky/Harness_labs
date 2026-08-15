"""PlanGraph-level metrics rollup: shared cumulative-merge and graph totals.

This module owns the single implementation of "cumulative metrics across a
logical PlanGraph node's tries" (moved out of ``dashboard_server``) so the
live dashboard API and the PlanGraph rollup compute identical numbers from
one code path. It is read-only: every function here is a pure projection
over already-verified catalog records (``run_catalog`` snapshot shapes),
already-projected per-run detail-metrics documents
(``harness-run-detail-metrics/1``), and the plain-JSONL retry-budget ledger
(``retry-budget-ledger/1``, read as plain JSON lines, never through
``harness_labs.plangraph``).

Tri-state availability convention used throughout: a degraded aggregate
never renders or sums missing data as zero. Each aggregate field is a
mapping with a ``state`` of ``"available"``, ``"partial"`` (a lower bound,
some but not all inputs covered), or ``"unavailable"`` (or, for cost only,
``"estimated"`` in place of ``"available"`` when any covered input is an
estimate), plus a ``reason`` string whenever ``state`` is not
``"available"``.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from harness_labs.observability.run_catalog import _ESTIMATED_MODEL_PRICES

PROTOCOL = "harness-plan-graph-metrics/1"
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_LEDGER_LINES = 20_000
_LEDGER_COUNTER_FIELDS = ("graph_launches", "gate_invocations", "repair_dispatches", "structural_decisions")
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "blocked", "interrupted"})


# ---------------------------------------------------------------------------
# Cumulative node-try merge (moved from dashboard_server; single shared impl)
# ---------------------------------------------------------------------------

def node_history_run_ids(
    catalog: Mapping[str, Any],
    target_run_id: str,
    known_run_ids: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Return the ordered tries for the target's logical node through this try.

    Tries are grouped by ``(plan_digest, node_id)`` across every PlanGraph in
    the catalog that shares the plan digest, chronologically by graph
    ``created_at`` — this spans a node's *entire* history, including repair
    attempts (a separate ``graph_attempt_id``). Callers that need
    attempt-scoped (this-attempt-only) history must use
    ``compute_graph_metrics``'s own scoping instead.

    A checkpoint's ``feature_run_id`` names the *planned* run for the node;
    for a node that never launched in that attempt (reused from a sealed
    predecessor, or the attempt died before dispatch) no such run directory
    exists. Those planned-but-never-executed ids are excluded: only runs
    the catalog actually contains are tries. Requiring a detail document
    for a phantom id would otherwise degrade every lineage merge to
    ``unavailable`` even when every executed try is fully verified.
    ``known_run_ids`` supplements the catalog's own feature-run listing as
    evidence of existence (callers that already hold verified detail
    documents pass their key set).
    """
    existing = {
        run.get("run_id")
        for run in catalog.get("feature_runs", []) or []
        if isinstance(run, Mapping) and isinstance(run.get("run_id"), str)
    } | set(known_run_ids)
    histories: dict[tuple[str, str], list[str]] = {}
    graphs = sorted(catalog.get("plan_graphs", []), key=lambda graph: (str(graph.get("created_at", "")), str(graph.get("run_id", ""))))
    for graph in graphs:
        plan_digest = graph.get("plan_digest")
        if not isinstance(plan_digest, str):
            continue
        for node in graph.get("nodes", []):
            if not isinstance(node, Mapping):
                continue
            node_id = node.get("node_id")
            run_id = node.get("feature_run_id")
            if not isinstance(node_id, str) or not isinstance(run_id, str):
                continue
            history = histories.setdefault((plan_digest, node_id), [])
            if run_id in existing and run_id not in history:
                history.append(run_id)
            if run_id == target_run_id:
                return list(history)
    return [target_run_id] if target_run_id in existing else []


def apply_cumulative_node_metrics(catalog: Mapping[str, Any], details: dict[str, dict[str, Any]]) -> None:
    """Accumulate verified metrics across tries of one logical PlanGraph node, in place."""
    base_metrics = {
        run_id: detail.get("metrics")
        for run_id, detail in details.items()
        if isinstance(detail.get("metrics"), Mapping)
    }
    histories: dict[tuple[str, str], list[str]] = {}
    graphs = sorted(catalog.get("plan_graphs", []), key=lambda graph: (str(graph.get("created_at", "")), str(graph.get("run_id", ""))))
    for graph in graphs:
        plan_digest = graph.get("plan_digest")
        if not isinstance(plan_digest, str):
            continue
        for node in graph.get("nodes", []):
            if not isinstance(node, Mapping):
                continue
            node_id = node.get("node_id")
            run_id = node.get("feature_run_id")
            if not isinstance(node_id, str) or not isinstance(run_id, str) or run_id not in details:
                continue
            history = histories.setdefault((plan_digest, node_id), [])
            if run_id in history:
                continue
            history.append(run_id)
            metrics = [base_metrics.get(item) for item in history]
            if all(isinstance(item, Mapping) for item in metrics):
                details[run_id]["metrics"] = merge_detail_metrics(metrics, history)


def merge_detail_metrics(metrics: list[Mapping[str, Any]], run_ids: list[str]) -> dict[str, Any]:
    """Merge one logical node's per-try detail-metrics documents into one cumulative view.

    Tokens/cost/duration/wall/busy/peak accumulate across every try (the
    pre-existing behaviour). ``quality`` (criteria, open findings) is
    retained from the latest try only — those are legitimately
    current-state, not additive. Review cycles, verification repairs, and
    findings totals are *also* legitimately additive across tries (a retry
    does not erase the repair work a prior try did), so they are reported
    separately in the labelled ``cumulative_quality`` block alongside the
    latest-try ``quality`` and the existing per-try ``by_try``/``stages``
    rows.
    """
    latest = metrics[-1]
    merged = dict(latest)
    merged["totals"] = _merge_metric_totals([item["totals"] for item in metrics])
    for key in ("by_phase", "by_agent", "by_agent_type", "by_model", "by_effort", "by_backend"):
        merged[key] = _merge_metric_breakdown([row for item in metrics for row in item.get(key, [])])
    merged["by_try"] = [
        {"label": run_id, **dict(item["totals"])}
        for run_id, item in zip(run_ids, metrics, strict=True)
    ]
    merged["stages"] = [
        {**stage, "feature_run_id": run_id, "try_index": index}
        for index, (run_id, item) in enumerate(zip(run_ids, metrics, strict=True), start=1)
        for stage in item.get("stages", [])
    ]
    merged["cumulative_quality"] = _merge_quality(metrics, run_ids)
    provenance = dict(latest.get("provenance", {}))
    provenance.update({
        "usage_records": sum(int(item.get("provenance", {}).get("usage_records", 0)) for item in metrics),
        "collection_method": "verified usage accumulated across logical PlanGraph node tries",
        "attempt_count": len(run_ids),
        "current_run_id": run_ids[-1],
        "scope": "cumulative_plan_graph_node",
    })
    merged["provenance"] = provenance
    return merged


def _merge_quality(metrics: list[Mapping[str, Any]], run_ids: list[str]) -> dict[str, Any]:
    per_try = [dict(item.get("quality") or {}) for item in metrics]
    return {
        "review_cycles": sum(_nonnegative_int(row.get("review_cycles")) for row in per_try),
        "verification_repairs": sum(_nonnegative_int(row.get("verification_repairs")) for row in per_try),
        "findings_total": sum(_nonnegative_int(row.get("findings_total")) for row in per_try),
        "try_count": len(run_ids),
        "reason": f"cumulative across {len(run_ids)} tr{'y' if len(run_ids) == 1 else 'ies'}",
        "by_try": [
            {
                "label": run_id,
                "review_cycles": _nonnegative_int(row.get("review_cycles")),
                "verification_repairs": _nonnegative_int(row.get("verification_repairs")),
                "findings_total": _nonnegative_int(row.get("findings_total")),
            }
            for run_id, row in zip(run_ids, per_try, strict=True)
        ],
    }


def _merge_metric_breakdown(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("label", "Unavailable")), []).append(row)
    result = []
    for label, values in grouped.items():
        row = {"label": label, **_merge_metric_totals(values)}
        for field in ("phase", "agent_type", "model", "effort", "backend"):
            items = sorted({str(value[field]) for value in values if value.get(field)})
            if items:
                row[field] = ", ".join(items)
        result.append(row)
    return sorted(result, key=lambda row: (-row["total_tokens"], row["label"]))


def _merge_metric_totals(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    result = {
        key: sum(int(row.get(key, 0)) for row in rows)
        for key in ("calls", "input_tokens", "cached_input_tokens", "output_tokens", "duration_ms")
    }
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    # None means the backend reported only cumulative session usage; a peak
    # is only meaningful when at least one row observed a real invocation.
    known_peaks = [
        row["peak_input_tokens"]
        for row in rows
        if isinstance(row.get("peak_input_tokens"), int)
    ]
    result["peak_input_tokens"] = max(known_peaks) if known_peaks else None
    # Wall and busy time are only meaningful cumulatively when every try
    # reports them.  Summing a subset (e.g. omitting a still-running try
    # whose summary does not exist yet) understates wall time while the
    # summed backend durations keep growing, which falsely displays agent
    # time exceeding wall time.  Tries execute sequentially, so summing
    # per-try values never double-counts overlapping intervals.
    wall = [row.get("wall_clock_ms") for row in rows]
    if rows and all(type(value) is int for value in wall):
        result["wall_clock_ms"] = sum(wall)
    else:
        result["wall_clock_ms"] = None
    busy = [row.get("busy_ms") for row in rows]
    if rows and all(type(value) is int for value in busy):
        result["busy_ms"] = sum(busy)
    else:
        result["busy_ms"] = None
    costs = [row.get("cost") for row in rows if isinstance(row.get("cost"), Mapping)]
    unavailable = [cost for cost in costs if cost.get("state") == "unavailable"]
    estimated = [cost for cost in costs if cost.get("state") == "estimated"]
    sources = sorted({str(source) for cost in costs for source in cost.get("sources", [])})
    result["cost"] = {
        "state": "unavailable" if unavailable or len(costs) != len(rows) else "estimated" if estimated else "available",
        "usd": None if unavailable or len(costs) != len(rows) else round(sum(float(cost.get("usd") or 0) for cost in costs), 6),
        "reason": f"{len(unavailable)} node try cost record(s) are unavailable" if unavailable else "Cumulative across verified node tries" if estimated else None,
        "sources": sources,
        "estimated_records": sum(int(cost.get("estimated_records", 0)) for cost in costs),
        "long_context_records": sum(int(cost.get("long_context_records", 0)) for cost in costs),
    }
    return result


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


# ---------------------------------------------------------------------------
# Retry-budget ledger (plain JSONL; never imports harness_labs.plangraph)
# ---------------------------------------------------------------------------

def read_budget_ledger(path: Path) -> dict[str, Any]:
    """Read one ``retry-budget-ledger/1`` JSONL file into a bounded summary.

    Each line's ``attempt_counters`` already reports the running total, so
    the last well-formed line names the current counts. The ledger's four
    counters (``graph_launches``, ``gate_invocations``, ``repair_dispatches``,
    ``structural_decisions``) are reported distinctly and never conflated.
    Absence, an oversized file, or a file with no recognized record all
    degrade to ``unavailable`` with a reason instead of raising.
    """
    supplied = Path(path)
    if supplied.is_symlink() or not supplied.is_file():
        return _unavailable_ledger("retry-budget ledger is absent")
    try:
        if supplied.stat().st_size > MAX_LEDGER_BYTES:
            return _unavailable_ledger("retry-budget ledger exceeds size limit")
        lines = supplied.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _unavailable_ledger("retry-budget ledger could not be read")
    counters: dict[str, int] | None = None
    for line in lines[:MAX_LEDGER_LINES]:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping) or record.get("protocol") != "retry-budget-ledger/1":
            continue
        attempt_counters = record.get("attempt_counters")
        if isinstance(attempt_counters, Mapping) and all(isinstance(attempt_counters.get(key), int) for key in _LEDGER_COUNTER_FIELDS):
            counters = {key: attempt_counters[key] for key in _LEDGER_COUNTER_FIELDS}
    if counters is None:
        return _unavailable_ledger("retry-budget ledger contains no recognized counter record")
    return {"state": "available", "reason": None, **counters}


def _unavailable_ledger(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason, **{key: None for key in _LEDGER_COUNTER_FIELDS}}


# ---------------------------------------------------------------------------
# PlanGraph rollup (pure: catalog records + child merged metrics + ledger)
# ---------------------------------------------------------------------------

def compute_graph_metrics(
    graph: Mapping[str, Any],
    catalog: Mapping[str, Any],
    node_details: Mapping[str, Mapping[str, Any]],
    *,
    own_summary: Mapping[str, Any] | None = None,
    budget_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute one PlanGraph's totals from already-verified inputs.

    ``node_details`` maps a feature-run id to that run's own (single-try,
    unmerged) ``harness-run-detail-metrics/1`` document — the same shape
    ``run_catalog.build_run_detail(...)["metrics"]`` produces. ``own_summary``
    is the graph run's own verified ``summary.json`` contents (``None`` for a
    live graph, or when unavailable). ``budget_ledger`` is the parsed result
    of ``read_budget_ledger`` (or ``None`` when not supplied / unavailable).

    Totals are **attempt-scoped**: only tries correlated to this graph's own
    ``run_id`` are summed into ``totals``. Cross-attempt (repair) history is
    reported separately, and only, under ``lineage_totals``.
    """
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, Mapping)]
    population = len(nodes)
    node_rows: list[dict[str, Any]] = []
    attempt_merges: list[Mapping[str, Any]] = []
    lineage_merges: list[Mapping[str, Any]] = []
    known_run_ids = frozenset(node_details.keys())
    for node in nodes:
        node_id = node.get("node_id") if isinstance(node.get("node_id"), str) else None
        attempt_history = _attempt_scoped_history(graph, node, catalog, known_run_ids)
        merged = _merge_if_available(attempt_history, node_details)
        if merged is not None:
            attempt_merges.append(merged)
        correlation = node.get("correlation") if isinstance(node.get("correlation"), Mapping) else None
        reused = isinstance(correlation, Mapping) and correlation.get("state") == "reused"
        reason = None
        if merged is None:
            if reused:
                reason = "node was reused from a prior attempt's sealed candidate; it executed no FeatureRun in this attempt, so it contributes no usage to this attempt's totals"
            elif not attempt_history:
                reason = "no FeatureRun is recorded for this node yet"
            else:
                reason = "one or more attempt-scoped FeatureRun detail record(s) are unverified"
        lineage_history: list[str] = []
        lineage_merged: dict[str, Any] | None = None
        feature_run_id = node.get("feature_run_id")
        if isinstance(feature_run_id, str):
            # Lineage must span every try this logical node has ever had,
            # including this-attempt retries: checkpoint-recorded history
            # alone (node_history_run_ids) only names the current try per
            # graph attempt, so in-attempt retries are folded in too — a
            # lineage total can never be smaller than this attempt's own.
            lineage_history = list(node_history_run_ids(catalog, feature_run_id, known_run_ids))
            for run_id in attempt_history:
                if run_id not in lineage_history:
                    lineage_history.append(run_id)
            lineage_merged = _merge_if_available(lineage_history, node_details)
            if lineage_merged is not None:
                lineage_merges.append(lineage_merged)
        node_rows.append({
            "node_id": node_id or "unavailable",
            "status": node.get("status"),
            "tries": len(attempt_history),
            "merged": merged,
            "reason": reason,
            "reused": reused,
            "lineage_tries": len(lineage_history),
            "lineage_merged": lineage_merged,
        })

    totals = _aggregate_totals(attempt_merges, population)
    lineage = _aggregate_totals(lineage_merges, population)

    status = graph.get("status")
    wall_block = _graph_wall_clock(status, own_summary)
    started_at = graph.get("created_at") if isinstance(graph.get("created_at"), str) else None

    if totals["agent_busy_ms"]["state"] in ("available", "partial") and wall_block["state"] == "available" and wall_block["value"]:
        ratio = round(totals["agent_busy_ms"]["value"] / wall_block["value"], 3)
        if totals["agent_busy_ms"]["state"] == "partial":
            parallelism = _metric("partial", ratio, "lower bound: derived from a lower-bound agent-busy time")
        else:
            parallelism = _metric("available", ratio)
    else:
        missing = [name for name, block in (("agent-busy time", totals["agent_busy_ms"]), ("graph wall time", wall_block)) if block["state"] not in ("available", "partial")]
        parallelism = _metric("unavailable", None, f"parallelism requires {' and '.join(missing)}")

    execution = graph.get("execution") if isinstance(graph.get("execution"), Mapping) else {}
    recovery = execution.get("recovery") if isinstance(execution.get("recovery"), Mapping) else {}
    dispositions = [item for item in recovery.get("dispositions", []) if isinstance(item, Mapping)] if isinstance(recovery.get("dispositions"), list) else []
    attempt_lineage = recovery.get("attempt_lineage") if isinstance(recovery.get("attempt_lineage"), list) else []
    retry_state = recovery.get("retry_state") if isinstance(recovery.get("retry_state"), Mapping) else {}
    invalidations = retry_state.get("invalidations") if isinstance(retry_state.get("invalidations"), list) else []

    blockers = _blockers(nodes, dispositions)
    ledger_block = dict(budget_ledger) if isinstance(budget_ledger, Mapping) else _unavailable_ledger("retry-budget ledger was not supplied")

    node_table = _node_table(node_rows)
    critical_path = _critical_path_ms(nodes, node_rows)
    cache_savings = _cache_savings(node_rows)

    return {
        "protocol": PROTOCOL,
        "run_id": graph.get("run_id"),
        "status": status,
        "timing": {"started_at": started_at, "wall_clock_ms": wall_block},
        "totals": {
            "tokens": totals["tokens"],
            "cost": totals["cost"],
            "calls": totals["calls"],
            "agent_busy_ms": totals["agent_busy_ms"],
            "parallelism": parallelism,
            "peak_input_tokens": totals["peak_input_tokens"],
        },
        "retries": {
            "budget_ledger": ledger_block,
            "node_retries": sum(max(0, row["tries"] - 1) for row in node_rows),
            "graph_attempts": _graph_attempt_count(graph, catalog),
        },
        "recovery": {
            "dispositions": dispositions,
            "attempt_lineage_count": len(attempt_lineage),
            "invalidations_count": len(invalidations),
        },
        "blockers": {"count": len(blockers), "nodes": blockers},
        "counts": {"logical_nodes": population, "feature_run_tries": sum(row["tries"] for row in node_rows)},
        "per_feature_run": _per_feature_run(node_rows, population),
        "nodes": node_table,
        "scheduling": {"critical_path_ms": critical_path},
        "cache": {"savings_usd": cache_savings},
        "lineage_totals": {
            "tokens": lineage["tokens"],
            "cost": lineage["cost"],
            "calls": lineage["calls"],
            "agent_busy_ms": lineage["agent_busy_ms"],
            "peak_input_tokens": lineage["peak_input_tokens"],
            "reason": "cross-attempt lineage: spans every graph attempt that shares this node's plan digest, not just this attempt",
        },
    }


def _attempt_scoped_history(
    graph: Mapping[str, Any],
    node: Mapping[str, Any],
    catalog: Mapping[str, Any],
    known_run_ids: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Feature-run tries correlated to this node within exactly this graph attempt.

    A node's ``feature_run_id`` in the graph's own checkpoint always names
    the current try; prior tries the controller retried *within this same
    graph run* stay discoverable because their own descriptor's
    ``parent_correlation`` still names ``(this graph's run_id, this
    node_id)`` even after the checkpoint moves on. Order is not
    semantically load-bearing here (values are only ever summed, never
    treated as "latest"), so a deterministic lexicographic order is used.
    """
    node_id = node.get("node_id")
    graph_run_id = graph.get("run_id")
    history: set[str] = set()
    if isinstance(node_id, str) and isinstance(graph_run_id, str):
        for run in catalog.get("feature_runs", []) or []:
            if not isinstance(run, Mapping):
                continue
            correlation = run.get("correlation")
            run_id = run.get("run_id")
            if (
                isinstance(correlation, Mapping)
                and correlation.get("plan_graph_id") == graph_run_id
                and correlation.get("plan_node_id") == node_id
                and isinstance(run_id, str)
            ):
                history.add(run_id)
    feature_run_id = node.get("feature_run_id")
    if isinstance(feature_run_id, str):
        # The checkpoint names the *planned* run; a reused node (or an
        # attempt that died before dispatch) has no such run directory, and
        # counting a phantom id as a try would both inflate `tries` and
        # force the merge to `unavailable` despite verified executed tries.
        existing = {
            run.get("run_id")
            for run in catalog.get("feature_runs", []) or []
            if isinstance(run, Mapping) and isinstance(run.get("run_id"), str)
        } | set(known_run_ids)
        if feature_run_id in existing:
            history.add(feature_run_id)
    return sorted(history)


def _merge_if_available(history: list[str], node_details: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    if not history:
        return None
    metrics = [node_details.get(run_id) for run_id in history]
    if not all(isinstance(item, Mapping) for item in metrics):
        return None
    return merge_detail_metrics(metrics, history)


def _metric(state: str, value: Any, reason: str | None = None) -> dict[str, Any]:
    return {"state": state, "value": value, "reason": reason}


def _aggregate_totals(rows: list[Mapping[str, Any]], population: int) -> dict[str, Any]:
    """Sum attempt- (or lineage-)scoped per-node merged metrics into graph totals.

    ``rows`` holds one merged-metrics document per node for which detail was
    verifiable; nodes with no verifiable detail are simply absent, which is
    exactly what degrades the aggregate below ``available``.
    """
    covered_tokens = [row for row in rows if int(row.get("provenance", {}).get("usage_records", 0)) > 0]
    if not covered_tokens:
        tokens = {"state": "unavailable", "reason": "no FeatureRun reports verified token usage", "input_tokens": None, "cached_input_tokens": None, "output_tokens": None, "total_tokens": None}
    else:
        sums = {field: sum(int(row["totals"].get(field, 0)) for row in covered_tokens) for field in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")}
        if len(covered_tokens) == population:
            tokens = {"state": "available", "reason": None, **sums}
        else:
            tokens = {"state": "partial", "reason": f"lower bound: {len(covered_tokens)} of {population} FeatureRun(s) report verified token usage", **sums}

    if population == 0:
        # An empty node population is not "verified zero cost" — there is
        # nothing to verify, so it must degrade like every other aggregate
        # instead of falling through to the available-$0.00 branch below.
        cost = {"state": "unavailable", "usd": None, "reason": "no logical nodes in this graph"}
    else:
        costs = [row["totals"]["cost"] for row in rows if isinstance(row.get("totals", {}).get("cost"), Mapping)]
        reporting = [cost for cost in costs if cost.get("state") in ("available", "estimated") and cost.get("usd") is not None]
        estimated_costs = [cost for cost in reporting if cost.get("state") == "estimated"]
        uncovered = population - len(reporting)
        if not reporting:
            cost = {"state": "unavailable", "usd": None, "reason": f"{population} of {population} node cost record(s) are unavailable"}
        elif uncovered:
            # Partial coverage sums the reporting subset as a verified lower
            # bound (matching the tokens/calls policy) rather than discarding
            # it: a node with no cost record cannot subtract from a sum.
            qualifier = "; includes estimated pricing" if estimated_costs else ""
            cost = {"state": "partial", "usd": round(sum(float(item.get("usd") or 0) for item in reporting), 6), "reason": f"lower bound: {len(reporting)} of {population} node cost record(s) report a cost{qualifier}"}
        elif estimated_costs:
            cost = {"state": "estimated", "usd": round(sum(float(item.get("usd") or 0) for item in reporting), 6), "reason": f"{len(estimated_costs)} of {population} node cost record(s) are estimated"}
        else:
            cost = {"state": "available", "usd": round(sum(float(item.get("usd") or 0) for item in reporting), 6), "reason": None}

    busy_values = [value for value in (row["totals"].get("busy_ms") for row in rows) if isinstance(value, int)]
    if busy_values and len(busy_values) == population:
        busy = _metric("available", sum(busy_values))
    elif busy_values:
        # Same lower-bound policy as tokens/calls/cost: the reporting subset
        # sums to a verified minimum; a node with no busy timing cannot
        # subtract from a sum.
        busy = _metric("partial", sum(busy_values), f"lower bound: {len(busy_values)} of {population} FeatureRun(s) report verified busy timing")
    else:
        busy = _metric("unavailable", None, "agent-busy time is unavailable: no FeatureRun reports verified busy timing")

    peaks = [row["totals"].get("peak_input_tokens") for row in rows if isinstance(row.get("totals", {}).get("peak_input_tokens"), int)]
    if rows and len(peaks) == population:
        peak = _metric("available", max(peaks))
    elif peaks:
        peak = _metric("partial", max(peaks), f"lower bound: {len(peaks)} of {population} FeatureRun(s) report per-invocation peaks")
    else:
        peak = _metric("unavailable", None, "no FeatureRun reports a per-invocation peak")

    # Calls are derived from the same usage records as tokens, so they share
    # tokens' coverage set and degrade the same way: an unverified child must
    # never silently shrink the sum toward zero.
    if not covered_tokens:
        calls = _metric("unavailable", None, "no FeatureRun reports verified call counts")
    elif len(covered_tokens) == population:
        calls = _metric("available", sum(int(row["totals"].get("calls", 0)) for row in covered_tokens))
    else:
        calls = _metric("partial", sum(int(row["totals"].get("calls", 0)) for row in covered_tokens), f"lower bound: {len(covered_tokens)} of {population} FeatureRun(s) report verified call counts")
    return {"tokens": tokens, "cost": cost, "calls": calls, "agent_busy_ms": busy, "peak_input_tokens": peak}


def _graph_wall_clock(status: Any, own_summary: Mapping[str, Any] | None) -> dict[str, Any]:
    if status not in _TERMINAL_STATUSES:
        return _metric("unavailable", None, "graph is live; elapsed is derived client-side from started_at")
    if not isinstance(own_summary, Mapping):
        return _metric("unavailable", None, "summary is unavailable")
    usage = own_summary.get("usage")
    wall_ms = usage.get("wall_clock_ms") if isinstance(usage, Mapping) else None
    if isinstance(wall_ms, int):
        return _metric("available", wall_ms)
    return _metric("unavailable", None, "graph summary does not record a wall clock")


def _blockers(nodes: list[Mapping[str, Any]], dispositions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    disposition_reasons = {
        item.get("node_id"): item.get("reason")
        for item in dispositions
        if item.get("disposition") == "blocked" and isinstance(item.get("node_id"), str)
    }
    nodes_by_id = {node.get("node_id"): node for node in nodes if isinstance(node.get("node_id"), str)}
    # A disposition records a recovery outcome independently of checkpoint
    # node status, so a node blocked by disposition but whose checkpoint has
    # since moved to failed/queued must still be counted here.
    blocked_ids = {node_id for node_id, node in nodes_by_id.items() if node.get("status") == "blocked"} | set(disposition_reasons)
    result = []
    for node_id in sorted(blocked_ids):
        node = nodes_by_id.get(node_id)
        evidence = node.get("evidence") if node and isinstance(node.get("evidence"), Mapping) else {}
        reason = evidence.get("reason") or disposition_reasons.get(node_id) or "blocked status recorded without an evidence reason"
        result.append({"node_id": node_id, "reason": reason})
    return result


def _graph_attempt_count(graph: Mapping[str, Any], catalog: Mapping[str, Any]) -> int:
    logical_id = graph.get("logical_graph_id")
    if not isinstance(logical_id, str):
        return 1
    return sum(1 for item in catalog.get("plan_graphs", []) if isinstance(item, Mapping) and item.get("logical_graph_id") == logical_id) or 1


def _distribution(values: list[float], population: int) -> dict[str, Any]:
    if not values:
        return {"state": "unavailable", "reason": "no FeatureRun in this graph reports this metric", "mean": None, "median": None, "max": None, "sample_size": 0, "population": population}
    state = "available" if len(values) == population else "partial"
    reason = None if state == "available" else f"lower bound: {len(values)} of {population} FeatureRun(s) report this metric"
    return {"state": state, "reason": reason, "mean": round(sum(values) / len(values), 3), "median": median(values), "max": max(values), "sample_size": len(values), "population": population}


def _per_feature_run(node_rows: list[dict[str, Any]], population: int) -> dict[str, Any]:
    wall_values = [row["merged"]["totals"]["wall_clock_ms"] for row in node_rows if row["merged"] and isinstance(row["merged"]["totals"].get("wall_clock_ms"), int)]
    token_values = [
        row["merged"]["totals"]["total_tokens"]
        for row in node_rows
        if row["merged"] and int(row["merged"].get("provenance", {}).get("usage_records", 0)) > 0
    ]
    return {
        "wall_ms": _distribution(wall_values, population),
        "tokens": _distribution(token_values, population),
        "cost_usd": _cost_distribution(node_rows, population),
    }


def _cost_distribution(node_rows: list[dict[str, Any]], population: int) -> dict[str, Any]:
    """Like ``_distribution``, but an included estimated cost must never be
    reported as ``available`` alongside recorded dollars."""
    values: list[float] = []
    estimated = False
    for row in node_rows:
        merged = row["merged"]
        if not merged:
            continue
        cost = merged["totals"].get("cost", {})
        state = cost.get("state")
        usd = cost.get("usd")
        if state in ("available", "estimated") and usd is not None:
            values.append(float(usd))
            estimated = estimated or state == "estimated"
    if not values:
        return {"state": "unavailable", "reason": "no FeatureRun in this graph reports this metric", "mean": None, "median": None, "max": None, "sample_size": 0, "population": population}
    covered = len(values) == population
    if not covered:
        state, reason = "partial", f"lower bound: {len(values)} of {population} FeatureRun(s) report this metric"
    elif estimated:
        state, reason = "estimated", "one or more FeatureRun cost records are estimated, not recorded"
    else:
        state, reason = "available", None
    return {"state": state, "reason": reason, "mean": round(sum(values) / len(values), 3), "median": median(values), "max": max(values), "sample_size": len(values), "population": population}


_WAIT_MS_UNAVAILABLE_REASON = "wait time requires per-node creation and dependency-finish timestamps that are not present in the current run catalog or per-run detail-metrics documents"


def _node_table(node_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-node display rows are lineage-cumulative, not attempt-scoped.

    The per-node table answers "what has this logical node cost so far?",
    matching the FeatureRun inspector's cumulative view — so a node reused
    from a prior attempt still shows the usage of the tries that produced
    its sealed candidate. Only the graph-level ``totals`` block remains
    attempt-scoped (the no-double-counting rule); a reused node's row is
    annotated with its provenance instead of being blanked.
    """
    table = []
    for row in node_rows:
        display = row.get("lineage_merged") or row["merged"]
        if display is None:
            detail = {"state": "unavailable", "reason": row["reason"]}
        elif row.get("reused") and row["merged"] is None:
            detail = {
                "state": "available",
                "reason": (
                    "cumulative across tries executed in prior attempt(s); this "
                    "attempt reused the node's sealed candidate and adds no usage"
                ),
            }
        else:
            detail = {"state": "available", "reason": None}
        table.append({
            "node_id": row["node_id"],
            "status": row["status"],
            "tries": max(row.get("lineage_tries", 0), row["tries"]) if display is not None else row["tries"],
            "detail": detail,
            "totals": display["totals"] if display else None,
            "wait_ms": _metric("unavailable", None, _WAIT_MS_UNAVAILABLE_REASON),
        })
    table.sort(key=lambda row: (row["totals"] is None or row["totals"]["cost"].get("usd") is None, -(row["totals"]["cost"].get("usd") or 0.0) if row["totals"] else 0.0, row["node_id"]))
    return table


def _critical_path_ms(nodes: list[Mapping[str, Any]], node_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Longest dependency chain by attempt-scoped wall time (plan:187-188).

    A missing node wall time along the chain would silently understate the
    longest path if treated as zero, so any such gap degrades the whole
    metric to a documented lower bound rather than a wrong "available".
    """
    if not nodes:
        return _metric("unavailable", None, "no logical nodes in this graph")
    wall_by_id = {row["node_id"]: (row["merged"]["totals"].get("wall_clock_ms") if row["merged"] else None) for row in node_rows}
    depends_by_id = {
        node.get("node_id"): [dep for dep in node.get("depends_on", []) if isinstance(dep, str)]
        for node in nodes
        if isinstance(node.get("node_id"), str)
    }
    incomplete = False
    memo: dict[str, int] = {}

    def finish(node_id: str, seen: frozenset[str]) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id not in depends_by_id or node_id in seen:
            return 0
        wall = wall_by_id.get(node_id)
        nonlocal incomplete
        if not isinstance(wall, int):
            incomplete = True
            wall = 0
        dep_finish = max((finish(dep, seen | {node_id}) for dep in depends_by_id[node_id]), default=0)
        memo[node_id] = dep_finish + wall
        return memo[node_id]

    longest = max((finish(node_id, frozenset()) for node_id in depends_by_id), default=0)
    if incomplete:
        return _metric("partial", longest, "lower bound: one or more nodes on the dependency graph lack a verified attempt-scoped wall-clock time")
    return _metric("available", longest)


def _cache_savings(node_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``cached_input × (input_rate − cached_input_rate)`` per model (plan:191-192)."""
    total = Decimal("0")
    priced_records = 0
    unpriced_records = 0
    for row in node_rows:
        merged = row["merged"]
        if not merged:
            continue
        for model_row in merged.get("by_model", []):
            cached = int(model_row.get("cached_input_tokens", 0) or 0)
            if cached <= 0:
                continue
            price = _ESTIMATED_MODEL_PRICES.get(model_row.get("label"))
            if price is None:
                unpriced_records += 1
                continue
            total += Decimal(cached) * (price["input"] - price["cached_input"]) / Decimal(1_000_000)
            priced_records += 1
    if not priced_records and not unpriced_records:
        return _metric("unavailable", None, "no FeatureRun reports cached input tokens")
    if unpriced_records:
        return _metric("partial", round(float(total), 6), f"lower bound: {unpriced_records} model breakdown record(s) use unrecognized pricing")
    return _metric("available", round(float(total), 6))
