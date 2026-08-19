#!/usr/bin/env python3
"""Prepare and issue operator-attested PlanGraph approval artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plangraph.plan_approval import (  # noqa: E402
    PlanApprovalError,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_refinement import (  # noqa: E402
    refine_repository_decomposition,
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
    refine = subparsers.add_parser("refine")
    refine.add_argument("decomposition", type=Path)
    refine.add_argument("--repository", type=Path, default=Path.cwd())
    refine.add_argument("--report", type=Path)
    refine.add_argument("--revised-decomposition", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            result = prepare_approval(
                repository=arguments.repository,
                decomposition_path=arguments.decomposition,
                output_directory=arguments.output_directory,
            )
            # The warnings used to reach gate-evidence.json and stop. An
            # operator who never opens that file approved 17 predicted join
            # conflicts on one real campaign, so they are reported here with
            # the digest an acknowledgement has to name.
            payload = {
                "subject": str(result.subject_path),
                "gate_evidence": str(result.gate_evidence_path),
                "subject_sha256": result.subject_sha256,
                "plan_graph_digest": result.plan_graph_digest,
                "warnings": [
                    {**dict(warning), "warning_sha256": warning_identity(warning)}
                    for warning in result.warnings
                ],
                "high_severity_warnings": sum(
                    1 for warning in result.warnings
                    if warning.get("severity") == "high"
                ),
            }
        elif arguments.command == "refine":
            # No judge is wired in from the command line: the CLI reports what
            # it would repair and leaves the decomposition alone. Callers that
            # want the loop to revise inject a judge through the library.
            outcome = refine_repository_decomposition(
                repository=arguments.repository,
                decomposition_path=arguments.decomposition,
            )
            record = outcome.as_mapping()
            if arguments.report is not None:
                arguments.report.parent.mkdir(parents=True, exist_ok=True)
                arguments.report.write_text(
                    json.dumps(record, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            if arguments.revised_decomposition is not None:
                arguments.revised_decomposition.parent.mkdir(
                    parents=True, exist_ok=True
                )
                arguments.revised_decomposition.write_text(
                    json.dumps(outcome.decomposition, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            payload = {
                "status": outcome.status,
                "reason": outcome.reason,
                "initial_warnings": dict(outcome.initial_warnings),
                "final_warnings": dict(outcome.final_warnings),
                "revised": outcome.revised,
                "report": str(arguments.report) if arguments.report else None,
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
