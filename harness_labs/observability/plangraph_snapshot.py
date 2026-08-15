"""Read-only PlanGraph metrics-snapshot builder (``plangraph-metrics-snapshot/1``).

This module computes every number through ``harness_labs.observability.graph_metrics``
(the single shared rollup implementation DM-01 built) so a persisted snapshot
and the live dashboard rollup can never numerically diverge. It only *reads*
run directories -- ``build_snapshot`` never writes anything; ``write_snapshot``
is the sole, atomic, best-effort write path, and it writes exclusively under
``<run-root>/.plan-graph-snapshots/``, never inside a run directory.

Layer note: this module may import ``harness_labs.core`` and
``harness_labs.observability`` only (``tests.test_import_boundaries``
enforces this) -- it never imports ``harness_labs.plangraph``.  Decomposition
text is read as plain ``json.load`` of a digest-checked file, never through
the plangraph plan-loading machinery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from harness_labs.core.audit import AuditError
from harness_labs.observability import graph_metrics
from harness_labs.observability.run_catalog import build_run_catalog, build_run_detail
from harness_labs.observability.run_metrics import project_run_metrics

PROTOCOL = "plangraph-metrics-snapshot/1"
SNAPSHOT_DIRNAME = ".plan-graph-snapshots"
MAX_DECOMPOSITION_BYTES = 4 * 1024 * 1024
MAX_LEDGER_CANDIDATES = 64
# "interrupted" is a genuine terminal status (a crashed/killed controller), but
# its evidence is inherently degraded; a snapshot is built for it only when
# the caller opts in, matching the historical-reconstruction CLI's
# ``--include-interrupted`` flag.
_DEFAULT_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "blocked"})
_INTERRUPTED_STATUS = "interrupted"
_SATISFIED_CRITERION_STATUSES = frozenset({"satisfied", "passed", "succeeded"})


class SnapshotSkipped(Exception):
    """The named graph cannot yield a snapshot right now.

    Raised for expected, non-error conditions (graph absent, not yet
    terminal, interrupted without opt-in) so callers can distinguish an
    honest skip from an unexpected read failure.
    """


def snapshot_path(run_root: Path, graph_attempt_id: str, *, output_dir: Path | None = None) -> Path:
    directory = Path(output_dir).resolve() if output_dir is not None else Path(run_root).resolve() / SNAPSHOT_DIRNAME
    return directory / f"{graph_attempt_id}.json"


def build_snapshot(
    run_root: Path,
    run_id: str,
    *,
    repository: Path | None = None,
    include_interrupted: bool = False,
    reconstructed: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one snapshot document for a terminal PlanGraph run, read-only."""
    run_root = Path(run_root).resolve()
    catalog = build_run_catalog(run_root)
    graph = next((item for item in catalog.get("plan_graphs", []) if item.get("run_id") == run_id), None)
    if graph is None:
        raise SnapshotSkipped(f"{run_id!r} is not a plan-graph run under {run_root}")
    status = graph.get("status")
    allowed = _DEFAULT_TERMINAL_STATUSES | ({_INTERRUPTED_STATUS} if include_interrupted else set())
    if status not in allowed:
        suffix = "" if include_interrupted else " (interrupted graphs require --include-interrupted)"
        raise SnapshotSkipped(f"graph status {status!r} is not eligible for a snapshot{suffix}")

    try:
        own_metrics = project_run_metrics(run_root / run_id)
    except (AuditError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotSkipped(f"graph run directory could not be verified: {exc}") from exc
    own_summary = own_metrics.get("summary")
    state = own_metrics["checkpoint"].get("state", {})
    state = state if isinstance(state, Mapping) else {}

    node_details = _collect_node_details(run_root, catalog)
    budget_ledger = _read_budget_ledger(run_root, run_id)
    metrics = graph_metrics.compute_graph_metrics(graph, catalog, node_details, own_summary=own_summary, budget_ledger=budget_ledger)

    display_name = graph.get("display_name") or run_id
    identity = {
        "logical_graph_id": graph.get("logical_graph_id"),
        "graph_attempt_id": graph.get("graph_attempt_id"),
        "run_id": run_id,
        "plan_path": graph.get("plan_path"),
        "plan_digest": graph.get("plan_digest"),
        "base_commit": state.get("base_commit") if isinstance(state.get("base_commit"), str) else None,
        "repository_id": state.get("repository_id") if isinstance(state.get("repository_id"), str) else None,
    }

    feature_runs = _feature_run_documents(catalog, graph, node_details)
    criteria_text, criteria_text_unavailable, criteria_reason = _criteria_text(repository, state, identity["plan_digest"])
    outcome = _outcome(graph, feature_runs, repository, identity["base_commit"], criteria_text)
    narrative = _narrative(display_name, status, outcome, metrics)
    outcome["narrative"] = narrative

    data_quality = _data_quality(own_summary, metrics, criteria_text_unavailable, criteria_reason, reconstructed)
    timing = _timing(metrics, own_summary, own_metrics.get("manifest"), own_metrics.get("events") or [])
    generated_at = _timestamp(now or datetime.now(timezone.utc))

    return {
        "protocol": PROTOCOL,
        "identity": identity,
        "display_name": display_name,
        "status": status,
        "timing": timing,
        "graph_metrics": metrics,
        "feature_runs": feature_runs,
        "outcome": outcome,
        "data_quality": data_quality,
        "provenance": {
            "generated_at": generated_at,
            "generator": "harness_labs.observability.plangraph_snapshot/1",
            "run_root": str(run_root),
            "reconstructed": reconstructed,
        },
    }


def write_snapshot(
    run_root: Path,
    snapshot: Mapping[str, Any],
    *,
    output_dir: Path | None = None,
    force: bool = False,
) -> tuple[Path, bool]:
    """Atomically, idempotently write one snapshot document.

    Returns ``(path, wrote)``: ``wrote`` is ``False`` when a snapshot already
    exists at the target path and ``force`` was not given (idempotent no-op).
    Never writes inside a run directory: the target is always
    ``<run-root>/.plan-graph-snapshots/`` (or the supplied ``output_dir``),
    a sibling of run directories, exactly like the existing
    ``.plan-graph-budgets`` / ``.plan-graph-locks`` infrastructure dirs.
    """
    identity = snapshot["identity"]
    graph_attempt_id = identity.get("graph_attempt_id") or identity["run_id"]
    if not isinstance(graph_attempt_id, str) or not graph_attempt_id or Path(graph_attempt_id).name != graph_attempt_id:
        raise SnapshotSkipped("snapshot identity does not name one safe file")
    target = snapshot_path(run_root, graph_attempt_id, output_dir=output_dir)
    if target.is_symlink():
        raise SnapshotSkipped("snapshot target must not be a symlink")
    if target.exists() and not force:
        return target, False
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-snapshot-", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target, True


def emit_best_effort_snapshot(run_root: Path, run_id: str, *, repository: Path | None = None) -> None:
    """Build and write one snapshot, swallowing every failure as a warning.

    Called by the runner and recovery-coordinator scripts after a graph
    attempt reaches a terminal state.  A snapshot failure must never alter
    run status or journals, so this function never raises; it prints a
    warning to stderr instead.
    """
    try:
        snapshot = build_snapshot(run_root, run_id, repository=repository)
    except SnapshotSkipped:
        return
    except Exception as exc:  # noqa: BLE001 - a snapshot failure must never fail the caller
        print(f"PlanGraph metrics-snapshot build failed for {run_id!r} (continuing): {exc}", file=sys.stderr)
        return
    try:
        write_snapshot(run_root, snapshot)
    except Exception as exc:  # noqa: BLE001 - see above
        print(f"PlanGraph metrics-snapshot write failed for {run_id!r} (continuing): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Node detail collection and budget-ledger lookup (thin selection over the
# shared graph_metrics functions; no metric arithmetic is duplicated here)
# ---------------------------------------------------------------------------

def _collect_node_details(run_root: Path, catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for run in catalog.get("feature_runs", []):
        run_id = run.get("run_id") if isinstance(run, Mapping) else None
        if not isinstance(run_id, str) or run.get("status") == "corrupt":
            continue
        try:
            detail = build_run_detail(run_root, run_id)
        except (AuditError, OSError, ValueError, json.JSONDecodeError):
            continue
        details[run_id] = detail["metrics"]
    return details


def _feature_run_documents(catalog: Mapping[str, Any], graph: Mapping[str, Any], node_details: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per logical node: cumulative cross-attempt metrics, the same
    merged shape ``/api/feature-runs/<id>`` already serves live."""
    details = {run_id: {"metrics": metrics} for run_id, metrics in node_details.items()}
    graph_metrics.apply_cumulative_node_metrics(catalog, details)
    display_names = {
        item.get("run_id"): item.get("display_name")
        for item in catalog.get("feature_runs", [])
        if isinstance(item, Mapping) and isinstance(item.get("run_id"), str)
    }
    rows = []
    for node in graph.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        feature_run_id = node.get("feature_run_id")
        node_id = node.get("node_id")
        entry = details.get(feature_run_id) if isinstance(feature_run_id, str) else None
        merged = entry["metrics"] if entry else None
        if isinstance(feature_run_id, str):
            tries = len(graph_metrics.node_history_run_ids(catalog, feature_run_id))
        else:
            tries = 0
        if merged is not None:
            reason = None
        elif not isinstance(feature_run_id, str):
            reason = "no FeatureRun is recorded for this node"
        else:
            reason = "this node's FeatureRun detail could not be verified"
        rows.append({
            "node_id": node_id,
            "objective": node.get("objective"),
            "display_name": display_names.get(feature_run_id) or node_id or feature_run_id,
            "status": node.get("status"),
            "feature_run_id": feature_run_id,
            "tries": tries,
            "detail": {"state": "available", "reason": None} if merged is not None else {"state": "unavailable", "reason": reason},
            "metrics": merged,
        })
    return rows


def _find_ledger_path(run_root: Path, run_id: str) -> tuple[Path | None, str | None]:
    directory = run_root / ".plan-graph-budgets"
    if not directory.is_dir():
        return None, "no retry-budget ledger directory exists under this run root"
    candidates = sorted(item for item in directory.iterdir() if item.is_file() and not item.is_symlink() and item.suffix == ".jsonl")
    if not candidates:
        return None, "no retry-budget ledger files exist under this run root"
    if len(candidates) == 1:
        return candidates[0], None
    matches = [item for item in candidates[:MAX_LEDGER_CANDIDATES] if _ledger_references_attempt(item, run_id)]
    if len(matches) == 1:
        return matches[0], None
    return None, f"{len(candidates)} retry-budget ledgers exist under this run root and this graph's lineage could not be uniquely identified"


def _ledger_references_attempt(path: Path, run_id: str) -> bool:
    try:
        if path.stat().st_size > graph_metrics.MAX_LEDGER_BYTES:
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines[: graph_metrics.MAX_LEDGER_LINES]:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping) and record.get("graph_attempt_id") == run_id:
            return True
    return False


def _read_budget_ledger(run_root: Path, run_id: str) -> dict[str, Any]:
    path, reason = _find_ledger_path(run_root, run_id)
    if path is None:
        return {"state": "unavailable", "reason": reason, "graph_launches": None, "gate_invocations": None, "repair_dispatches": None, "structural_decisions": None}
    return graph_metrics.read_budget_ledger(path)


# ---------------------------------------------------------------------------
# Digest-checked decomposition text (plain json.load; no plangraph import)
# ---------------------------------------------------------------------------

def _criteria_text(repository: Path | None, state: Mapping[str, Any], plan_digest: Any) -> tuple[dict[str, Any] | None, bool, str | None]:
    if repository is None:
        return None, True, "no repository was supplied to the builder; decomposition text could not be read"
    plan_relpath = state.get("plan")
    if not isinstance(plan_relpath, str) or not plan_relpath or not isinstance(plan_digest, str) or not plan_digest:
        return None, True, "checkpoint does not record a decomposition path and digest"
    repo_root = Path(repository).resolve()
    candidate = repo_root.joinpath(*Path(plan_relpath).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None, True, "recorded decomposition file is not present in the supplied repository"
    if resolved.is_symlink() or (resolved != repo_root and repo_root not in resolved.parents):
        return None, True, "recorded decomposition path is unsafe"
    try:
        if resolved.stat().st_size > MAX_DECOMPOSITION_BYTES:
            return None, True, "recorded decomposition file exceeds the size limit"
        raw = resolved.read_bytes()
    except OSError:
        return None, True, "recorded decomposition file could not be read"
    if hashlib.sha256(raw).hexdigest() != plan_digest:
        return None, True, "recorded decomposition file no longer matches the recorded plan digest"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, True, "recorded decomposition file is not valid JSON"
    if not isinstance(payload, Mapping):
        return None, True, "recorded decomposition file is not a JSON object"
    sections = payload.get("plan_sections")
    criteria = payload.get("acceptance_criteria")
    sections = {key: value for key, value in sections.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(sections, Mapping) else {}
    criteria = {key: value for key, value in criteria.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(criteria, Mapping) else {}
    return {"plan_sections": sections, "acceptance_criteria": criteria}, False, None


# ---------------------------------------------------------------------------
# Outcome: per-node status/criteria, graph-level counts, git-derived delta,
# templated narrative
# ---------------------------------------------------------------------------

def _outcome(graph: Mapping[str, Any], feature_runs: list[dict[str, Any]], repository: Path | None, base_commit: Any, criteria_text: Mapping[str, Any] | None) -> dict[str, Any]:
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, Mapping)]
    rows_by_node_id = {row["node_id"]: row for row in feature_runs}
    node_rows = []
    attempted = succeeded = blocked = failed = 0
    for node in nodes:
        status = node.get("status")
        if status != "queued":
            attempted += 1
        if status == "succeeded":
            succeeded += 1
        elif status == "blocked":
            blocked += 1
        elif status == "failed":
            failed += 1
        row = rows_by_node_id.get(node.get("node_id")) or {}
        merged = row.get("metrics")
        quality = merged.get("quality") if isinstance(merged, Mapping) else None
        evidence = node.get("evidence") if isinstance(node.get("evidence"), Mapping) else {}
        detail = row.get("detail") or {}
        node_rows.append({
            "node_id": node.get("node_id"),
            "objective": node.get("objective"),
            "status": status,
            "criteria_satisfied": quality.get("criteria_satisfied") if isinstance(quality, Mapping) else None,
            "criteria_total": quality.get("criteria_total") if isinstance(quality, Mapping) else None,
            "criteria_state": "available" if isinstance(quality, Mapping) else "unavailable",
            "evidence_reason": None if status == "succeeded" else (evidence.get("reason") or detail.get("reason")),
        })
    execution = graph.get("execution") if isinstance(graph.get("execution"), Mapping) else {}
    integration = execution.get("integration") if isinstance(execution.get("integration"), Mapping) else {}
    final_commit = integration.get("staging_head") if isinstance(integration.get("staging_head"), str) else None
    delta = _git_delta(repository, base_commit, final_commit, nodes)
    return {
        "nodes": node_rows,
        "nodes_total": len(nodes),
        "nodes_attempted": attempted,
        "nodes_succeeded": succeeded,
        "nodes_blocked": blocked,
        "nodes_failed": failed,
        "delta": delta,
        "plan_sections": criteria_text.get("plan_sections") if criteria_text else None,
        "acceptance_criteria": criteria_text.get("acceptance_criteria") if criteria_text else None,
    }


_SHORTSTAT_FILES = re.compile(r"(\d+) files? changed")
_SHORTSTAT_INSERTIONS = re.compile(r"(\d+) insertions?\(\+\)")
_SHORTSTAT_DELETIONS = re.compile(r"(\d+) deletions?\(-\)")


def _parse_shortstat(text: str) -> tuple[int, int, int]:
    files = _SHORTSTAT_FILES.search(text)
    insertions = _SHORTSTAT_INSERTIONS.search(text)
    deletions = _SHORTSTAT_DELETIONS.search(text)
    return (
        int(files.group(1)) if files else 0,
        int(insertions.group(1)) if insertions else 0,
        int(deletions.group(1)) if deletions else 0,
    )


def _git_delta(repository: Path | None, base_commit: Any, final_commit: str | None, nodes: list[Mapping[str, Any]]) -> dict[str, Any]:
    node_rows = [{"node_id": node.get("node_id"), "candidate_commit": node.get("candidate_commit")} for node in nodes]
    base = base_commit if isinstance(base_commit, str) and base_commit else None

    def unavailable(reason: str) -> dict[str, Any]:
        return {"state": "unavailable", "reason": reason, "base_commit": base, "final_integrated_commit": final_commit, "files_changed": None, "insertions": None, "deletions": None, "nodes": node_rows}

    if repository is None:
        return unavailable("no repository was supplied to the builder")
    if base is None:
        return unavailable("base commit is not recorded")
    if final_commit is None:
        return unavailable("no integrated commit is recorded for this graph")
    repo = Path(repository).resolve()
    if not (repo / ".git").exists():
        return unavailable("supplied repository path is not a git repository")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--shortstat", f"{base}..{final_commit}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return unavailable("git could not be run to compute change stats")
    if result.returncode != 0:
        return unavailable("git could not resolve the recorded commits")
    files_changed, insertions, deletions = _parse_shortstat(result.stdout)
    return {"state": "available", "reason": None, "base_commit": base, "final_integrated_commit": final_commit, "files_changed": files_changed, "insertions": insertions, "deletions": deletions, "nodes": node_rows}


def _narrative(display_name: str, status: Any, outcome: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    """A templated narrative: every slot is a value already present elsewhere
    in this document (display_name, status, outcome counts, graph_metrics
    totals) -- never free-form generation."""
    parts = [f"{display_name}: {status}", f"{outcome['nodes_succeeded']} of {outcome['nodes_total']} node(s) succeeded"]
    if outcome["nodes_blocked"]:
        parts.append(f"{outcome['nodes_blocked']} blocked")
    if outcome["nodes_failed"]:
        parts.append(f"{outcome['nodes_failed']} failed")
    wall = metrics["timing"]["wall_clock_ms"]
    if wall["state"] == "available" and wall["value"] is not None:
        parts.append(f"wall clock {wall['value']} ms")
    tokens = metrics["totals"]["tokens"]
    if tokens["state"] in ("available", "partial") and tokens.get("total_tokens") is not None:
        parts.append(f"{tokens['total_tokens']} total tokens")
    cost = metrics["totals"]["cost"]
    if cost["state"] in ("available", "estimated") and cost.get("usd") is not None:
        parts.append(f"est. cost ${cost['usd']:.2f}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Timing and data-quality
# ---------------------------------------------------------------------------

def _timing(metrics: Mapping[str, Any], own_summary: Mapping[str, Any] | None, manifest: Mapping[str, Any] | None, events: list[Any]) -> dict[str, Any]:
    finished_at = None
    if isinstance(own_summary, Mapping) and isinstance(own_summary.get("finished_at"), str):
        finished_at = own_summary["finished_at"]
    elif isinstance(manifest, Mapping) and isinstance(manifest.get("finished_at"), str):
        finished_at = manifest["finished_at"]
    wall_clock_ms = metrics["timing"]["wall_clock_ms"]
    if wall_clock_ms["state"] != "available":
        derived_ms = _derive_wall_clock_ms(events)
        if derived_ms is not None:
            wall_clock_ms = {
                "state": "partial",
                "value": derived_ms,
                "reason": "derived from first/last verified journal event timestamps (graph summary.json is unavailable)",
            }
    return {"started_at": metrics["timing"]["started_at"], "finished_at": finished_at, "wall_clock_ms": wall_clock_ms}


def _derive_wall_clock_ms(events: list[Any]) -> int | None:
    """Fallback wall-clock estimate for the pre-2026-08-05 corpus shape whose
    graphs have no ``summary.json``: the span between the first and last
    verified journal event timestamps.  Only used for the snapshot's own
    ``timing`` block -- the shared ``graph_metrics`` rollup never estimates a
    wall clock this way (its wall-time contract is ``summary.json`` only)."""
    timestamps = [parsed for parsed in (_parse_event_timestamp(event) for event in events) if parsed is not None]
    if len(timestamps) < 2:
        return None
    span_ms = (max(timestamps) - min(timestamps)).total_seconds() * 1000
    return max(0, round(span_ms))


def _parse_event_timestamp(event: Any) -> datetime | None:
    value = event.get("timestamp") if isinstance(event, Mapping) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _data_quality(own_summary: Mapping[str, Any] | None, metrics: Mapping[str, Any], criteria_text_unavailable: bool, criteria_reason: str | None, reconstructed: bool) -> dict[str, Any]:
    notes: list[str] = []
    summary_missing = own_summary is None
    if summary_missing:
        notes.append("graph summary.json is unavailable; wall clock is reported unavailable unless derivable from another verified source")
    token_state = metrics["totals"]["tokens"]["state"]
    token_records_missing = token_state == "unavailable"
    if token_records_missing:
        notes.append("no FeatureRun in this graph reports verified token usage")
    cost_state = metrics["totals"]["cost"]["state"]
    busy = metrics["totals"]["agent_busy_ms"]
    busy_unavailable_reason = busy["reason"] if busy["state"] != "available" else None
    if criteria_text_unavailable and criteria_reason:
        notes.append(criteria_reason)
    completeness = _completeness_grade(summary_missing, token_state, cost_state, busy["state"])
    return {
        "summary_missing": summary_missing,
        "token_records_missing": token_records_missing,
        "cost_state": cost_state,
        "busy_unavailable_reason": busy_unavailable_reason,
        "criteria_text_unavailable": criteria_text_unavailable,
        "reconstructed": reconstructed,
        "reconstruction_notes": notes,
        "completeness": completeness,
    }


def _completeness_grade(summary_missing: bool, token_state: str, cost_state: str, busy_state: str) -> str:
    signals = (not summary_missing, token_state in ("available", "partial"), cost_state in ("available", "estimated", "partial"), busy_state == "available")
    covered = sum(1 for signal in signals if signal)
    if covered == len(signals):
        return "complete"
    if covered == 0:
        return "minimal"
    return "partial"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
