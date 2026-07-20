#!/usr/bin/env python3
"""Regenerate the active-learning summary from the portable learning ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {"id", "severity", "status", "tags", "problem", "solution"}


def load_ledger(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read ledger {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError("Ledger must be an object with an entries list")
    schema = value.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("Ledger must define a schema object")
    return value


def active_entries(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    severities = set(ledger["schema"].get("severity", []))
    results: list[dict[str, Any]] = []
    for index, entry in enumerate(ledger["entries"], 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {index} must be an object")
        missing = REQUIRED_FIELDS - set(entry)
        if missing:
            raise ValueError(f"Entry {index} is missing: {', '.join(sorted(missing))}")
        if entry["severity"] not in severities:
            raise ValueError(f"Entry {index} has an unknown severity")
        if entry["status"] == "active":
            results.append(entry)
    return sorted(results, key=lambda item: (item["severity"], item["id"]))


def render(entries: list[dict[str, Any]], project_name: str) -> str:
    lines = [
        f"# {project_name} pitfalls",
        "",
        "**Status:** Current",
        "**Purpose:** Generated quick-reference guidance from `learnings/learnings.json`.",
        "",
    ]
    if not entries:
        lines.extend(["## No active learnings yet", "", "Capture verified, reusable lessons in the ledger.", ""])
        return "\n".join(lines)
    for entry in entries:
        tags = ", ".join(str(tag) for tag in entry["tags"])
        lines.extend([
            f"## {entry['severity']} — {entry['id']}",
            "",
            f"**Tags:** {tags or 'none'}",
            "",
            f"**Problem:** {entry['problem']}",
            "",
            f"**Guidance:** {entry['solution']}",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=here.parent / "learnings.json")
    parser.add_argument("--output", type=Path, default=here.parent / "PITFALLS.md")
    parser.add_argument("--project-name", default="Project")
    args = parser.parse_args()
    try:
        args.output.write_text(render(active_entries(load_ledger(args.ledger)), args.project_name), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"regenerate_md.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
