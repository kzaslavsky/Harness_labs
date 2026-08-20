#!/usr/bin/env python3
"""Thin shim over harness_labs.graphrun.campaign_launcher (DTR-F5 / DTR-LK-KIT).

Convergence campaign harness PlanGraph runner (CC-01 … CC-05, CC-07): six
nodes from the committed decomposition
docs/development/convergence-campaign-decomposition.json, hand-authored
against docs/development/convergence-campaign-plan.md and decomposition-
reviewed (verdict DECOMPOSABLE-WITH-EDITS, edits applied). There is no
decompose stage: the decomposition is the reviewed artifact itself.

Stages (prepare / issue / run / resume) and the full launch machinery — the
worker instructions, review-fix wiring, and PlanGraph run/resume calls — now
live in harness_labs/graphrun/campaign_launcher.py, extracted behind
build_campaign_launch_config(). This file only resolves the harness_labs
package on sys.path and forwards argv to that module's CLI entry point, so
the product-specific values below exist to document the runner rather than
to configure it — campaign_launcher.build_campaign_launch_config() carries
the pinned values (coordinator/implementer/reviewer specs and models,
recovery/continuation/verification-repair limits, worktree-policy booleans,
max_parallelism) and this shim's own defaults (plan/decomposition paths,
logical graph id) already match them.

Agent mixture (operator-fixed):
  coordinator   claude:claude-opus-4-8[1m]@medium
  implementers  claude-sonnet-5 (high effort)
  reviewers     claude-opus-5 (high effort)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = Path(os.environ.get("HARNESS_LABS_SOURCE", str(ROOT)))
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.graphrun.campaign_launcher import (  # noqa: E402
    main as _campaign_launcher_main,
)


def main() -> int:
    return _campaign_launcher_main()


if __name__ == "__main__":
    raise SystemExit(main())
