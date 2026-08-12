#!/usr/bin/env python3
"""Prepare and issue operator-attested PlanGraph approval artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plan_approval import (  # noqa: E402
    PlanApprovalError,
    issue_receipt,
    prepare_approval,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("decomposition", type=Path)
    prepare.add_argument("--repository", type=Path, default=Path.cwd())
    prepare.add_argument("--output-directory", type=Path, required=True)
    issue = subparsers.add_parser("issue")
    issue.add_argument("--repository", type=Path, default=Path.cwd())
    issue.add_argument("--subject", type=Path, required=True)
    issue.add_argument("--gate-evidence", type=Path, required=True)
    issue.add_argument("--operator-approval", type=Path, required=True)
    issue.add_argument("--receipt", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            result = prepare_approval(
                repository=arguments.repository,
                decomposition_path=arguments.decomposition,
                output_directory=arguments.output_directory,
            )
            payload = {
                "subject": str(result.subject_path),
                "gate_evidence": str(result.gate_evidence_path),
                "subject_sha256": result.subject_sha256,
                "plan_graph_digest": result.plan_graph_digest,
            }
        else:
            receipt = issue_receipt(
                repository=arguments.repository,
                subject_path=arguments.subject,
                gate_evidence_path=arguments.gate_evidence,
                operator_approval_path=arguments.operator_approval,
                receipt_path=arguments.receipt,
            )
            payload = {"receipt": str(receipt)}
    except (OSError, PlanApprovalError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
