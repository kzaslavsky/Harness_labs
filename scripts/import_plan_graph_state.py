#!/usr/bin/env python3
"""Import one operator-supplied legacy PlanGraph state into a canonical journal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plan_graph import PlanGraph, PlanGraphError, plan_from_mapping
from harness_labs.plan_graph_audit import PlanGraphAudit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import one explicitly paired decomposition and legacy PlanGraph state."
    )
    parser.add_argument("decomposition", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--graph-run-id", required=True)
    arguments = parser.parse_args()
    plan = plan_from_mapping(json.loads(arguments.decomposition.read_text(encoding="utf-8")))
    # Reuse PlanGraph's validation and strict lineage checks; no launcher is ever run.
    legacy = PlanGraph(plan, lambda request: (_ for _ in ()).throw(AssertionError("import cannot launch")), state_path=arguments.state)
    legacy.validate()
    completed = legacy._load_completed()
    legacy._validate_completed_dependencies(legacy._ordered_runs(), completed)
    nodes = {
        run.id: {
            "status": "queued",
            "objective": run.objective,
            "plan_sections": list(run.plan_sections),
            "depends_on": list(run.depends_on),
            "criteria": list(run.criteria),
            "verification_argv": list(run.verification_argv),
            "feature_run_id": f"{arguments.graph_run_id}-{run.id}",
            "run_dir": str((arguments.run_root / f"{arguments.graph_run_id}-{run.id}").resolve()),
            "started_at": None,
            "finished_at": None,
            "candidate_commit": None,
        }
        for run in legacy._ordered_runs()
    }
    audit = PlanGraphAudit(
        run_root=arguments.run_root,
        graph_run_id=arguments.graph_run_id,
        plan=plan.plan,
        base_commit=plan.base_commit,
        objective="; ".join(run.objective for run in plan.runs),
        nodes=nodes,
        functionality_tests=plan.functionality_tests,
    )
    if audit.terminal or any(
        node.get("status") != "queued"
        for node in audit.state.get("nodes", {}).values()
        if isinstance(node, dict)
    ):
        raise PlanGraphError("canonical graph run already contains imported state")
    for run in legacy._ordered_runs():
        candidate = completed.get(run.id)
        if candidate is None:
            break
        audit.node_started(run.id)
        audit.node_completed(run.id, candidate)
    print(json.dumps({"graph_run_id": arguments.graph_run_id, "run_dir": str(audit.run_dir), "completed": completed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
