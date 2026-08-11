#!/usr/bin/env python3
"""Register an immutable PlanGraph or run one audited attempt."""

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
    load_registration,
    persist_registration,
    register_plan_graph,
)


def _load_callable(reference: str) -> Callable[..., object]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("launcher must use module:callable syntax")
    launcher = getattr(importlib.import_module(module_name), attribute)
    if not callable(launcher):
        raise ValueError("launcher is not callable")
    return launcher


def _repository_path(repository: Path, value: Path | None, default: str) -> Path:
    selected = value if value is not None else Path(default)
    return selected.resolve() if selected.is_absolute() else (repository / selected).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="mode", required=True)

    register = modes.add_parser("register")
    register.add_argument("decomposition", type=Path)
    register.add_argument("--repository", type=Path, required=True)
    register.add_argument("--logical-graph-id", required=True)
    register.add_argument("--registration-root", type=Path)

    run = modes.add_parser("run")
    run.add_argument("--repository", type=Path, required=True)
    run.add_argument("--registration", type=Path, required=True)
    run.add_argument("--graph-attempt-id", required=True)
    launcher_group = run.add_mutually_exclusive_group(required=True)
    launcher_group.add_argument("--launcher")
    launcher_group.add_argument("--launcher-command", nargs="+")
    run.add_argument("--launcher-cwd", type=Path)
    run.add_argument("--launcher-timeout", type=float)
    run.add_argument("--run-root", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    repository = arguments.repository.resolve()
    if arguments.mode == "register":
        payload = json.loads(arguments.decomposition.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("decomposition must be a JSON object")
        registration = register_plan_graph(
            repository=repository,
            logical_graph_id=arguments.logical_graph_id,
            decomposition=payload,
        )
        registration_root = _repository_path(
            repository,
            arguments.registration_root,
            "logs/plan-graph-registrations",
        )
        path = persist_registration(
            repository=repository,
            registration_root=registration_root,
            registration=registration,
        )
        print(
            json.dumps(
                {
                    "logical_graph_id": registration.logical_graph_id,
                    "graph_digest": registration.graph_digest,
                    "registration": str(path),
                },
                sort_keys=True,
            )
        )
        return 0

    registration_path = _repository_path(repository, arguments.registration, "")
    registration = load_registration(registration_path)
    run_root = _repository_path(repository, arguments.run_root, "logs/runs")
    launcher_cwd = (
        _repository_path(repository, arguments.launcher_cwd, "")
        if arguments.launcher_cwd is not None
        else repository
    )
    if arguments.launcher:
        launcher = _load_callable(arguments.launcher)
    else:
        launcher = SubprocessFeatureRunLauncher(
            arguments.launcher_command,
            cwd=launcher_cwd,
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
        repository,
        registration,
        launch,
        run_root=run_root,
        graph_run_id=arguments.graph_attempt_id,
    )
    result = graph.run()
    print(
        json.dumps(
            {
                "status": result.status,
                "candidate_commit": result.candidate_commit,
                "failed_run_id": result.failed_run_id,
                "logical_graph_id": registration.logical_graph_id,
                "graph_attempt_id": graph.graph_run_id,
                "registration_digest": registration.graph_digest,
            },
            sort_keys=True,
        )
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
