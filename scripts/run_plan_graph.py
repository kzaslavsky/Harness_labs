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

from harness_labs.plan_approval import PlanApprovalAdmission, PlanApprovalError
from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    RepairResumeDirective,
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
    source_group = run.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--registration", type=Path)
    source_group.add_argument("--approval-receipt", type=Path)
    run.add_argument("--decomposition", type=Path)
    run.add_argument("--graph-attempt-id", required=True)
    launcher_group = run.add_mutually_exclusive_group(required=True)
    launcher_group.add_argument("--launcher")
    launcher_group.add_argument("--launcher-command", nargs="+")
    run.add_argument("--launcher-cwd", type=Path)
    run.add_argument("--launcher-timeout", type=float)
    run.add_argument("--run-root", type=Path)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--logical-graph-id")
    run.add_argument("--predecessor-attempt-id")
    run.add_argument("--retry-frontier", action="append", default=[])
    run.add_argument("--blocker-evidence-ref")
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
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

    approval_validator = None
    if arguments.approval_receipt is not None:
        try:
            admission = PlanApprovalAdmission(
                repository=repository,
                receipt_path=arguments.approval_receipt,
            )
            approved = admission.validate()
            if arguments.decomposition is not None:
                decomposition_path = arguments.decomposition.resolve()
                try:
                    relative = decomposition_path.relative_to(repository).as_posix()
                except ValueError as exc:
                    raise PlanApprovalError(
                        "decomposition must be inside the repository"
                    ) from exc
                if relative != approved.decomposition_path:
                    raise PlanApprovalError(
                        "decomposition path does not match the approved subject"
                    )
            registration = register_plan_graph(
                repository=repository,
                logical_graph_id=arguments.logical_graph_id
                or arguments.graph_attempt_id,
                decomposition=approved.decomposition,
                base_commit=approved.base_commit,
                repository_id=approved.repository_id,
            )
            if registration.plan_sha256 != approved.plan_sha256:
                raise PlanApprovalError(
                    "approved plan hash does not match the registered plan"
                )
            approval_validator = admission.approval_validator()
        except (OSError, PlanApprovalError, PlanGraphError, ValueError) as exc:
            print(f"PlanGraph admission failed: {exc}", file=sys.stderr)
            return 1
    else:
        if arguments.decomposition is not None:
            parser.error("--decomposition requires --approval-receipt")
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

    try:
        graph = _build_graph(arguments, parser, repository, registration, launch, run_root, approval_validator)
        result = graph.run()
    except PlanGraphError as exc:
        print(f"PlanGraph failed: {exc}", file=sys.stderr)
        return 1
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


def _build_graph(arguments, parser, repository, registration, launch, run_root, approval_validator):
    if arguments.resume:
        if not all(
            (
                arguments.logical_graph_id,
                arguments.predecessor_attempt_id,
                arguments.blocker_evidence_ref,
            )
        ):
            parser.error(
                "--resume requires --logical-graph-id, --predecessor-attempt-id, "
                "and --blocker-evidence-ref"
            )
        graph = PlanGraph.resume(
            repository,
            registration,
            launch,
            run_root=run_root,
            directive=RepairResumeDirective(
                arguments.logical_graph_id,
                arguments.predecessor_attempt_id,
                tuple(arguments.retry_frontier),
                arguments.blocker_evidence_ref,
            ),
            approval_validator=approval_validator,
        )
        return graph
    else:
        if any(
            (
                arguments.logical_graph_id,
                arguments.predecessor_attempt_id,
                arguments.retry_frontier,
                arguments.blocker_evidence_ref,
            )
        ):
            parser.error("repair arguments require --resume")
        graph = PlanGraph(
            repository,
            registration,
            launch,
            run_root=run_root,
            graph_run_id=arguments.graph_attempt_id,
            approval_validator=approval_validator,
        )
    return graph


if __name__ == "__main__":
    raise SystemExit(main())
