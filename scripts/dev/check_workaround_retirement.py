#!/usr/bin/env python3
"""Deterministic gate for CB-08: the program's own workarounds are retired.

Checks the experiment launcher and the living diagnosis:
1. BASE_INSTRUCTIONS no longer pins bare criterion ids or frozen
   required_capabilities (those pins exist only because of the gates CB-01
   removes).
2. Plan-graph criteria bind with source "plan", not the "operator" workaround.
3. assemble_decomposition no longer mechanically appends objectives/criterion
   statements to plan sections (the compliance transformation CB-02 removes).
4. Every diagnosis item a program node resolved records its landing node
   ("landed (CB-…" appears at least 6 times; no worklist item section still
   says "Status:** open" for items 1, 2, 5, 7 at minimum).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "experiments" / "run_burden_plan_graph.py"
DIAGNOSIS = ROOT / "docs" / "development" / "contract-burden-reduction.md"


def main() -> int:
    failures: list[str] = []
    runner = RUNNER.read_text(encoding="utf-8")
    if "bare criterion ids" in runner:
        failures.append("runner still pins bare criterion ids in BASE_INSTRUCTIONS")
    if "required_capabilities and details schema unchanged" in runner.replace("\n", " "):
        failures.append("runner still pins frozen required_capabilities in BASE_INSTRUCTIONS")
    if '"source": "operator"' in runner:
        failures.append('runner still binds criteria with source "operator"')
    if '"source": "plan"' not in runner:
        failures.append('runner does not bind criteria with source "plan"')
    if "additions" in runner and "criterion statements" not in runner:
        # The normalization loop appends objective/criterion strings; its
        # signature variable is `additions`.
        failures.append("assemble_decomposition still normalizes sections mechanically")

    diagnosis = DIAGNOSIS.read_text(encoding="utf-8")
    if len(re.findall(r"landed \(CB-", diagnosis)) < 6:
        failures.append("diagnosis records fewer than 6 landed items")
    for item in ("### 1\\.", "### 2\\.", "### 5\\.", "### 7\\."):
        section = re.search(item + r".*?(?=\n### |\n## )", diagnosis, re.S)
        if section and re.search(r"\*\*Status:\*\* open\b", section.group(0)):
            failures.append(f"diagnosis section {item} still open")

    print(json.dumps({"failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
