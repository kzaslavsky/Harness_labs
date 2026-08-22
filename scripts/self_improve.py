#!/usr/bin/env python3
"""Self-improvement agent CLI (SI-05): thin argparse over
``harness_labs.graphrun.improvement_loop``.

Subcommands: ``audit`` (SI-02 mining + SI-03 clustering; ``--propose-if-ready
--judgment module.path:factory`` additionally drafts proposals for patterns
past threshold, resolving ``factory`` as a zero-argument callable that
returns the injected ``JudgmentCallable``), ``open`` (accepted
proposal -> campaign root + seeded ``ConvergenceLedger``), ``round``
(``plan_synthesis`` over the ledger's open findings; ``--launch`` dispatches
through an issued plan-approval receipt), ``remeasure`` (re-run assertions,
fold verdicts, bounded termination -- a successful close also drafts a
``docs/decisions/`` record under the campaign root), ``status``. JSON to
stdout, errors to stderr, exit nonzero on any failure -- no subcommand
prints a placeholder on success.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.graphrun import improvement_loop as loop  # noqa: E402


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_judgment(dotted: str | None):
    """Resolve ``--judgment module.path:factory`` into the ``JudgmentCallable``
    ``run_audit``'s ``--propose-if-ready`` path needs, by importing
    ``module.path`` and calling its zero-argument ``factory`` attribute.
    ``None`` when ``--judgment`` is unset -- mining and clustering still run
    in full, but no proposal is fabricated from a model this CLI was never
    given a way to name."""

    if not dotted:
        return None
    module_name, sep, factory_name = dotted.partition(":")
    if not sep or not module_name or not factory_name:
        raise SystemExit(
            f"--judgment must be 'module.path:factory', got {dotted!r}"
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    return factory()


def _cmd_audit(arguments: argparse.Namespace) -> int:
    result = loop.run_audit(
        repository=arguments.repository, propose_if_ready=arguments.propose_if_ready,
        judgment=_load_judgment(arguments.judgment),
    )
    _print(result.as_dict())
    return 0


def _cmd_open(arguments: argparse.Namespace) -> int:
    opened = loop.open_campaign(
        repository=arguments.repository, proposal_path=arguments.proposal,
        campaign_id=arguments.campaign_id, round_bound=arguments.round_bound,
    )
    _print(opened.as_dict())
    return 0


def _cmd_round(arguments: argparse.Namespace) -> int:
    campaign_root = loop.campaign_root_for(arguments.repository, arguments.campaign_id)
    if arguments.launch:
        round_number = arguments.round_number or loop.latest_round_number(campaign_root)
        if round_number is None:
            print(
                f"no round has been synthesized yet for campaign {arguments.campaign_id!r}",
                file=sys.stderr,
            )
            return 1
        synthesized = loop.load_round(campaign_root, round_number)
        outcome = loop.dispatch_round(
            repository=arguments.repository, campaign_root=campaign_root,
            round=synthesized, receipt_path=arguments.receipt,
        )
        _print({"round_number": round_number, "success": outcome.success, "detail": dict(outcome.detail)})
        return 0 if outcome.success else 1

    synthesized = loop.synthesize_round(
        repository=arguments.repository, campaign_root=campaign_root,
        plan_path=arguments.plan_path, plan_section_id=arguments.plan_section_id,
        plan_section_heading=arguments.plan_section_heading,
    )
    approval_dir = synthesized.round_dir / "approval"
    _print(synthesized.as_dict())
    print(
        "\nHALTED for operator approval. Next:\n"
        f"  python3 scripts/approve_plan.py prepare {synthesized.decomposition_path} "
        f"--repository {arguments.repository} --output-directory {approval_dir}\n"
        f"  # write {approval_dir / 'operator-approval.json'} by hand\n"
        f"  python3 scripts/approve_plan.py issue --repository {arguments.repository} "
        f"--subject {approval_dir / 'subject.json'} "
        f"--gate-evidence {approval_dir / 'gate-evidence.json'} "
        f"--operator-approval {approval_dir / 'operator-approval.json'} "
        f"--receipt {approval_dir / 'receipt.json'}\n"
        f"  python3 scripts/self_improve.py round --campaign-id {arguments.campaign_id} "
        f"--repository {arguments.repository} --launch",
        file=sys.stderr,
    )
    return 0


def _cmd_remeasure(arguments: argparse.Namespace) -> int:
    campaign_root = loop.campaign_root_for(arguments.repository, arguments.campaign_id)
    outcome = loop.remeasure(repository=arguments.repository, campaign_root=campaign_root)
    _print(outcome.as_dict())
    return 0


def _cmd_status(arguments: argparse.Namespace) -> int:
    campaign_root = loop.campaign_root_for(arguments.repository, arguments.campaign_id)
    status = loop.campaign_status(campaign_root=campaign_root)
    _print(status.as_dict())
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="run SI-02 mining + SI-03 clustering")
    audit.add_argument("--repository", type=Path, default=Path.cwd())
    audit.add_argument("--propose-if-ready", action="store_true")
    audit.add_argument(
        "--judgment", default=None,
        help=(
            "module.path:factory naming a zero-argument callable that "
            "returns the JudgmentCallable draft_proposal needs; with "
            "--propose-if-ready and no --judgment, mining/clustering still "
            "run but no proposal is drafted"
        ),
    )
    audit.set_defaults(handler=_cmd_audit)

    open_cmd = subparsers.add_parser("open", help="open a campaign for an accepted proposal")
    open_cmd.add_argument("--proposal", type=Path, required=True)
    open_cmd.add_argument("--repository", type=Path, default=Path.cwd())
    open_cmd.add_argument("--campaign-id", default=None)
    open_cmd.add_argument("--round-bound", type=int, default=loop.DEFAULT_ROUND_BOUND)
    open_cmd.set_defaults(handler=_cmd_open)

    round_cmd = subparsers.add_parser("round", help="synthesize or launch one round")
    round_cmd.add_argument("--campaign-id", required=True)
    round_cmd.add_argument("--repository", type=Path, default=Path.cwd())
    round_cmd.add_argument("--plan-path", default=loop.DEFAULT_PLAN_PATH)
    round_cmd.add_argument("--plan-section-id", default=loop.DEFAULT_PLAN_SECTION_ID)
    round_cmd.add_argument("--plan-section-heading", default=loop.DEFAULT_PLAN_SECTION_HEADING)
    round_cmd.add_argument("--launch", action="store_true")
    round_cmd.add_argument("--round-number", type=int, default=None)
    round_cmd.add_argument("--receipt", type=Path, default=None)
    round_cmd.set_defaults(handler=_cmd_round)

    remeasure = subparsers.add_parser("remeasure", help="re-run assertions and fold verdicts")
    remeasure.add_argument("--campaign-id", required=True)
    remeasure.add_argument("--repository", type=Path, default=Path.cwd())
    remeasure.set_defaults(handler=_cmd_remeasure)

    status = subparsers.add_parser("status", help="report campaign state")
    status.add_argument("--campaign-id", required=True)
    status.add_argument("--repository", type=Path, default=Path.cwd())
    status.set_defaults(handler=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except loop.ImprovementLoopError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
