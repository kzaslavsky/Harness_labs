#!/usr/bin/env python3
"""Require an archived Markdown plan to link its recorded decision file."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from state_io import StateError


MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def validate(plan: Path, decision_record: Path) -> dict[str, str]:
    plan = plan.resolve()
    decision_record = decision_record.resolve()
    links = []
    for raw in MARKDOWN_LINK.findall(plan.read_text(encoding="utf-8")):
        target = raw.split("#", 1)[0]
        if target and "://" not in target:
            links.append((plan.parent / target).resolve())
    if decision_record not in links:
        raise StateError("archived Markdown plan does not link its recorded decision file")
    if not decision_record.is_file():
        raise StateError("recorded decision file does not exist")
    return {"status": "valid", "plan": str(plan), "decision_record": str(decision_record)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("decision_record", type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.plan, args.decision_record)
    except (OSError, StateError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
