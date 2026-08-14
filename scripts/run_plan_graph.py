#!/usr/bin/env python3
"""Register an immutable PlanGraph or run one audited attempt."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plangraph.plan_approval import PlanApprovalAdmission, PlanApprovalError
from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    RepairResumeDirective,
    SubprocessFeatureRunLauncher,
    load_registration,
    persist_registration,
    register_plan_graph,
)
from harness_labs.plangraph.plan_graph_budget import BudgetError, RetryBudgetLedger


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


def _approval_lineage_id(
    repository_id: str,
    decomposition_path: str,
) -> str:
    """Bind receipt-backed registrations to a stable approved graph slot.

    Approval digests deliberately include the plan revision and base commit, so
    using one as the ledger identity would mint a new retry allowance for every
    re-approval.  The repository-owned decomposition path identifies the slot.
    ``logical_graph_id`` is deliberately excluded: it identifies repair state
    and is required only for a resume invocation, so including it would cause a
    fresh attempt and its successor to select different ledgers.
    """
    identity = {
        "repository_id": repository_id,
        "decomposition_path": decomposition_path,
    }
    return "approval-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    modes = parser.add_subparsers(dest="mode", required=True)

    register = modes.add_parser("register")
    register.add_argument("decomposition", type=Path)
    register.add_argument("--repository", type=Path, required=True)
    register.add_argument("--logical-graph-id", required=True)
    register.add_argument("--registration-root", type=Path)
    register.add_argument("--lineage-id")
    register.add_argument("--automatic-recovery", type=Path)
    register.add_argument("--transition", type=Path)

    budget = modes.add_parser("budget")
    budget.add_argument("operation", choices=("extend", "reset"))
    budget.add_argument("--repository", type=Path, required=True)
    budget.add_argument("--run-root", type=Path)
    budget.add_argument("--lineage-id", required=True)
    budget.add_argument("--node", required=True)
    budget.add_argument("--launches", type=int, default=1)
    budget.add_argument("--accept-gate-change", action="store_true")
    budget.add_argument("--accept-plan-sha256")
    budget.add_argument("--carryover", choices=("full", "reset"), default="full")
    budget.add_argument("--reason", required=True)

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
    run.add_argument("--lineage-id")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--logical-graph-id")
    run.add_argument("--predecessor-attempt-id")
    run.add_argument("--retry-frontier", action="append", default=[])
    run.add_argument("--blocker-evidence-ref")
    run.add_argument("--on-block-argv")
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    repository = arguments.repository.resolve()
    if arguments.mode == "budget":
        try:
            ledger = RetryBudgetLedger(_repository_path(repository, arguments.run_root, "logs/runs"), arguments.lineage_id)
            if arguments.operation == "extend":
                ledger.extend(node_id=arguments.node, launches=arguments.launches, reason=arguments.reason)
            else:
                ledger.reset(node_id=arguments.node, reason=arguments.reason, accept_gate_change=arguments.accept_gate_change, accept_plan_sha256=arguments.accept_plan_sha256, carryover=arguments.carryover)
        except BudgetError as exc:
            print(f"PlanGraph budget failed: {exc}", file=sys.stderr)
            return 3
        print(json.dumps({"lineage_id": arguments.lineage_id, "node_id": arguments.node, "operation": arguments.operation}, sort_keys=True))
        return 0
    if arguments.mode == "register":
        payload = json.loads(arguments.decomposition.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("decomposition must be a JSON object")
        authority = None
        if arguments.automatic_recovery is not None:
            authority = json.loads(arguments.automatic_recovery.read_text(encoding="utf-8"))
            if not isinstance(authority, dict):
                raise ValueError("automatic recovery authority must be a JSON object")
        transition = None
        if arguments.transition is not None:
            transition = json.loads(arguments.transition.read_text(encoding="utf-8"))
            if not isinstance(transition, dict):
                raise ValueError("plan-version transition must be a JSON object")
        registration = register_plan_graph(
            repository=repository,
            logical_graph_id=arguments.logical_graph_id,
            decomposition=payload,
            plan_lineage_id=arguments.lineage_id,
            automatic_recovery=authority,
            plan_version_transition=transition,
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
            approval_lineage_id = _approval_lineage_id(
                approved.repository_id,
                approved.decomposition_path,
            )
            if (
                arguments.lineage_id is not None
                and arguments.lineage_id != approval_lineage_id
            ):
                parser.error("--lineage-id must match the approval-bound retry lineage")
            registration = register_plan_graph(
                repository=repository,
                logical_graph_id=arguments.logical_graph_id
                or arguments.graph_attempt_id,
                decomposition=approved.decomposition,
                base_commit=approved.base_commit,
                repository_id=approved.repository_id,
                plan_lineage_id=approval_lineage_id,
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
        if arguments.lineage_id is not None and arguments.lineage_id != registration.plan_lineage_id:
            parser.error("--lineage-id must match the persisted registration lineage")
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
        on_block_argv = None
        if arguments.on_block_argv is not None:
            on_block_argv = json.loads(arguments.on_block_argv)
            if not isinstance(on_block_argv, list) or not all(isinstance(value, str) and value for value in on_block_argv):
                raise ValueError("--on-block-argv must be a JSON array of non-empty strings")
        graph = _build_graph(arguments, parser, repository, registration, launch, run_root, approval_validator, on_block_argv)
        result = graph.run()
    except (PlanGraphError, ValueError, json.JSONDecodeError) as exc:
        print(f"PlanGraph failed: {exc}", file=sys.stderr)
        return 3 if any(
            marker in str(exc)
            for marker in ("retry budget", "gate-change block", "changed-plan lineage", "operator intervention required")
        ) else 1
    print(
        json.dumps(
            {
                "status": result.status,
                "candidate_commit": result.candidate_commit,
                "failed_run_id": result.failed_run_id,
                "logical_graph_id": registration.logical_graph_id,
                "graph_attempt_id": graph.graph_run_id,
                "registration_digest": registration.graph_digest,
                "status_flags": PlanGraph._status_flags(result.status),
                "deviation_records": [dict(record) for record in result.deviation_records],
                "deviation_summary": PlanGraph._result_payload(result)["deviation_summary"],
                "blocker_evidence_ref": graph._audit_for_run().state.get("block_escalation_ref"),
                "on_block_hook_log": str((run_root / graph.graph_run_id / "on-block-hook.log")) if graph.on_block_argv else None,
                "on_block_hook": graph.on_block_hook,
            },
            sort_keys=True,
        )
    )
    return 0 if result.status in {"succeeded", "completed_with_deviations", "completed_under_full_autonomy"} else 1


def _build_graph(arguments, parser, repository, registration, launch, run_root, approval_validator, on_block_argv=None):
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
            on_block_argv=on_block_argv,
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
            on_block_argv=on_block_argv,
        )
    return graph


if __name__ == "__main__":
    raise SystemExit(main())
