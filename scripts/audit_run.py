#!/usr/bin/env python3
"""Verify or terminalize a durable Harness Labs audit run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness_labs import AuditActor, AuditJournal


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("run_dir", type=Path)
    recover = subparsers.add_parser("recover")
    recover.add_argument("run_dir", type=Path)
    recover.add_argument("--reason", required=True)
    args = parser.parse_args()

    if args.command == "verify":
        result = AuditJournal.verify(args.run_dir)
    else:
        result = AuditJournal.recover_interrupted(
            args.run_dir,
            actor=AuditActor("audit-recovery", "recovery"),
            reason=args.reason,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
