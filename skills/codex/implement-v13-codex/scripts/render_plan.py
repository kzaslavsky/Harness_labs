#!/usr/bin/env python3
"""Render the authoritative plan JSON into deterministic Markdown."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _bullets(values: list[Any]) -> list[str]:
    return [f"- {value}" for value in values] or ["- None"]


def render(plan: dict[str, Any]) -> str:
    """Return the compatibility Markdown view of a validated plan."""
    lines = [
        f"# Implementation Plan: {plan['task']}",
        "",
        "**Status:** Current",
        f"**Protocol:** {plan['protocol']}",
        f"**Complexity:** {plan['complexity']}",
        f"**Critical path share:** {plan['critical_path_share']:.2f}",
        "",
        "## Scope",
        "",
        "### In",
        "",
        *_bullets(plan.get("scope", {}).get("in", [])),
        "",
        "### Out",
        "",
        *_bullets(plan.get("scope", {}).get("out", [])),
        "",
        "## Governing contracts",
        "",
        *_bullets(plan.get("governing_contracts", [])),
        "",
        "## Steps",
        "",
    ]
    for position, step in enumerate(plan.get("steps", []), 1):
        if isinstance(step, dict):
            label = step.get("title") or step.get("id") or json.dumps(step, sort_keys=True)
        else:
            label = str(step)
        lines.append(f"{position}. {label}")
    lines.extend(["", "## Runtime contracts", "", *_bullets(plan.get("runtime_contracts", []))])
    lines.extend(["", "## Testing strategy", "", *_bullets(plan.get("testing_strategy", []))])
    lines.extend(["", "## Review lenses", ""])
    for position, lens in enumerate(plan.get("review_lenses", []), 1):
        lines.append(f"{position}. `{lens['id']}` — {lens['charge']}")
    lines.extend(["", "## Risks", "", *_bullets(plan.get("risks", [])), ""])
    return "\n".join(lines)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("plan_json", type=Path)
    parser.add_argument("markdown", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    args.markdown.write_text(render(plan), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
