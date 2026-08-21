#!/usr/bin/env python3
"""Thin shim over harness_labs.graphrun.campaign_launcher (DTR-LK-KIT).

Self-improvement agent PlanGraph runner (SI-01 … SI-06): six nodes from the
committed decomposition docs/development/self-improvement-decomposition.json,
hand-authored against docs/development/self-improvement-agent-plan.md
(rev 2, operator-approved 2026-08-21). There is no decompose stage: the
decomposition is the reviewed artifact itself.

All launch machinery lives in harness_labs/graphrun/campaign_launcher.py
behind build_campaign_launch_config(); this shim only parameterizes the
product-specific values (plan/decomposition paths, logical graph id) and
forwards argv to that module's CLI entry point. Coordinator/implementer/
reviewer specs, recovery limits, worktree policy, and max_parallelism are
the kit's pinned campaign-proven defaults.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = Path(os.environ.get("HARNESS_LABS_SOURCE", str(ROOT)))
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.graphrun.campaign_launcher import (  # noqa: E402
    build_campaign_launch_config,
    main as _campaign_launcher_main,
)

PLAN_PATH = "docs/development/self-improvement-agent-plan.md"
DECOMPOSITION_PATH = "docs/development/self-improvement-decomposition.json"
# -r2: the amended decomposition (AC-SI01-1 resolution-clause fix) is a new
# plan digest; logs/registration is fail-closed per logical id, and the kit
# does not wire plan_version_transition, so the amendment registers as an
# overt supersession under a new logical id. attempt-1's registration and
# journals remain the historical record of the superseded plan version.
LOGICAL_GRAPH_ID = "self-improvement-agent-r2"


def main() -> int:
    config = build_campaign_launch_config(
        plan_path=PLAN_PATH,
        decomposition_path=DECOMPOSITION_PATH,
        logical_graph_id=LOGICAL_GRAPH_ID,
    )
    return _campaign_launcher_main(config=config)


if __name__ == "__main__":
    raise SystemExit(main())
