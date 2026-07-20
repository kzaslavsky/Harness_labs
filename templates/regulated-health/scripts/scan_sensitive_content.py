#!/usr/bin/env python3
"""Run explicitly configured, project-specific sensitive-content checks.

This is a configurable placeholder, not a compliance tool. It intentionally
refuses to claim a scan occurred until reviewed patterns are enabled.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def is_excluded(path: Path, root: Path, exclusions: list[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(relative == item or relative.startswith(item.rstrip("/") + "/") for item in exclusions)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "config" / "sensitive-content-scan.json")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"sensitive-content scan configuration error: {exc}", file=sys.stderr)
        return 2
    patterns = config.get("patterns")
    exclusions = config.get("exclude", [])
    if not config.get("enabled") or not isinstance(patterns, list) or not patterns:
        print("sensitive-content scan is not configured; no compliance claim can be made", file=sys.stderr)
        return 2
    try:
        compiled = [re.compile(pattern) for pattern in patterns]
    except re.error as exc:
        print(f"invalid scan pattern: {exc}", file=sys.stderr)
        return 2
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or is_excluded(path, root, exclusions):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in compiled:
            if pattern.search(text):
                findings.append(f"{path.relative_to(root)} matched {pattern.pattern!r}")
    if findings:
        print("sensitive-content scan found potential matches:")
        print("\n".join(f"  {finding}" for finding in findings))
        return 1
    print("sensitive-content scan passed configured checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
