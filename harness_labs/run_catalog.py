"""Read-only verified catalog for FeatureRun and PlanGraph audit directories."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from .audit import AuditError
from .run_metrics import TERMINAL_STATUSES, availability, project_run_metrics

Clock = Callable[[], datetime]
ProcessProbe = Callable[[int], str | None]
_DESCRIPTOR_FIELDS = frozenset({"protocol", "run_kind", "run_id", "created_at", "objective", "evidence_classification", "repository", "approved_plan", "parent_correlation"})
_PLAN_GRAPH_LINEAGE_FIELDS = frozenset({"logical_graph_id", "graph_attempt_id", "predecessor_attempt_id"})
_LEASE_FIELDS = frozenset({"protocol", "run_id", "controller_instance_id", "hostname", "pid", "process_start_token", "heartbeat_sequence", "heartbeat_at", "controller_kind"})
_ESTIMATED_MODEL_PRICES = {
    "gpt-5.6-sol": {"input": Decimal("5.00"), "cached_input": Decimal("0.50"), "output": Decimal("30.00"), "source": "https://developers.openai.com/api/docs/models/gpt-5.6-sol"},
    "gpt-5.6-terra": {"input": Decimal("2.50"), "cached_input": Decimal("0.25"), "output": Decimal("15.00"), "source": "https://developers.openai.com/api/docs/models/gpt-5.6-terra"},
    "gpt-5.6-luna": {"input": Decimal("1.00"), "cached_input": Decimal("0.10"), "output": Decimal("6.00"), "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna"},
}
_LONG_CONTEXT_THRESHOLD = 272_000


def build_run_catalog(source_root: Path, *, clock: Clock | None = None, process_probe: ProcessProbe | None = None, heartbeat_freshness_seconds: float = 30.0, excluded_runs: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Discover direct, non-symlinked run directories under one explicit root."""
    if heartbeat_freshness_seconds <= 0:
        raise ValueError("heartbeat_freshness_seconds must be positive")
    root = Path(source_root).resolve()
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    probe = process_probe or _local_process_start_token
    diagnostics: list[dict[str, str | None]] = []
    records: list[dict[str, Any]] = []
    exclusions = dict(excluded_runs or {})
    if not root.is_dir():
        return _snapshot(root, now, diagnostics, records, "source root is unavailable")
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name.startswith("."):
            # Ledger and lock bookkeeping (.plan-graph-budgets,
            # .plan-graph-locks) live beside run directories; they are
            # infrastructure, not runs, and must not poison catalog IDs.
            continue
        if entry.name in exclusions:
            reason = exclusions[entry.name]
            diagnostics.append(_diagnostic("bounded_run_rejected", reason, entry.name))
            records.append(_corrupt_record(entry.name, reason))
            continue
        try:
            records.append(_project_run(entry, root, now, probe, heartbeat_freshness_seconds))
        except (AuditError, OSError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(_diagnostic("corrupt_run", str(exc), entry.name))
            records.append(_corrupt_record(entry.name, str(exc)))
    return _snapshot(root, now, diagnostics, records)


def build_run_detail(source_root: Path, run_id: str) -> dict[str, Any]:
    if not run_id or Path(run_id).name != run_id:
        raise ValueError("run_id must name one direct child run directory")
    root = Path(source_root).resolve()
    directory = root / run_id
    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
        raise AuditError("run directory is unavailable")
    metrics = project_run_metrics(directory)
    descriptor, raw = _descriptor(directory / "descriptor.json")
    if descriptor:
        _validate_descriptor(descriptor, metrics["run_id"])
        if not _descriptor_is_bound(raw, metrics["events"]):
            raise AuditError("descriptor is not bound by the verified journal")
    return _detail(metrics, descriptor)


class RunCatalog:
    def __init__(self, source_root: Path, **options: Any) -> None:
        self.source_root, self.options = Path(source_root), options
    def snapshot(self) -> dict[str, Any]:
        return build_run_catalog(self.source_root, **self.options)
    def detail(self, run_id: str) -> dict[str, Any]:
        return build_run_detail(self.source_root, run_id)


def merge_run_catalogs(
    catalogs: list[tuple[Path, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Merge verified single-root projections without guessing identity.

    Run IDs are the public API keys and therefore must be globally unique.  If
    the same ID appears under more than one configured root, every conflicting
    record is withheld and a diagnostic is emitted rather than selecting a
    source by configuration order.
    """
    if not catalogs:
        raise ValueError("at least one run catalog is required")

    roots = [str(Path(root).resolve()) for root, _ in catalogs]
    records_by_id: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    diagnostics: list[dict[str, str | None]] = []
    source_states: list[str] = []
    generated_at: list[str] = []

    for root, catalog in catalogs:
        source_root = str(Path(root).resolve())
        source_states.append(str(catalog.get("availability", {}).get("state", "unavailable")))
        if isinstance(catalog.get("generated_at"), str):
            generated_at.append(str(catalog["generated_at"]))
        for item in catalog.get("diagnostics", []):
            if isinstance(item, Mapping):
                diagnostic = dict(item)
                diagnostic["source_root"] = source_root
                diagnostics.append(diagnostic)
        for family in ("plan_graphs", "feature_runs"):
            for item in catalog.get(family, []):
                if not isinstance(item, Mapping) or not isinstance(item.get("run_id"), str):
                    continue
                record = deepcopy(dict(item))
                record["source_root"] = source_root
                records_by_id.setdefault(record["run_id"], []).append((family, record))

    ambiguous = {run_id: entries for run_id, entries in records_by_id.items() if len(entries) > 1}
    for run_id, entries in sorted(ambiguous.items()):
        locations = sorted({str(record["source_root"]) for _, record in entries})
        diagnostics.append({
            "code": "ambiguous_run_id",
            "message": f"run ID exists in multiple audit roots: {', '.join(locations)}",
            "run_id": run_id,
            "source_root": None,
        })

    accepted = {
        run_id: entries[0]
        for run_id, entries in records_by_id.items()
        if run_id not in ambiguous
    }
    graphs = sorted(
        (record for family, record in accepted.values() if family == "plan_graphs"),
        key=lambda record: record["run_id"],
    )
    features = sorted(
        (record for family, record in accepted.values() if family == "feature_runs"),
        key=lambda record: record["run_id"],
    )

    # A graph and its children may live in different roots.  Re-evaluate the
    # descriptor correlation after merging because each single-root projector
    # correctly treated an absent local child as only partial evidence.
    for graph in graphs:
        for node in graph.get("nodes", []):
            if not isinstance(node, dict) or node.get("feature_run_id") is None:
                continue
            child = next(
                (feature for feature in features if _node_matches_child(graph, node, feature)),
                None,
            )
            if child is None:
                # A graph and a descriptor-less legacy child may also live in
                # different roots; exact run-id equality still binds them.
                child = _id_match_child(graph, node, features)
            if child is None:
                node["evidence"] = availability("partial", "child correlation is not verified")
            elif _correlation_is_id_matched(child):
                # Never let a merged view upgrade an id-matched correlation to
                # the fully-available evidence reserved for attested children.
                node["evidence"] = availability("partial", _ID_MATCH_REASON)
            else:
                node["evidence"] = availability("available")
            if child is not None:
                node["liveness"] = dict(child["liveness"])

    ungrouped = [
        feature for feature in features
        if not any(
            _node_matches_child(graph, node, feature)
            for graph in graphs
            for node in graph.get("nodes", [])
            if isinstance(node, Mapping)
        )
    ]
    if all(state == "unavailable" for state in source_states) and not graphs and not features:
        source = availability("unavailable", "all configured audit roots are unavailable")
    elif diagnostics or any(state != "available" for state in source_states):
        source = availability("partial", "one or more audit roots or runs are unavailable")
    else:
        source = availability("available")
    revision = hashlib.sha256(json.dumps(
        {
            "source_roots": roots,
            "source_states": source_states,
            "diagnostics": diagnostics,
            "plan_graphs": graphs,
            "feature_runs": features,
            "ungrouped_feature_runs": ungrouped,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    return {
        "protocol": "harness-run-catalog-snapshot/1",
        "revision": revision,
        "generated_at": max(generated_at) if generated_at else _timestamp(datetime.now(timezone.utc)),
        # Retained for older clients; source_roots is authoritative.
        "source_root": roots[0],
        "source_roots": roots,
        "availability": source,
        "diagnostics": diagnostics,
        "plan_graphs": graphs,
        "feature_runs": features,
        "ungrouped_feature_runs": ungrouped,
    }


def _project_run(directory: Path, root: Path, now: datetime, probe: ProcessProbe, freshness: float) -> dict[str, Any]:
    if directory.resolve().parent != root:
        raise AuditError("run directory escapes source root")
    metrics = project_run_metrics(directory)
    descriptor, raw = _descriptor(directory / "descriptor.json")
    if descriptor:
        _validate_descriptor(descriptor, metrics["run_id"])
        if not _descriptor_is_bound(raw, metrics["events"]):
            raise AuditError("descriptor is not bound by the verified journal")
    kind = descriptor.get("run_kind") if descriptor else None
    status = _graph_status(metrics, metrics["status"]) if kind == "plan_graph" else metrics["status"]
    liveness = _liveness(directory, metrics["run_id"], kind, status, now, probe, freshness)
    evidence = availability("available") if metrics["manifest"] else availability("unavailable", "no terminal manifest exists")
    if not descriptor:
        evidence = availability("partial", "descriptor was absent for the legacy run")
    record: dict[str, Any] = {"run_id": metrics["run_id"], "kind": kind or "legacy_feature_run", "status": status, "liveness": liveness, "evidence": evidence}
    if kind == "plan_graph":
        state = metrics["checkpoint"].get("state", {})
        state = state if isinstance(state, Mapping) else {}
        approved_plan = descriptor.get("approved_plan", {})
        approved_plan = approved_plan if isinstance(approved_plan, Mapping) else {}
        plan_digest = approved_plan.get("sha256")
        graph_digest = state.get("plan_graph_digest") or plan_digest
        record.update({
            "created_at": descriptor["created_at"],
            "plan_path": approved_plan.get("path"),
            "plan_digest": plan_digest,
            "plan_graph_digest": graph_digest,
            # Older audited descriptors predate the lineage extension.  Their
            # run ID is the only durable graph and attempt identity, so retain
            # that compatibility without inferring a predecessor.
            "logical_graph_id": descriptor.get("logical_graph_id", metrics["run_id"]),
            "graph_attempt_id": descriptor.get("graph_attempt_id", metrics["run_id"]),
            "predecessor_attempt_id": descriptor.get("predecessor_attempt_id"),
            # PG-06 does not yet receive a retention policy from an audited
            # descriptor or checkpoint.  Make that boundary observable rather
            # than treating references as retained by implication.
            "retention_constraints": availability(
                "unavailable",
                "retention constraints were not recorded in the audited descriptor or checkpoint",
            ),
        })
        record["nodes"] = _nodes(metrics)
        record["execution"] = _graph_execution(metrics)
        del record["kind"]
    else:
        record["correlation"] = _correlation(descriptor)
        record["_integration_merge_commits"] = _integration_merge_commits(metrics["events"])
    return record


def _snapshot(root: Path, now: datetime, diagnostics: list[dict[str, str | None]], records: list[dict[str, Any]], reason: str | None = None) -> dict[str, Any]:
    graphs = [record for record in records if "nodes" in record]
    features = [record for record in records if "nodes" not in record]
    graph_ids = {record["run_id"] for record in graphs}
    for graph in graphs:
        for node in graph["nodes"]:
            child = next((record for record in features if _node_matches_child(graph, node, record)), None)
            if child is not None:
                node["liveness"] = dict(child["liveness"])
            if node["feature_run_id"] is not None and child is None:
                # A recovery disposition is durable graph evidence.  Do not
                # replace it with the separate, less-specific correlation
                # warning merely because a child catalog record is absent.
                if node.get("evidence", {}).get("state") != "available":
                    continue
                id_matched = _id_match_child(graph, node, features)
                if id_matched is not None:
                    node["liveness"] = dict(id_matched["liveness"])
                    node["evidence"] = availability("partial", _ID_MATCH_REASON)
                    continue
                legacy_matches = [
                    record for record in features
                    if node.get("_candidate_commit") in record.get("_integration_merge_commits", ())
                ]
                if node.get("_candidate_commit") and len(legacy_matches) == 1:
                    node["feature_run_id"] = legacy_matches[0]["run_id"]
                    node["evidence"] = availability(
                        "partial",
                        "legacy child recovered from a unique audited integration commit; parent correlation is unavailable",
                    )
                else:
                    node["evidence"] = availability("partial", "child correlation is not verified")
    ungrouped = [
        record for record in features
        if not any(_node_matches_child(graph, node, record) for graph in graphs for node in graph["nodes"])
    ]
    source = availability("unavailable", reason) if reason else availability("partial", "one or more runs are corrupt") if diagnostics else availability("available")
    for graph in graphs:
        for node in graph["nodes"]:
            node.pop("_candidate_commit", None)
    for feature in features:
        feature.pop("_integration_merge_commits", None)
    revision = hashlib.sha256(json.dumps({"records": records, "diagnostics": diagnostics}, sort_keys=True).encode()).hexdigest()
    return {"protocol": "harness-run-catalog-snapshot/1", "revision": revision, "generated_at": _timestamp(now), "source_root": str(root), "availability": source, "diagnostics": diagnostics, "plan_graphs": graphs, "feature_runs": features, "ungrouped_feature_runs": ungrouped}


def _detail(metrics: Mapping[str, Any], descriptor: Mapping[str, Any] | None) -> dict[str, Any]:
    state = metrics["checkpoint"].get("state", {})
    controller = state.get("controller", {}) if isinstance(state, Mapping) else {}
    controller = controller if isinstance(controller, Mapping) else {}
    events = metrics["events"]
    git = [event["payload"] for event in events if str(event.get("event_type", "")).startswith("git_")]
    def family_present(key: str, reason: str) -> dict[str, str | None]:
        return availability("available") if key in controller else availability("unavailable", reason)
    return {"lifecycle": events, "criteria": controller.get("criteria", []), "tasks": controller.get("tasks", []), "findings": controller.get("findings", []), "decisions": controller.get("decisions", []), "evidence_metadata": metrics["manifest"].get("artifacts", []) if metrics["manifest"] else [], "git_custody": git, "usage": metrics["summary"], "metrics": _detail_metrics(metrics), "timing": {"started_at": metrics["checkpoint"].get("started_at"), "updated_at": metrics["checkpoint"].get("updated_at")}, "availability": {"lifecycle": metrics["availability"]["journal"], "criteria": family_present("criteria", "criteria were not recorded"), "tasks": family_present("tasks", "tasks were not recorded"), "findings": family_present("findings", "findings were not recorded"), "evidence_metadata": availability("available") if metrics["manifest"] is not None else availability("unavailable", "manifest is unavailable"), "git_custody": availability("available"), "usage": availability("available") if metrics["summary"] is not None else availability("unavailable", "summary is unavailable")}, "descriptor": descriptor}


def _detail_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Project verified transport records into reconciled operator metrics."""
    records: list[dict[str, Any]] = []
    for event in metrics.get("events", []):
        if event.get("event_type") != "backend_transport":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        actor = event.get("actor") if isinstance(event.get("actor"), Mapping) else {}
        attempt = event.get("attempt_id") or actor.get("id") or "unknown agent"
        model = str(payload.get("model") or usage.get("model") or "unavailable")
        recorded_cost = _nonnegative_number(usage.get("cost_usd"))
        estimated_cost = _estimated_api_cost(model, usage) if recorded_cost is None else None
        records.append({
            "agent": str(attempt),
            "phase": _attempt_phase(str(attempt)),
            "agent_type": str(actor.get("role") or "unknown"),
            "model": model,
            "effort": str(payload.get("reasoning") or "unavailable"),
            "backend": str(event.get("backend_id") or payload.get("transport") or payload.get("backend_id") or "unavailable"),
            "calls": 1,
            "input_tokens": _nonnegative_int(usage.get("input_tokens")),
            "cached_input_tokens": _nonnegative_int(usage.get("cached_input_tokens")),
            "output_tokens": _nonnegative_int(usage.get("output_tokens")),
            # claude-print result usage is CUMULATIVE across every API turn of
            # the subprocess; a per-invocation context peak is not derivable
            # from it, and reporting the cumulative sum as "peak" inflated the
            # metric by the number of agentic turns.
            "peak_input_tokens": None,
            "duration_ms": _nonnegative_int(event.get("duration_ms")),
            "cost_usd": recorded_cost if recorded_cost is not None else estimated_cost["usd"] if estimated_cost else None,
            "cost_kind": "authoritative" if recorded_cost is not None else "estimated" if estimated_cost else "unavailable",
            "cost_source": usage.get("pricing_source") if recorded_cost is not None else estimated_cost["source"] if estimated_cost else None,
            "long_context_priced": estimated_cost["long_context"] if estimated_cost else False,
        })
    stages = _execution_stages(metrics)
    collection_method = "verified backend_transport journal events"
    if not records:
        records.extend(_codex_token_usage_records(metrics, stages))
        if records:
            collection_method = "verified cumulative Codex token-usage notifications"
    totals = _aggregate_metric_rows(records)
    summary = metrics.get("summary") if isinstance(metrics.get("summary"), Mapping) else {}
    summary_usage = summary.get("usage") if isinstance(summary.get("usage"), Mapping) else {}
    if isinstance(summary_usage.get("wall_clock_ms"), int):
        totals["wall_clock_ms"] = summary_usage["wall_clock_ms"]
    else:
        totals["wall_clock_ms"] = None
    state = metrics.get("checkpoint", {}).get("state", {})
    state = state if isinstance(state, Mapping) else {}
    controller = state.get("controller") if isinstance(state.get("controller"), Mapping) else {}
    criteria = controller.get("criteria", {})
    findings = controller.get("findings", {})
    criteria_values = list(criteria.values()) if isinstance(criteria, Mapping) else criteria if isinstance(criteria, list) else []
    finding_values = list(findings.values()) if isinstance(findings, Mapping) else findings if isinstance(findings, list) else []
    review_fix = state.get("review_fix") if isinstance(state.get("review_fix"), Mapping) else {}
    verification = state.get("verification") if isinstance(state.get("verification"), Mapping) else {}
    recorded_verification_repairs = len({record["agent"] for record in records if "/verification-repair/" in record["agent"]})
    checkpoint_repairs = len(verification.get("repair_attempts", [])) if isinstance(verification.get("repair_attempts"), list) else _nonnegative_int(verification.get("repair_attempts"))
    return {
        "protocol": "harness-run-detail-metrics/1",
        "totals": totals,
        "quality": {
            "criteria_total": len(criteria_values),
            "criteria_satisfied": sum(1 for item in criteria_values if isinstance(item, Mapping) and str(item.get("status", "")).lower() in {"satisfied", "passed", "succeeded"}),
            "findings_total": len(finding_values),
            "open_findings": sum(1 for item in finding_values if not isinstance(item, Mapping) or str(item.get("status", "open")).lower() not in {"closed", "resolved", "fixed"}),
            "review_cycles": _nonnegative_int(review_fix.get("cycles")),
            "verification_repairs": max(recorded_verification_repairs, checkpoint_repairs),
        },
        "by_phase": _breakdown(records, "phase"),
        "by_agent": _breakdown(records, "agent"),
        "by_agent_type": _breakdown(records, "agent_type"),
        "by_model": _breakdown(records, "model"),
        "by_effort": _breakdown(records, "effort"),
        "by_backend": _breakdown(records, "backend"),
        "stages": stages,
        "provenance": {
            "usage_records": sum(record["calls"] for record in records),
            "collection_method": collection_method,
            "peak_context_definition": (
                "maximum observed input_tokens in one backend invocation; "
                "unavailable when every record reports only cumulative "
                "session usage (claude-print results)"
            ),
        },
    }


def _codex_token_usage_records(metrics: Mapping[str, Any], stages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read numeric usage only from hash-verified Codex transport artifacts."""
    supplied = metrics.get("run_dir")
    if not isinstance(supplied, str):
        return []
    run_dir = Path(supplied).resolve()
    updates: list[dict[str, Any]] = []
    for event in metrics.get("events", []):
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if event.get("event_type") != "transport_message" or payload.get("direction") != "inbound" or payload.get("method") != "thread/tokenUsage/updated":
            continue
        for artifact in event.get("artifacts", []):
            if not isinstance(artifact, Mapping) or not isinstance(artifact.get("path"), str):
                continue
            target = run_dir.joinpath(*Path(artifact["path"]).parts)
            try:
                resolved = target.resolve(strict=True)
                if resolved.is_symlink() or run_dir not in resolved.parents or resolved.stat().st_size > 4 * 1024 * 1024:
                    continue
                lines = resolved.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, Mapping) or message.get("method") != "thread/tokenUsage/updated":
                    continue
                params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
                usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), Mapping) else {}
                total = usage.get("total") if isinstance(usage.get("total"), Mapping) else {}
                last = usage.get("last") if isinstance(usage.get("last"), Mapping) else {}
                normalized_total = _codex_usage_values(total)
                normalized_last = _codex_usage_values(last)
                if normalized_total is not None and normalized_last is not None:
                    updates.append({"total": normalized_total, "last": normalized_last})
    if not updates:
        return []
    final = updates[-1]["total"]
    model_event = next((event for event in metrics.get("events", []) if event.get("event_type") == "backend_process_started"), {})
    model_payload = model_event.get("payload") if isinstance(model_event.get("payload"), Mapping) else {}
    model = str(model_payload.get("model") or "unavailable")
    effort = str(model_payload.get("reasoning") or "unavailable")
    estimates = [_estimated_api_cost(model, update["last"]) for update in updates]
    priced = all(estimate is not None for estimate in estimates)
    duration_ms = sum(stage["duration_ms"] for stage in stages if stage.get("kind") == "coordinator session" and type(stage.get("duration_ms")) is int)
    return [{
        "agent": "codex-app-server coordinator",
        "phase": next((str(stage.get("phase")) for stage in stages if stage.get("kind") == "coordinator session"), "other"),
        "agent_type": "run_coordinator",
        "model": model,
        "effort": effort,
        "backend": str(model_event.get("backend_id") or "codex-app-server"),
        "calls": len(updates),
        "input_tokens": final["input_tokens"],
        "cached_input_tokens": final["cached_input_tokens"],
        "output_tokens": final["output_tokens"],
        "peak_input_tokens": max(update["last"]["input_tokens"] for update in updates),
        "duration_ms": duration_ms,
        "cost_usd": sum(estimate["usd"] for estimate in estimates if estimate is not None) if priced else None,
        "cost_kind": "estimated" if priced else "unavailable",
        "cost_source": estimates[0]["source"] if priced else None,
        "long_context_priced": any(estimate["long_context"] for estimate in estimates if estimate is not None),
    }]


def _codex_usage_values(value: Mapping[str, Any]) -> dict[str, int] | None:
    fields = {
        "input_tokens": value.get("inputTokens"),
        "cached_input_tokens": value.get("cachedInputTokens"),
        "output_tokens": value.get("outputTokens"),
    }
    return fields if all(type(item) is int and item >= 0 for item in fields.values()) else None


def _execution_stages(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project audited coordinator, task, and verification stages."""
    events = metrics.get("events", [])
    starts: dict[str, Mapping[str, Any]] = {}
    stages: list[dict[str, Any]] = []
    runtime_model = "unavailable"
    runtime_effort = "unavailable"
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if event.get("event_type") == "backend_process_started":
            runtime_model = str(payload.get("model") or "unavailable")
            runtime_effort = str(payload.get("reasoning") or "unavailable")
        nested = payload.get("controller_event") if isinstance(payload.get("controller_event"), Mapping) else {}
        nested_payload = nested.get("payload") if isinstance(nested.get("payload"), Mapping) else {}
        event_type = nested.get("event_type")
        session_id = nested_payload.get("session_id")
        if event_type == "coordinator.session_started" and isinstance(session_id, str):
            starts[session_id] = event
        elif event_type == "coordinator.session_ended" and isinstance(session_id, str):
            stages.append({
                "label": str(nested_payload.get("segment_id") or "coordinator session"),
                "kind": "coordinator session",
                "phase": str(nested_payload.get("ending_phase") or nested_payload.get("starting_phase") or "unavailable"),
                "attempt": str(nested_payload.get("attempt") or "unavailable"),
                "status": str(nested_payload.get("result_status") or nested_payload.get("outcome") or event.get("status") or "recorded"),
                "backend": str(nested_payload.get("backend_id") or "unavailable"),
                "model": runtime_model,
                "effort": runtime_effort,
                "duration_ms": _event_elapsed_ms(starts.get(session_id), event),
            })
        if event.get("event_type") == "deterministic_verification_completed":
            stages.append({
                "label": str(payload.get("stage") or "verification"),
                "kind": "deterministic verification",
                "phase": "verify",
                "attempt": str(payload.get("attempt") or "unavailable"),
                "status": "succeeded" if payload.get("exit_code") == 0 and not payload.get("timed_out") else "failed",
                "backend": "local command",
                "model": "not applicable",
                "effort": "not applicable",
                "duration_ms": _nonnegative_int(payload.get("duration_ms")),
            })
    state = metrics.get("checkpoint", {}).get("state", {})
    controller = state.get("controller") if isinstance(state, Mapping) and isinstance(state.get("controller"), Mapping) else {}
    tasks = controller.get("tasks") if isinstance(controller.get("tasks"), Mapping) else {}
    for task_id, task in tasks.items():
        if not isinstance(task, Mapping):
            continue
        attempt = str(task.get("attempt_id") or "unavailable")
        evidence = task.get("evidence") if isinstance(task.get("evidence"), list) else []
        task_backend = next((value.removeprefix("model-backend:") for value in evidence if isinstance(value, str) and value.startswith("model-backend:")), "unavailable")
        stages.append({
            "label": str(task_id),
            "kind": str(task.get("role") or "task"),
            "phase": _attempt_phase(attempt),
            "attempt": attempt,
            "status": str(task.get("status") or "recorded"),
            "backend": task_backend,
            "model": runtime_model,
            "effort": runtime_effort,
            "duration_ms": None,
        })
    return stages


def _event_elapsed_ms(start: Mapping[str, Any] | None, end: Mapping[str, Any]) -> int | None:
    if not start:
        return None
    start_ns, end_ns = start.get("monotonic_ns"), end.get("monotonic_ns")
    if type(start_ns) is not int or type(end_ns) is not int or end_ns < start_ns:
        return None
    return (end_ns - start_ns) // 1_000_000


def _attempt_phase(attempt: str) -> str:
    if "/review-fix/" in attempt and attempt.endswith("/review"):
        return "review"
    if attempt.endswith("/verify"):
        return "verify"
    if attempt.endswith("/fix") or "/verification-repair/" in attempt:
        return "repair"
    if attempt.startswith("implement-") or "/implement" in attempt:
        return "implement"
    return "other"


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return float(parsed) if parsed.is_finite() and parsed >= 0 else None


def _estimated_api_cost(model: str, usage: Mapping[str, Any]) -> dict[str, Any] | None:
    price = _ESTIMATED_MODEL_PRICES.get(model)
    if price is None:
        return None
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    cached_tokens = min(input_tokens, _nonnegative_int(usage.get("cached_input_tokens")))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    uncached_tokens = input_tokens - cached_tokens
    long_context = input_tokens > _LONG_CONTEXT_THRESHOLD
    input_multiplier = Decimal("2") if long_context else Decimal("1")
    output_multiplier = Decimal("1.5") if long_context else Decimal("1")
    million = Decimal("1000000")
    cost = (
        Decimal(uncached_tokens) * price["input"] * input_multiplier
        + Decimal(cached_tokens) * price["cached_input"] * input_multiplier
        + Decimal(output_tokens) * price["output"] * output_multiplier
    ) / million
    return {"usd": float(cost), "source": price["source"], "long_context": long_context}


def _aggregate_metric_rows(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    missing_cost = sum(1 for row in rows if row["cost_usd"] is None)
    estimated_cost = sum(1 for row in rows if row["cost_kind"] == "estimated")
    sources = sorted({str(row["cost_source"]) for row in rows if row["cost_source"]})
    long_context_records = sum(1 for row in rows if row["long_context_priced"])
    if rows and missing_cost == 0:
        cost_state = "estimated" if estimated_cost else "available"
        cost_reason = (
            f"API-equivalent estimate from published model rates; long-context pricing applied to {long_context_records} record(s); excludes tool fees and cache-write premiums"
            if estimated_cost else None
        )
    else:
        cost_state = "unavailable"
        cost_reason = f"{missing_cost} usage record(s) lack recognized pricing" if rows else "no usage records were recorded"
    result = {
        "calls": sum(row["calls"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "duration_ms": sum(row["duration_ms"] for row in rows),
        "peak_input_tokens": max(
            (
                peak
                for peak in (
                    row.get("peak_input_tokens", row["input_tokens"]) for row in rows
                )
                if isinstance(peak, int)
            ),
            default=None,
        ),
        "cost": {
            "state": cost_state,
            "usd": round(sum(row["cost_usd"] or 0 for row in rows), 6) if rows and missing_cost == 0 else None,
            "reason": cost_reason,
            "sources": sources,
            "estimated_records": estimated_cost,
            "long_context_records": long_context_records,
        },
    }
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _breakdown(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record[key], []).append(record)
    rows = []
    for label, grouped in groups.items():
        row = {"label": label, **_aggregate_metric_rows(grouped)}
        if key == "agent":
            for field in ("phase", "agent_type", "model", "effort", "backend"):
                values = sorted({record[field] for record in grouped})
                row[field] = ", ".join(values)
        rows.append(row)
    return sorted(rows, key=lambda row: (-row["total_tokens"], row["label"]))


def _nodes(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = metrics["checkpoint"].get("state", {})
    nodes = state.get("nodes", {}) if isinstance(state, Mapping) else {}
    if not isinstance(nodes, Mapping): return []
    result = []
    ordered = state.get("ordered_node_ids", ()) if isinstance(state, Mapping) else ()
    node_ids = [value for value in ordered if isinstance(value, str) and value in nodes]
    node_ids.extend(sorted(value for value in nodes if value not in node_ids))
    for node_id in node_ids:
        data = nodes[node_id]
        if not isinstance(node_id, str) or not isinstance(data, Mapping): continue
        recorded_status = data.get("status", "queued")
        status = recorded_status
        if status not in {"queued", "running", "succeeded", "failed", "blocked"}: status = "queued"
        dependencies = data.get("depends_on", ())
        if not isinstance(dependencies, (list, tuple)):
            dependencies = ()
        liveness = (
            {"state": "not_applicable", "reason": None}
            if recorded_status in {"queued", "succeeded", "failed", "blocked"}
            else {"state": "liveness_unavailable", "reason": "child liveness is unavailable until a correlated FeatureRun is discovered"}
        )
        evidence = data.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        reason = evidence.get("reason")
        result.append({"node_id": node_id, "status": status, "feature_run_id": data.get("feature_run_id") if isinstance(data.get("feature_run_id"), str) else None, "depends_on": [value for value in dependencies if isinstance(value, str)], "liveness": liveness, "evidence": availability("partial", reason) if isinstance(reason, str) and reason else availability("available"), "_candidate_commit": data.get("candidate_commit") if isinstance(data.get("candidate_commit"), str) else None})
    return result


def _graph_execution(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Project checkpoint-recorded parallel state without reconstructing it."""
    state = metrics["checkpoint"].get("state", {})
    state = state if isinstance(state, Mapping) else {}
    nodes = state.get("nodes", {})
    nodes = nodes if isinstance(nodes, Mapping) else {}
    active = state.get("active_node_ids", ())
    active_nodes = [node_id for node_id in active if isinstance(node_id, str) and node_id in nodes] if isinstance(active, (list, tuple)) else []
    attempts = state.get("successor_attempts", ())
    attempts = attempts if isinstance(attempts, list) else ()
    projected_attempts = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        node_id = attempt.get("node_id")
        logical_attempt = attempt.get("logical_attempt")
        if not isinstance(node_id, str) or type(logical_attempt) is not int:
            continue
        node = nodes.get(node_id)
        node = node if isinstance(node, Mapping) else {}
        projected_attempts.append({
            "node_id": node_id,
            "logical_attempt": logical_attempt,
            "allocation_id": attempt.get("allocation_id") if isinstance(attempt.get("allocation_id"), str) else None,
            "checkpoint_revision": attempt.get("checkpoint_revision") if type(attempt.get("checkpoint_revision")) is int else None,
            "parent_candidate_commit": attempt.get("parent_candidate_commit") if isinstance(attempt.get("parent_candidate_commit"), str) else None,
            "expected_staging_head": attempt.get("expected_staging_head") if isinstance(attempt.get("expected_staging_head"), str) else None,
            "status": node.get("status") if isinstance(node.get("status"), str) else "unavailable",
            "candidate_commit": node.get("candidate_commit") if isinstance(node.get("candidate_commit"), str) else None,
        })
    projected_attempts.sort(key=lambda item: (item["logical_attempt"], item["node_id"], item["allocation_id"] or ""))
    barriers = _integration_barriers(state.get("integration_barriers"))
    lease_record = _integration_lease(state.get("integration_lease"))
    lineage = _attempt_lineage(state.get("attempt_lineage"))
    retry_state = _retry_state(state.get("retry_state"))
    head = state.get("current_candidate_commit")
    return {
        "logical_graph": {
            "base_commit": state.get("base_commit") if isinstance(state.get("base_commit"), str) else None,
            "plan_digest": state.get("plan_digest") if isinstance(state.get("plan_digest"), str) else None,
            "plan_graph_digest": state.get("plan_graph_digest") if isinstance(state.get("plan_graph_digest"), str) else None,
        },
        "attempts": projected_attempts,
        "concurrency": {
            "active_nodes": active_nodes,
            "active_count": len(active_nodes),
            "max_parallelism": availability("unavailable", "parallelism limit was not recorded in this checkpoint"),
        },
        "integration": {
            "staging_head": head if isinstance(head, str) else None,
            "lease": availability("available") if lease_record else availability("unavailable", "integration lease was not recorded in this checkpoint"),
            "lease_record": lease_record,
            "barriers": barriers,
        },
        "recovery": {
            "active_allocations": [item for item in projected_attempts if item["node_id"] in active_nodes],
            "authority": availability("unavailable", "recovery disposition is not recorded in this checkpoint"),
            "dispositions": _recovery_dispositions(metrics.get("events", ())),
            "attempt_lineage": lineage,
            "retry_state": retry_state,
        },
    }


def _integration_lease(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    fields = ("node_id", "lease_id", "expected_staging_head")
    if not all(isinstance(value.get(field), str) and value[field] for field in fields):
        return None
    return {field: value[field] for field in fields}


def _integration_barriers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    fields = ("barrier_id", "node_id", "attempt_id", "allocation_id", "lease_id", "action", "input_commit", "expected_staging_head", "integrated_commit")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        record = {field: item.get(field) if isinstance(item.get(field), str) else None for field in fields}
        record["logical_attempt"] = item.get("logical_attempt") if type(item.get("logical_attempt")) is int else None
        record["checkpoint_revision"] = item.get("checkpoint_revision") if type(item.get("checkpoint_revision")) is int else None
        refs = _evidence_refs(item)
        receipt = item.get("receipt")
        if isinstance(receipt, Mapping):
            refs.update(_evidence_refs(receipt))
        record["evidence_refs"] = sorted(refs)
        result.append(record)
    return result


def _attempt_lineage(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        required = ("attempt_id", "node_id", "allocation_id", "input_commit")
        if not all(isinstance(item.get(field), str) and item[field] for field in required) or type(item.get("logical_attempt")) is not int:
            continue
        result.append({
            "attempt_id": item["attempt_id"], "node_id": item["node_id"], "logical_attempt": item["logical_attempt"],
            "allocation_id": item["allocation_id"], "input_commit": item["input_commit"],
            "predecessor_attempt_id": item.get("predecessor_attempt_id") if isinstance(item.get("predecessor_attempt_id"), str) else None,
        })
    return result


def _retry_state(value: Any) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {"invalidations": [], "reuse": []}
    if not isinstance(value, Mapping):
        return result
    for item in value.get("invalidations", ()) if isinstance(value.get("invalidations"), list) else ():
        if isinstance(item, Mapping) and all(isinstance(item.get(field), str) and item[field] for field in ("attempt_id", "node_id", "allocation_id", "reason", "invalidated_at")):
            result["invalidations"].append({field: item[field] for field in ("attempt_id", "node_id", "allocation_id", "reason", "invalidated_at")})
    for item in value.get("reuse", ()) if isinstance(value.get("reuse"), list) else ():
        if isinstance(item, Mapping) and all(isinstance(item.get(field), str) and item[field] for field in ("node_id", "reused_from_attempt_id", "replacement_attempt_id")):
            result["reuse"].append({field: item[field] for field in ("node_id", "reused_from_attempt_id", "replacement_attempt_id")})
    return result


def _evidence_refs(value: Mapping[str, Any]) -> set[str]:
    return {item for key, item in value.items() if key.endswith("_ref") and isinstance(item, str) and item}


def _recovery_dispositions(events: Any) -> list[dict[str, Any]]:
    """Expose only recovery outcomes and refs committed to the graph journal."""
    if not isinstance(events, list):
        return []
    dispositions: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("event_type")
        payload = event.get("payload")
        if event_type not in {"plan_graph_child_recovery_blocked", "plan_graph_child_seal_adopted"} or not isinstance(payload, Mapping):
            continue
        node_id = payload.get("plan_node_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        if event_type == "plan_graph_child_recovery_blocked":
            reason = payload.get("reason")
            refs = [payload["evidence_ref"]] if isinstance(payload.get("evidence_ref"), str) else []
            dispositions.append({"node_id": node_id, "disposition": "blocked", "reason": reason if isinstance(reason, str) else "recovery outcome reason was not recorded", "forced": reason == "force_reconcile", "evidence_refs": refs})
            continue
        receipt = payload.get("seal_receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        refs = [value for key, value in receipt.items() if key.endswith("_ref") and isinstance(value, str)]
        if isinstance(payload.get("force_evidence_ref"), str):
            refs.append(payload["force_evidence_ref"])
        dispositions.append({"node_id": node_id, "disposition": "sealed", "reason": None, "forced": payload.get("forced") is True, "evidence_refs": sorted(set(refs))})
    return dispositions


def _graph_status(metrics: Mapping[str, Any], fallback: str) -> str:
    state = metrics["checkpoint"].get("state", {})
    value = state.get("terminal_graph_status") if isinstance(state, Mapping) else None
    return value if value in TERMINAL_STATUSES or value in {"queued", "running"} else fallback

_ID_MATCH_REASON = "correlated by exact run id; descriptor attestation absent"


def _id_match_child(graph: Mapping[str, Any], node: Mapping[str, Any], features: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Bind a descriptor-less legacy run to a node by exact run-id equality.

    This correlation is deliberately weaker than descriptor attestation: the
    injected correlation is labeled state "id_matched" and callers must keep
    the node evidence partial so it can never read as descriptor-attested.
    Runs that carry a descriptor (even one with a null parent_correlation)
    are excluded because their attestation already speaks for itself.
    """
    run_id = node.get("feature_run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    child = next(
        (
            record for record in features
            if record.get("run_id") == run_id
            and record.get("kind") == "legacy_feature_run"
            and record.get("status") != "corrupt"
            and not record.get("correlation")
        ),
        None,
    )
    if child is None:
        return None
    child["correlation"] = {
        "plan_graph_id": graph["run_id"],
        "plan_node_id": node["node_id"],
        "parent_run_id": graph["run_id"],
        "state": "id_matched",
        "reason": _ID_MATCH_REASON,
    }
    return child


def _correlation_is_id_matched(child: Mapping[str, Any] | None) -> bool:
    correlation = child.get("correlation") if child else None
    return isinstance(correlation, Mapping) and correlation.get("state") == "id_matched"


def _correlation(descriptor: Mapping[str, Any] | None) -> dict[str, str] | None:
    value = descriptor.get("parent_correlation") if descriptor else None
    return dict(value) if isinstance(value, Mapping) and all(isinstance(value.get(key), str) and value[key] for key in ("plan_graph_id", "plan_node_id", "parent_run_id")) else None


def _node_matches_child(graph: Mapping[str, Any], node: Mapping[str, Any], child: Mapping[str, Any]) -> bool:
    correlation = child.get("correlation")
    return bool(
        node.get("feature_run_id") == child.get("run_id")
        and isinstance(correlation, Mapping)
        and correlation.get("plan_graph_id") == graph.get("run_id")
        and correlation.get("plan_node_id") == node.get("node_id")
        and correlation.get("parent_run_id") == graph.get("run_id")
    )

def _integration_merge_commits(events: list[Mapping[str, Any]]) -> tuple[str, ...]:
    commits = {
        event["payload"]["merge_commit"]
        for event in events
        if event.get("event_type") == "git_integrate_completed"
        and event.get("status") == "succeeded"
        and isinstance(event.get("payload"), Mapping)
        and isinstance(event["payload"].get("merge_commit"), str)
    }
    return tuple(sorted(commits))

def _corrupt_record(run_id: str, reason: str) -> dict[str, Any]:
    return {"run_id": run_id, "kind": "legacy_feature_run", "status": "corrupt", "liveness": {"state": "liveness_unavailable", "reason": "run is corrupt"}, "evidence": availability("unavailable", reason), "correlation": None}

def _liveness(directory: Path, run_id: str, kind: str | None, status: str, now: datetime, probe: ProcessProbe, freshness: float) -> dict[str, str | None]:
    if status in TERMINAL_STATUSES: return {"state": "terminal", "reason": None}
    try: lease = _optional_object(directory / "liveness.json")
    except AuditError: return {"state": "liveness_unavailable", "reason": "invalid liveness lease"}
    if not lease: return {"state": "liveness_unavailable", "reason": "no liveness lease"}
    if set(lease) != _LEASE_FIELDS or lease.get("protocol") != "harness-controller-liveness/1" or lease.get("run_id") != run_id or lease.get("controller_kind") != kind: return {"state": "liveness_unavailable", "reason": "invalid liveness lease"}
    if not all(isinstance(lease.get(key), str) and lease[key] for key in ("controller_instance_id", "hostname", "process_start_token")) or type(lease.get("pid")) is not int or lease["pid"] < 1 or type(lease.get("heartbeat_sequence")) is not int or lease["heartbeat_sequence"] < 0: return {"state": "liveness_unavailable", "reason": "invalid liveness lease"}
    if lease["hostname"] != socket.gethostname(): return {"state": "remote_unverified", "reason": "lease host is not local"}
    try: heartbeat = datetime.fromisoformat(str(lease["heartbeat_at"]).replace("Z", "+00:00"))
    except ValueError: return {"state": "liveness_unavailable", "reason": "invalid heartbeat timestamp"}
    if heartbeat.tzinfo is None or heartbeat.utcoffset() is None or heartbeat > now: return {"state": "liveness_unavailable", "reason": "invalid heartbeat timestamp"}
    if (now - heartbeat).total_seconds() > freshness: return {"state": "stale", "reason": "heartbeat is stale"}
    if probe(lease["pid"]) != lease["process_start_token"]: return {"state": "stale", "reason": "process identity does not match"}
    return {"state": "live", "reason": None}

def _validate_descriptor(value: Mapping[str, Any], run_id: str) -> None:
    fields = set(value)
    kind = value.get("run_kind")
    allowed_fields = _DESCRIPTOR_FIELDS | _PLAN_GRAPH_LINEAGE_FIELDS
    if (
        fields not in {_DESCRIPTOR_FIELDS, allowed_fields}
        or kind not in {"feature_run", "plan_graph"}
        or (fields == allowed_fields and kind != "plan_graph")
        or value.get("protocol") != "harness-run-descriptor/1"
        or value.get("run_id") != run_id
    ):
        raise AuditError("descriptor does not bind this run")
    repository = value.get("repository")
    if not isinstance(repository, Mapping) or set(repository) != {"path", "base_branch", "base_commit"} or not all(isinstance(repository.get(key), str) and repository[key] for key in repository) or len(repository["base_commit"]) != 40 or any(character not in "0123456789abcdef" for character in repository["base_commit"]): raise AuditError("descriptor repository is invalid")
    if not all(isinstance(value.get(key), str) and value[key] for key in ("created_at", "objective", "evidence_classification")) or value["evidence_classification"] not in {"production_lifecycle", "component", "synthetic", "fabricated_fixture"}: raise AuditError("descriptor fields are invalid")
    try:
        created_at = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("descriptor creation timestamp is invalid") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise AuditError("descriptor creation timestamp is invalid")
    plan = value.get("approved_plan")
    if plan is not None and (not isinstance(plan, Mapping) or set(plan) != {"path", "sha256"} or not isinstance(plan.get("path"), str) or not plan["path"] or not isinstance(plan.get("sha256"), str) or len(plan["sha256"]) != 64 or any(character not in "0123456789abcdef" for character in plan["sha256"])):
        raise AuditError("descriptor approved plan is invalid")
    correlation = value.get("parent_correlation")
    if correlation is not None and (not isinstance(correlation, Mapping) or set(correlation) != {"plan_graph_id", "plan_node_id", "parent_run_id"} or not all(isinstance(correlation.get(key), str) and correlation[key] for key in correlation)):
        raise AuditError("descriptor correlation is invalid")
    if kind == "plan_graph" and (plan is None or correlation is not None):
        raise AuditError("plan graph descriptor is invalid")
    if fields == allowed_fields and (
        not all(isinstance(value.get(key), str) and value[key] and "/" not in value[key] for key in ("logical_graph_id", "graph_attempt_id"))
        or value["predecessor_attempt_id"] is not None and (not isinstance(value["predecessor_attempt_id"], str) or not value["predecessor_attempt_id"] or "/" in value["predecessor_attempt_id"])
    ):
        raise AuditError("plan graph descriptor lineage is invalid")

def _descriptor(path: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    if path.is_symlink(): raise AuditError("descriptor.json must not be a symlink")
    if not path.is_file(): return None, None
    raw = path.read_bytes(); value = json.loads(raw)
    if not isinstance(value, dict): raise AuditError("descriptor.json must be an object")
    return value, raw

def _optional_object(path: Path) -> dict[str, Any] | None:
    if path.is_symlink(): raise AuditError(f"{path.name} must not be a symlink")
    if not path.is_file(): return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise AuditError(f"{path.name} must be an object")
    return value

def _descriptor_is_bound(raw: bytes | None, events: list[Mapping[str, Any]]) -> bool:
    digest = hashlib.sha256(raw).hexdigest() if raw else None
    return bool(digest and any(event.get("event_type") in {"run_descriptor_bound", "plan_graph_initialized"} and isinstance(event.get("payload"), Mapping) and event["payload"].get("descriptor_sha256") == digest for event in events))

def _local_process_start_token(pid: int) -> str | None:
    try: return str(os.stat(f"/proc/{pid}").st_ctime_ns)
    except OSError: pass
    try: result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], check=False, capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError): return None
    return result.stdout.strip() or None

def _diagnostic(code: str, message: str, run_id: str | None) -> dict[str, str | None]: return {"code": code, "message": message, "run_id": run_id}
def _timestamp(value: datetime) -> str: return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
