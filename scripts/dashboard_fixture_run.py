#!/usr/bin/env python3
"""Create a bounded, fabricated audit root for dashboard certification tests.

This helper is deliberately test-only.  FeatureRun journals use
``fabricated_fixture`` evidence; the PlanGraph adapter currently has a fixed
production-lifecycle descriptor and is exercised here only as fabricated test
input, never as an operational run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.plan_graph_audit import PlanGraphAudit

_ACTOR = AuditActor("dashboard-fixture", "test")
_BASE = "a" * 40


def _descriptor(run_id: str, *, kind: str = "feature_run", parent: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "protocol": "harness-run-descriptor/1",
        "run_kind": kind,
        "run_id": run_id,
        "created_at": "2026-08-09T00:00:00Z",
        "objective": f"Dashboard fixture {run_id}",
        "evidence_classification": "fabricated_fixture",
        "repository": {"path": "/fixture", "base_branch": "main", "base_commit": _BASE},
        "approved_plan": None,
        "parent_correlation": parent,
    }


def _feature(root: Path, run_id: str, *, parent: dict[str, str] | None = None, terminal: bool = False) -> Path:
    journal = AuditJournal(root / run_id, run_id, actor=_ACTOR, evidence_classification="fabricated_fixture")
    raw = (json.dumps(_descriptor(run_id, parent=parent), sort_keys=True, separators=(",", ":")) + "\n").encode()
    (journal.run_dir / "descriptor.json").write_bytes(raw)
    journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
    journal.checkpoint("running", {"controller": {"criteria": ["AC-fixture"], "tasks": ["inspect"], "findings": [], "decisions": []}})
    if terminal:
        journal.finalize("succeeded", result={"status": "succeeded"})
    return journal.run_dir


def _lease(run_dir: Path, run_id: str, *, stale: bool) -> None:
    heartbeat = datetime.now(timezone.utc) - (timedelta(seconds=120) if stale else timedelta())
    run_dir.joinpath("liveness.json").write_text(json.dumps({
        "protocol": "harness-controller-liveness/1", "run_id": run_id,
        "controller_instance_id": "fixture-controller", "hostname": socket.gethostname(),
        "pid": 7, "process_start_token": "fixture-process-token",
        "heartbeat_sequence": 1, "heartbeat_at": heartbeat.isoformat(),
        "controller_kind": "feature_run",
    }), encoding="utf-8")


def _graph(root: Path, graph_id: str, plan: Path, nodes: dict[str, dict[str, object]], *, terminal: bool) -> None:
    plan_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
    audit = PlanGraphAudit(
        repository=root,
        run_root=root,
        graph_run_id=graph_id,
        plan=str(plan),
        plan_sha256=plan_sha256,
        base_commit=_BASE,
        registration_binding={
            "logical_graph_id": graph_id,
            "registration_protocol": "plan-graph-registration/1",
            "registration_digest": hashlib.sha256(graph_id.encode()).hexdigest(),
            "graph_attempt_id": graph_id,
        },
        objective="Dashboard fixture graph",
        nodes=nodes,
        functionality_tests=(),
    )
    if terminal:
        audit.node_started("done")
        audit.node_completed("done", _BASE)
        audit.finalize("succeeded", {"status": "succeeded"})


def create_fixture(root: Path) -> None:
    """Create the exact independent records used by the e2e walk."""
    root.mkdir(parents=True, exist_ok=True)
    plan = root / "approved-plan.md"
    plan.write_text("dashboard fixture plan\n", encoding="utf-8")
    _feature(root, "completed-child", parent={"plan_graph_id": "completed-graph", "plan_node_id": "done", "parent_run_id": "completed-graph"}, terminal=True)
    _graph(root, "completed-graph", plan, {"done": {"status": "queued", "feature_run_id": "completed-child"}}, terminal=True)
    live_parent = {"plan_graph_id": "active-graph", "plan_node_id": "live", "parent_run_id": "active-graph"}
    live = _feature(root, "live-child", parent=live_parent)
    _lease(live, "live-child", stale=False)
    stale_parent = {"plan_graph_id": "active-graph", "plan_node_id": "stale", "parent_run_id": "active-graph"}
    stale = _feature(root, "stale-child", parent=stale_parent)
    _lease(stale, "stale-child", stale=True)
    _graph(root, "active-graph", plan, {
        "live": {"status": "running", "feature_run_id": "live-child"},
        "stale": {"status": "running", "feature_run_id": "stale-child"},
        "planned": {"status": "queued", "feature_run_id": "planned-child", "depends_on": ["live"]},
    }, terminal=False)
    _feature(root, "legacy-child")
    (root / "legacy-child" / "descriptor.json").unlink()
    (root / "malformed-run").mkdir()


def advance_live_fixture(root: Path) -> None:
    """Terminalize the selected live fixture; no API request writes this state."""
    journal = AuditJournal.open_existing(root / "live-child", actor=_ACTOR)
    journal.finalize("succeeded", result={"status": "succeeded"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_root", type=Path)
    parser.add_argument("--advance-live", action="store_true")
    args = parser.parse_args()
    if args.advance_live:
        advance_live_fixture(args.audit_root)
    else:
        create_fixture(args.audit_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
