#!/usr/bin/env python3
"""Render a bounded PHI-free run manifest from validated JSON artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def render(record: dict[str, Any]) -> str:
    """Return deterministic run-manifest Markdown."""
    lines = [
        f"# Run manifest: {record['task']}",
        "",
        "**Status:** Current",
        f"**Result:** {record['status']}",
        f"**Runner:** {record['runner']}",
        f"**Queue run:** {record['queue_run_id']}",
        f"**Feature run:** {record['feature_run_id']}",
        f"**Base branch:** {record['base_branch']}",
        f"**Archived plan JSON:** {record['plan_json']}",
        f"**Archived plan Markdown:** {record['plan_markdown']}",
        "",
        "## Decisions",
        "",
    ]
    for decision in record.get("decisions", []):
        lines.extend(
            [
                f"### {decision['id']} — {decision['title']}",
                "",
                f"- Status: {decision['status']}",
                f"- Decision: {decision['decision']}",
                f"- Evidence: {decision['evidence']}",
                "",
            ]
        )
    lines.extend(["## Review", "", f"- {record.get('review_summary', 'No summary supplied')}", ""])
    lines.extend(["## Gates", ""])
    for gate in record.get("gates", []):
        lines.append(f"- {gate['name']}: {gate['status']} — {gate.get('evidence', '')}")
    lines.extend(["", "## Artifact hashes", ""])
    for name, digest in sorted(record.get("artifact_hashes", {}).items()):
        lines.append(f"- `{name}`: `{digest}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    record = json.loads(args.record.read_text(encoding="utf-8"))
    args.output.write_text(render(record), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
