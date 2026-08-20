#!/usr/bin/env python3
"""Transcribe operator statements into a sealed finding-intake artifact.

Single-statement mode: an operator messages a session mid-campaign; the
session appends a keyed finding for the next round. ``--batch`` mode
transcribes a seed-audit JSON file of statements into one sealed artifact.

This CLI never calls ``ConvergenceLedger.ingest_audit``: folding a partial
statement mid-round would mark every other open key ``unobserved`` and
fabricate failed repair claims (the exact refusal
``scripts/run_convergence_campaign.py`` documents for harvested findings).
The sealed artifact is carried for the next round's real measure/ingest
path. Re-running with byte-identical input reseals the same digest and
changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plangraph.convergence_campaign import (  # noqa: E402
    CampaignArtifactStore,
)
from harness_labs.plangraph.convergence_ledger import ConvergenceLedger  # noqa: E402
from harness_labs.plangraph.finding_intake import (  # noqa: E402
    FindingIntakeError,
    IntakeQuestion,
    draft_finding,
    draft_findings_batch,
    seal_findings,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "statement", nargs="?",
        help="free-text operator statement (single-statement mode)",
    )
    parser.add_argument(
        "--batch", type=Path, default=None,
        help="seed-audit JSON file: a list of statement strings or "
        '{"statement": ..., "evidence_refs": [...]} objects',
    )
    parser.add_argument(
        "--ledger", type=Path, required=True,
        help="path to the campaign's ConvergenceLedger journal "
        "(read-only: checked for an already-open key, never ingested into)",
    )
    parser.add_argument(
        "--campaign-root", type=Path, required=True,
        help="campaign root; artifacts are sealed under <root>/artifacts",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path("."),
        help="working tree to search for the owning file of each statement",
    )
    parser.add_argument(
        "--target", required=True,
        help="subsystem/product label recorded as each finding's category",
    )
    parser.add_argument(
        "--evidence-ref", action="append", default=[],
        help="capture-evidence ref attached to a single statement (repeatable)",
    )
    return parser


def _load_batch(path: Path) -> tuple[list[str], dict[int, tuple[str, ...]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise FindingIntakeError(f"seed-audit file {path} must hold a JSON list")
    statements: list[str] = []
    evidence_by_index: dict[int, tuple[str, ...]] = {}
    for index, item in enumerate(raw):
        if isinstance(item, str):
            statements.append(item)
        elif isinstance(item, dict) and isinstance(item.get("statement"), str):
            statements.append(item["statement"])
            refs = item.get("evidence_refs") or []
            if refs:
                evidence_by_index[index] = tuple(refs)
        else:
            raise FindingIntakeError(
                f"seed-audit entry {index} must be a statement string or an "
                'object with a "statement" field'
            )
    return statements, evidence_by_index


def _warn_if_already_open(ledger_path: Path, key: tuple[str, str]) -> None:
    if not ledger_path.exists():
        return
    if key in ConvergenceLedger(ledger_path).open_set():
        print(
            f"warning: key {key!r} is already open in the ledger at "
            f"{ledger_path}; this statement will be treated as a new "
            "finding, not a duplicate, at the next real ingest",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if bool(args.batch) == bool(args.statement):
        print(
            "error: pass exactly one of a STATEMENT or --batch SEED_FILE",
            file=sys.stderr,
        )
        return 2

    repo_root = args.repo_root.resolve()
    store = CampaignArtifactStore(args.campaign_root / "artifacts")

    try:
        if args.batch is not None:
            statements, evidence_by_index = _load_batch(args.batch)
            findings = draft_findings_batch(
                statements,
                repo_root=repo_root,
                target=args.target,
                evidence_refs_by_index=evidence_by_index,
            )
        else:
            result = draft_finding(
                args.statement,
                repo_root=repo_root,
                target=args.target,
                evidence_refs=tuple(args.evidence_ref),
            )
            if isinstance(result, IntakeQuestion):
                print(f"error: ambiguous statement -- {result.reason}", file=sys.stderr)
                for candidate in result.candidates:
                    print(f"  candidate: {candidate}", file=sys.stderr)
                return 1
            findings = (result,)
    except FindingIntakeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for finding in findings:
        _warn_if_already_open(args.ledger, (finding.file, finding.subject))

    record = seal_findings(findings, store)
    print(f"sealed {len(findings)} finding(s) as digest {record.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
