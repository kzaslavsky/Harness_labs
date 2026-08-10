"""Read-only verified catalog for FeatureRun and PlanGraph audit directories."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .audit import AuditError
from .run_metrics import TERMINAL_STATUSES, availability, project_run_metrics

Clock = Callable[[], datetime]
ProcessProbe = Callable[[int], str | None]
_DESCRIPTOR_FIELDS = frozenset({"protocol", "run_kind", "run_id", "created_at", "objective", "evidence_classification", "repository", "approved_plan", "parent_correlation"})
_LEASE_FIELDS = frozenset({"protocol", "run_id", "controller_instance_id", "hostname", "pid", "process_start_token", "heartbeat_sequence", "heartbeat_at", "controller_kind"})


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
        record["nodes"] = _nodes(metrics, liveness)
        del record["kind"]
    else:
        record["correlation"] = _correlation(descriptor)
    return record


def _snapshot(root: Path, now: datetime, diagnostics: list[dict[str, str | None]], records: list[dict[str, Any]], reason: str | None = None) -> dict[str, Any]:
    graphs = [record for record in records if "nodes" in record]
    features = [record for record in records if "nodes" not in record]
    graph_ids = {record["run_id"] for record in graphs}
    for graph in graphs:
        for node in graph["nodes"]:
            child = next((record for record in features if _node_matches_child(graph, node, record)), None)
            if node["feature_run_id"] is not None and child is None:
                node["evidence"] = availability("partial", "child correlation is not verified")
    ungrouped = [
        record for record in features
        if not any(_node_matches_child(graph, node, record) for graph in graphs for node in graph["nodes"])
    ]
    source = availability("unavailable", reason) if reason else availability("partial", "one or more runs are corrupt") if diagnostics else availability("available")
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
    return {"lifecycle": events, "criteria": controller.get("criteria", []), "tasks": controller.get("tasks", []), "findings": controller.get("findings", []), "decisions": controller.get("decisions", []), "evidence_metadata": metrics["manifest"].get("artifacts", []) if metrics["manifest"] else [], "git_custody": git, "usage": metrics["summary"], "timing": {"started_at": metrics["checkpoint"].get("started_at"), "updated_at": metrics["checkpoint"].get("updated_at")}, "availability": {"lifecycle": availability("available"), "criteria": family_present("criteria", "criteria were not recorded"), "tasks": family_present("tasks", "tasks were not recorded"), "findings": family_present("findings", "findings were not recorded"), "evidence_metadata": availability("available") if metrics["manifest"] is not None else availability("unavailable", "manifest is unavailable"), "git_custody": availability("available"), "usage": availability("available") if metrics["summary"] is not None else availability("unavailable", "summary is unavailable")}, "descriptor": descriptor}


def _nodes(metrics: Mapping[str, Any], parent_liveness: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = metrics["checkpoint"].get("state", {})
    nodes = state.get("nodes", {}) if isinstance(state, Mapping) else {}
    if not isinstance(nodes, Mapping): return []
    result = []
    for node_id, data in sorted(nodes.items()):
        if not isinstance(node_id, str) or not isinstance(data, Mapping): continue
        status = data.get("status", "queued")
        if status not in {"queued", "running", "succeeded", "failed", "blocked"}: status = "queued"
        result.append({"node_id": node_id, "status": status, "feature_run_id": data.get("feature_run_id") if isinstance(data.get("feature_run_id"), str) else None, "liveness": dict(parent_liveness) if status == "running" else {"state": "not_applicable", "reason": None}, "evidence": availability("available")})
    return result


def _graph_status(metrics: Mapping[str, Any], fallback: str) -> str:
    state = metrics["checkpoint"].get("state", {})
    value = state.get("terminal_graph_status") if isinstance(state, Mapping) else None
    return value if value in TERMINAL_STATUSES or value in {"queued", "running"} else fallback

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
    if set(value) != _DESCRIPTOR_FIELDS or value.get("protocol") != "harness-run-descriptor/1" or value.get("run_id") != run_id or value.get("run_kind") not in {"feature_run", "plan_graph"}: raise AuditError("descriptor does not bind this run")
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
    if value["run_kind"] == "plan_graph" and (plan is None or correlation is not None):
        raise AuditError("plan graph descriptor is invalid")

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
