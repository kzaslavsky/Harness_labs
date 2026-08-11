#!/usr/bin/env python3
"""Run one live schema-dispatched FeatureRun against the rocketship experiment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness_labs.codex_agent_session import CodexAppServerSession
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import RunContract, RunLimits
from harness_labs.controller_live import CodexSemanticTaskExecutor
from harness_labs.controller_scheduler import RoleProfile
from harness_labs.coordinator_dispatcher import (
    CoordinatorLaunch,
    run_dispatched_controller,
)
from harness_labs.coordinator_schema import (
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)


REPOSITORY = ROOT / "experiments" / "rocketship"

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files or
run commands. Use only the typed controller tools. Dispatch exactly the bounded
worker described by your segment instructions, inspect its structured result,
and open its material artifact before advancing. Task contexts are JSON objects
encoded as strings and must include the requested artifact_kind. Use only the
advertised role, capabilities, and details schema. Never claim work without
worker evidence. Do not issue a final answer while the run is still active.
"""


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def schema() -> CoordinatorDispatchSchema:
    return CoordinatorDispatchSchema(
        schema_id="rocketship-feature/1",
        segments=(
            CoordinatorSegment(
                id="plan",
                phases=("plan",),
                coordinator_profile="planning-coordinator",
                instructions=(
                    "Dispatch one rocketship_planner task using rocketship-plan/1 "
                    "and repo.read. It must produce artifact implementation-plan and "
                    "satisfy plan-ready. Open the artifact, then advance to implement."
                ),
                context_artifact_kinds=(),
                max_tool_calls=24,
            ),
            CoordinatorSegment(
                id="build",
                phases=("implement",),
                coordinator_profile="build-coordinator",
                instructions=(
                    "Open the required implementation-plan. Dispatch one "
                    "rocketship_builder task using rocketship-implementation/1 and "
                    "repo.read plus repo.write. Put artifact_kind "
                    "implementation-summary and the plan artifact ref in context. The "
                    "worker must implement the animation and satisfy "
                    "implementation-complete. Open its artifact, then advance to verify."
                ),
                context_artifact_kinds=("implementation-plan",),
                required_artifact_kinds=("implementation-plan",),
                max_tool_calls=28,
            ),
            CoordinatorSegment(
                id="verify-review",
                phases=("verify", "review"),
                coordinator_profile="quality-coordinator",
                instructions=(
                    "In verify, dispatch one rocketship_verifier task using "
                    "rocketship-verification/1 and repo.read. It must produce "
                    "verification-report and satisfy verification-passed only from the "
                    "controller-owned check. Open it and advance to review. In review, "
                    "dispatch one independent rocketship_reviewer using "
                    "rocketship-review/1 and repo.read. It must inspect the actual "
                    "candidate, produce review-report, and satisfy review-passed only "
                    "if no material defect remains. Open it. If a material finding "
                    "exists, block rather than waive it; otherwise advance to report."
                ),
                context_artifact_kinds=(
                    "implementation-plan",
                    "implementation-summary",
                    "verification-report",
                ),
                required_artifact_kinds=("implementation-summary",),
                max_tool_calls=40,
            ),
            CoordinatorSegment(
                id="report",
                phases=("report",),
                coordinator_profile="reporting-coordinator",
                instructions=(
                    "Open the required verification-report and review-report. Dispatch "
                    "one rocketship_reporter using rocketship-report/1 and repo.read. "
                    "It must produce feature-report and satisfy report-ready. Open the "
                    "artifact, inspect the acceptance matrix, then request completion."
                ),
                context_artifact_kinds=(
                    "implementation-plan",
                    "implementation-summary",
                    "verification-report",
                    "review-report",
                ),
                required_artifact_kinds=(
                    "verification-report",
                    "review-report",
                ),
                max_tool_calls=28,
            ),
        ),
    )


def profiles(evidence: EvidenceCatalog) -> tuple[RoleProfile, ...]:
    definitions = (
        (
            "planner",
            "rocketship_planner",
            frozenset({"repo.read"}),
            frozenset({"rocketship-plan/1"}),
            (
                "Inspect README.md and verify_animation.py. Produce a concise build "
                "plan for index.html, styles.css, and animation.js, including visual "
                "composition, launch timing, replay, reduced motion, and the exact "
                "deterministic acceptance command. Do not edit files."
            ),
            False,
            (),
        ),
        (
            "builder",
            "rocketship_builder",
            frozenset({"repo.read", "repo.write"}),
            frozenset({"rocketship-implementation/1"}),
            (
                "Implement the supplied plan completely in index.html, styles.css, "
                "and animation.js. Create a polished self-contained rocket launch "
                "animation using semantic HTML, CSS shapes/animation, and small local "
                "JavaScript for replay. Honor prefers-reduced-motion. Run "
                "python3 verify_animation.py and fix failures. Do not modify the "
                "acceptance checker or repository policy files."
            ),
            True,
            (),
        ),
        (
            "verifier",
            "rocketship_verifier",
            frozenset({"repo.read"}),
            frozenset({"rocketship-verification/1"}),
            (
                "Use the supplied controller_verified_command receipt as the "
                "authoritative deterministic check. Inspect the generated files and "
                "report exactly what passed. Never claim verification-passed unless "
                "the receipt exit_code is zero."
            ),
            False,
            ("python3", "verify_animation.py"),
        ),
        (
            "reviewer",
            "rocketship_reviewer",
            frozenset({"repo.read"}),
            frozenset({"rocketship-review/1"}),
            (
                "Independently review the actual HTML, CSS, and JavaScript against "
                "README.md and verify_animation.py. Check animation coherence, replay, "
                "accessibility, responsive layout, and external dependencies. Report "
                "material findings with requires_disposition=true. Satisfy "
                "review-passed only if no critical or major defect remains."
            ),
            False,
            (),
        ),
        (
            "reporter",
            "rocketship_reporter",
            frozenset({"repo.read"}),
            frozenset({"rocketship-report/1"}),
            (
                "Inspect the final repository and summarize the delivered experience, "
                "files, verification evidence, review outcome, and how to run it. Do "
                "not edit files."
            ),
            False,
            (),
        ),
    )
    result = []
    for (
        profile_id,
        role,
        capabilities,
        details_schemas,
        instructions,
        writable,
        preflight,
    ) in definitions:
        result.append(
            RoleProfile(
                profile_id=profile_id,
                role=role,
                capabilities=capabilities,
                details_schemas=details_schemas,
                backend_id="codex-exec",
                executor_factory=lambda task, instructions=instructions,
                writable=writable, preflight=preflight: CodexSemanticTaskExecutor(
                    task=task,
                    repository=REPOSITORY,
                    evidence=evidence,
                    role_instructions=instructions,
                    reasoning="medium",
                    timeout_seconds=900,
                    preflight_argv=preflight,
                    require_preflight_success=bool(preflight),
                    sandbox="workspace-write" if writable else "read-only",
                    require_repository_change=writable,
                    audit=evidence.audit,
                ),
            )
        )
    return tuple(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    arguments = parser.parse_args()
    if git("status", "--porcelain"):
        raise SystemExit("rocketship repository must be clean before the run")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = arguments.run_id or f"{stamp}-rocketship-feature"
    run_dir = ROOT / "logs" / "runs" / run_id
    contract = RunContract(
        run_id=run_id,
        objective=(
            "Create a polished, self-contained browser animation of a rocketship "
            "taking off in the isolated rocketship experiment repository. Plan, "
            "implement, deterministically verify, independently review, and report."
        ),
        phases=("plan", "implement", "verify", "review", "report"),
        criteria=(
            {
                "id": "plan-ready",
                "statement": "The implementation is planned against repository checks.",
                "source": "operator",
            },
            {
                "id": "implementation-complete",
                "statement": "The rocketship animation is implemented in the repository.",
                "source": "operator",
            },
            {
                "id": "verification-passed",
                "statement": "The controller-owned deterministic check passes.",
                "source": "operator",
            },
            {
                "id": "review-passed",
                "statement": "Independent review finds no remaining material defect.",
                "source": "operator",
            },
            {
                "id": "report-ready",
                "statement": "A final evidence-backed feature report exists.",
                "source": "operator",
            },
        ),
        terminal_artifact_kinds=("feature-report",),
        limits=RunLimits(
            max_depth=1,
            max_subagents=5,
            max_parallelism=1,
            max_tasks=5,
        ),
        repository={
            "path": str(REPOSITORY),
            "branch": git("branch", "--show-current"),
            "base_commit": git("rev-parse", "HEAD"),
        },
    )

    def session_factory(
        launch: CoordinatorLaunch,
        evidence: EvidenceCatalog,
    ) -> CodexAppServerSession:
        return CodexAppServerSession(
            reasoning="medium",
            timeout_seconds=300,
            persistent_rollout=True,
            base_instructions=(
                BASE_INSTRUCTIONS
                + "\nSegment-specific instructions:\n"
                + launch.instructions
            ),
            audit=evidence.audit,
        )

    result = run_dispatched_controller(
        contract,
        schema=schema(),
        session_factory=session_factory,
        profile_builder=profiles,
        run_dir=run_dir,
        evidence_classification="production_lifecycle",
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": result.run_view["status"],
                "phase": result.run_view["phase"],
                "run_dir": str(run_dir),
                "launches": len(result.dispatch.launches),
                "repository_status": git("status", "--porcelain"),
            },
            indent=2,
        )
    )
    return 0 if result.run_view["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
