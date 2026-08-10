#!/usr/bin/env python3
"""Run an explicit PlanGraph decomposition with an injected launcher callable."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    SubprocessFeatureRunLauncher,
    plan_from_mapping,
)


def _load_callable(reference: str) -> Callable[..., object]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("launcher must use module:callable syntax")
    launcher = getattr(importlib.import_module(module_name), attribute)
    if not callable(launcher):
        raise ValueError("launcher is not callable")
    return launcher


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("decomposition", type=Path)
    launcher_group = parser.add_mutually_exclusive_group(required=True)
    launcher_group.add_argument("--launcher")
    launcher_group.add_argument("--launcher-command", nargs="+")
    parser.add_argument("--launcher-cwd", type=Path)
    parser.add_argument("--launcher-timeout", type=float)
    parser.add_argument("--run-root", type=Path, default=Path("logs/runs"))
    parser.add_argument("--graph-run-id")
    parser.add_argument("--functionality-test", action="append", default=[])
    arguments = parser.parse_args()
    payload = json.loads(arguments.decomposition.read_text(encoding="utf-8"))
    if arguments.launcher:
        launcher = _load_callable(arguments.launcher)
    else:
        launcher = SubprocessFeatureRunLauncher(
            arguments.launcher_command,
            cwd=arguments.launcher_cwd,
            timeout_seconds=arguments.launcher_timeout,
        )

    def launch(request):
        result = launcher(request)
        if isinstance(result, FeatureRunOutcome):
            return result
        if isinstance(result, dict):
            return FeatureRunOutcome(**result)
        raise TypeError("launcher must return FeatureRunOutcome or a mapping")

    graph = PlanGraph(
        plan_from_mapping(payload),
        launch,
        run_root=arguments.run_root,
        graph_run_id=arguments.graph_run_id,
        functionality_tests=arguments.functionality_test,
    )
    result = graph.run()
    print(
        json.dumps(
            {
                "status": result.status,
                "candidate_commit": result.candidate_commit,
                "failed_run_id": result.failed_run_id,
                "graph_run_id": graph.graph_run_id,
            }
        )
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
