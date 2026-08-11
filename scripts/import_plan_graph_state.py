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


def _load_legacy_completed(state_path: Path, run_ids: set[str]) -> dict[str, str]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        completed = payload["completed"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PlanGraphError(f"invalid PlanGraph state: {exc}") from exc
    if not isinstance(completed, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in completed.items()
    ):
        raise PlanGraphError(
            "invalid PlanGraph state: completed must map ids to commits"
        )
    unknown = set(completed) - run_ids
    if unknown:
        raise PlanGraphError(
            "PlanGraph state contains unknown completed runs: "
            + ", ".join(sorted(unknown))
        )
    return dict(completed)


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
    graph = PlanGraph(
        plan,
        lambda request: (_ for _ in ()).throw(AssertionError("import cannot launch")),
        run_root=arguments.run_root,
        graph_run_id=arguments.graph_run_id,
    )
    graph.validate()
    completed = _load_legacy_completed(
        arguments.state, {run.id for run in plan.runs}
    )
    graph._validate_completed_dependencies(graph._ordered_runs(), completed)
    nodes = {
        run.id: {
            "status": "queued",
            "objective": run.objective,
            "plan_sections": list(run.plan_sections),
            "depends_on": list(run.depends_on),
            "criteria": list(run.criteria),
            "verification_argv": list(run.verification_argv),
            "verification_timeout_seconds": run.verification_timeout_seconds,
            "allowed_paths": list(run.allowed_paths),
            "path_intents": [
                {"path": value.path, "action": value.action}
                for value in run.path_intents
            ],
            "feature_run_id": f"{arguments.graph_run_id}-{run.id}",
            "run_dir": str((arguments.run_root / f"{arguments.graph_run_id}-{run.id}").resolve()),
            "started_at": None,
            "finished_at": None,
            "candidate_commit": None,
        }
        for run in graph._ordered_runs()
    }
    audit = PlanGraphAudit(
        run_root=arguments.run_root,
        graph_run_id=arguments.graph_run_id,
        plan=plan.plan,
        plan_sha256=graph._plan_sha256(),
        base_commit=plan.base_commit,
        repository_id=plan.repository_id,
        repository_path=Path.cwd(),
        plan_graph_digest=graph._identity_digest(),
        approval=None,
        objective="; ".join(run.objective for run in plan.runs),
        nodes=nodes,
        functionality_tests=tuple(
            {
                "argv": list(command.argv),
                "timeout_seconds": command.timeout_seconds,
                "required_paths": [],
            }
            for command in graph.functionality_tests
        ),
    )
    if audit.terminal or any(
        node.get("status") != "queued"
        for node in audit.state.get("nodes", {}).values()
        if isinstance(node, dict)
    ):
        raise PlanGraphError("canonical graph run already contains imported state")
    for run in graph._ordered_runs():
        candidate = completed.get(run.id)
        if candidate is None:
            break
        audit.node_started(run.id)
        audit.node_completed(run.id, candidate)
    print(json.dumps({"graph_run_id": arguments.graph_run_id, "run_dir": str(audit.run_dir), "completed": completed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
