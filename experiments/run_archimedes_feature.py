#!/usr/bin/env python3
"""Run an audited FeatureRun that builds the Archimedes simulation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = Path(
    os.environ.get(
        "HARNESS_LABS_SOURCE",
        "/Users/kirillzaslavsky/Documents/harness_labs_feature_worktrees/"
        "production-hardening",
    )
)
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.core.codex_agent_session import CodexAppServerSession
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import RunContract
from harness_labs.core.controller_live import CodexSemanticTaskExecutor
from harness_labs.core.controller_scheduler import RoleProfile
from harness_labs.core.coordinator_dispatcher import CoordinatorLaunch
from harness_labs.core.coordinator_schema import (
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)
from harness_labs.featurerun.feature_run import run_feature_worktree


BASE_REPOSITORY = ROOT / "experiments" / "archimedes"
WORKTREE_ROOT = (
    ROOT.parent / "harness_labs_feature_worktrees" / "archimedes-simulation"
)

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files or
run commands. Use only typed controller tools. Follow the current segment
instructions, inspect structured worker results, and open material artifacts
before advancing. Task context is a JSON object encoded as a string. Never claim
work without worker evidence. Do not delegate beyond the named worker.
"""


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def schema() -> CoordinatorDispatchSchema:
    return CoordinatorDispatchSchema(
        schema_id="archimedes-simulation/1",
        segments=(
            CoordinatorSegment(
                id="build",
                phases=("implement",),
                coordinator_profile="build-coordinator",
                instructions=(
                    "Dispatch exactly one archimedes_builder with repo.read and "
                    "repo.write, details schema "
                    "archimedes-implementation/1, criterion implementation-complete, "
                    "and artifact_kind implementation-summary. Require it to inspect "
                    "the repository contract and incorporate every adversarial lesson "
                    "in its role instructions. Open the summary, then advance."
                ),
            ),
            CoordinatorSegment(
                id="quality",
                phases=("verify", "review"),
                coordinator_profile="quality-coordinator",
                instructions=(
                    "In verify, dispatch exactly one archimedes_verifier with repo.read, "
                    "schema archimedes-verification/1, criterion verification-passed, "
                    "and artifact_kind verification-report. It must rely on the "
                    "controller-owned verification receipt. Open the report and advance "
                    "to review. In review, dispatch exactly one independent "
                    "archimedes_reviewer with repo.read, schema archimedes-review/1, "
                    "criterion review-passed, and artifact_kind review-report. It must "
                    "inspect the actual candidate for physics, teaching, accessibility, "
                    "and interaction defects. Open its report. Block on any material "
                    "finding; otherwise advance to report."
                ),
                context_artifact_kinds=(
                    "implementation-summary",
                    "verification-report",
                ),
                required_artifact_kinds=("implementation-summary",),
            ),
            CoordinatorSegment(
                id="report",
                phases=("report",),
                coordinator_profile="reporting-coordinator",
                instructions=(
                    "Open verification-report and review-report. Dispatch exactly one "
                    "archimedes_reporter with repo.read, schema archimedes-report/1, "
                    "criterion report-ready, and artifact_kind feature-report. Require "
                    "an acceptance matrix and concise usage instructions. Open the "
                    "report, then request run completion."
                ),
                context_artifact_kinds=(
                    "implementation-summary",
                    "verification-report",
                    "review-report",
                ),
                required_artifact_kinds=(
                    "verification-report",
                    "review-report",
                ),
            ),
        ),
    )


def profiles(
    worktree: Path,
    evidence: EvidenceCatalog,
) -> tuple[RoleProfile, ...]:
    definitions = (
        (
            "planner",
            "archimedes_planner",
            frozenset({"repo.read"}),
            frozenset({"archimedes-plan/1"}),
            (
                "Inspect README.md and verify_simulation.py. Plan a polished, "
                "child-friendly interactive water-displacement lesson. Be precise "
                "about mass, displaced volume, density, floating/sinking, controls, "
                "responsive layout, reduced motion, and deterministic verification. "
                "Do not edit files."
            ),
            False,
            (),
            "implementation-plan",
        ),
        (
            "builder",
            "archimedes_builder",
            frozenset({"repo.read", "repo.write"}),
            frozenset({"archimedes-implementation/1"}),
            (
                "Implement the approved plan in index.html, styles.css, and "
                "simulation.js. Make it visually engaging and scientifically correct "
                "for children. Use only local HTML/CSS/JS. Run python3 "
                "verify_simulation.py and fix all failures. Do not edit README.md, "
                "verify_simulation.py, .gitignore, or Git metadata. Incorporate two "
                "adversarial lessons from the preserved prior run: for a floating "
                "object, the fraction below the surface must equal object density "
                "divided by water density; and the tank height, graduated ruler, and "
                "numeric water-level label must share one explicit mL-to-pixel scale "
                "with inputs constrained to the displayed tank capacity. Also scale "
                "the object graphic with the cube root of calculated volume so changing "
                "mass visibly teaches changing volume; include a prediction control "
                "and compare it with the result; and do not emit repetitive live-region "
                "reset announcements for every range-slider input event. Ensure the "
                "water-containing interior, ruler, and JavaScript use the same exact "
                "height after CSS borders and box-sizing are accounted for. A sinking "
                "object must be drawn entirely below the final water surface, normally "
                "resting on the tank bottom, while still displacing its full volume."
            ),
            True,
            (),
            "implementation-summary",
        ),
        (
            "verifier",
            "archimedes_verifier",
            frozenset({"repo.read"}),
            frozenset({"archimedes-verification/1"}),
            (
                "Treat controller_verified_command as authoritative. Inspect the "
                "candidate and report what the deterministic command proves and what "
                "it does not. Satisfy verification-passed only when exit_code is zero. "
                "The FeatureRun integration owner intentionally commits only after all "
                "semantic gates pass, so an otherwise verified candidate being "
                "untracked is expected transaction state, not a disposition-required "
                "finding."
            ),
            False,
            ("python3", "verify_simulation.py"),
            "verification-report",
        ),
        (
            "reviewer",
            "archimedes_reviewer",
            frozenset({"repo.read"}),
            frozenset({"archimedes-review/1"}),
            (
                "Independently inspect the actual HTML, CSS, and JavaScript. Review "
                "physics correctness, the distinction between mass and volume, density "
                "arithmetic, float/sink behavior, child comprehension, keyboard and "
                "screen-reader usability, responsiveness, and external dependencies. "
                "Mark material defects requires_disposition=true. Satisfy review-passed "
                "only if no critical or major defect remains. Non-blocking minor "
                "observations must use requires_disposition=false. If passing, the "
                "satisfied_criteria array must contain the exact bare ID "
                "\"review-passed\" with no rationale or suffix."
            ),
            False,
            (),
            "review-report",
        ),
        (
            "reporter",
            "archimedes_reporter",
            frozenset({"repo.read"}),
            frozenset({"archimedes-report/1"}),
            (
                "Inspect the final repository and produce an evidence-backed feature "
                "report: learning experience, physics model, files, verification, "
                "review outcome, acceptance matrix, and local run instructions. Do "
                "not edit files."
            ),
            False,
            (),
            "feature-report",
        ),
    )
    result = []
    for (
        profile_id,
        role,
        capabilities,
        detail_schemas,
        instructions,
        writable,
        preflight,
        artifact_kind,
    ) in definitions:
        result.append(
            RoleProfile(
                profile_id=profile_id,
                role=role,
                capabilities=capabilities,
                details_schemas=detail_schemas,
                backend_id="codex-exec",
                executor_factory=lambda task, instructions=instructions,
                writable=writable, preflight=preflight,
                artifact_kind=artifact_kind: CodexSemanticTaskExecutor(
                    task=_task_with_artifact_kind(task, artifact_kind),
                    repository=worktree,
                    evidence=evidence,
                    role_instructions=instructions,
                    reasoning="medium",
                    preflight_argv=preflight,
                    require_preflight_success=bool(preflight),
                    sandbox="workspace-write" if writable else "read-only",
                    require_repository_change=writable,
                    writable_paths=(
                        ("index.html", "styles.css", "simulation.js")
                        if writable
                        else ()
                    ),
                    audit=evidence.audit,
                ),
            )
        )
    return tuple(result)


def _task_with_artifact_kind(
    task: dict[str, object],
    artifact_kind: str,
) -> dict[str, object]:
    """Deterministically bind the evidence kind expected at phase boundaries."""

    raw_context = task.get("context", "")
    try:
        context = json.loads(str(raw_context)) if raw_context else {}
    except json.JSONDecodeError:
        context = {"supplied_context": str(raw_context)}
    if not isinstance(context, dict):
        context = {"supplied_context": context}
    context["artifact_kind"] = artifact_kind
    return {**task, "context": json.dumps(context, sort_keys=True)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    arguments = parser.parse_args()
    if not HARNESS_SOURCE.is_dir():
        raise SystemExit(f"Harness source does not exist: {HARNESS_SOURCE}")
    if git(BASE_REPOSITORY, "status", "--porcelain"):
        raise SystemExit("archimedes repository must be clean before FeatureRun")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = arguments.run_id or f"{stamp}-archimedes-feature"
    run_dir = ROOT / "logs" / "runs" / run_id
    worktree = WORKTREE_ROOT.with_name(f"{WORKTREE_ROOT.name}-{run_id}")
    feature_branch = f"feature/{run_id}"

    def contract_factory(
        candidate_worktree: Path,
        receipt: dict[str, object],
    ) -> RunContract:
        return RunContract(
            run_id=run_id,
            objective=(
                "Build, verify, independently review, and report a polished browser "
                "simulation that teaches children how water displacement connects "
                "object volume, mass, density, and floating or sinking."
            ),
            phases=("implement", "verify", "review", "report"),
            criteria=(
                {
                    "id": "implementation-complete",
                    "statement": "The interactive simulation is implemented.",
                    "source": "operator",
                },
                {
                    "id": "verification-passed",
                    "statement": "The controller-owned deterministic check passes.",
                    "source": "operator",
                },
                {
                    "id": "review-passed",
                    "statement": "Independent review finds no material defect.",
                    "source": "operator",
                },
                {
                    "id": "report-ready",
                    "statement": "An evidence-backed feature report exists.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("feature-report",),
            repository={
                "path": str(candidate_worktree),
                "branch": receipt["feature_branch"],
                "base_branch": receipt["base_branch"],
                "base_commit": receipt["base_commit"],
            },
        )

    def session_factory(
        candidate_worktree: Path,
        launch: CoordinatorLaunch,
        evidence: EvidenceCatalog,
    ) -> CodexAppServerSession:
        return CodexAppServerSession(
            reasoning="medium",
            persistent_rollout=True,
            base_instructions=(
                BASE_INSTRUCTIONS
                + "\nSegment-specific instructions:\n"
                + launch.instructions
            ),
            audit=evidence.audit,
        )

    result = run_feature_worktree(
        base_repository=BASE_REPOSITORY,
        base_branch="main",
        feature_branch=feature_branch,
        worktree_path=worktree,
        run_dir=run_dir,
        contract_factory=contract_factory,
        schema=schema(),
        session_factory=session_factory,
        profile_builder=profiles,
        allowed_paths=("index.html", "styles.css", "simulation.js"),
        commit_message="Build Archimedes water displacement simulation",
        merge=True,
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": result.status,
                "phase": result.run_view["phase"],
                "run_dir": str(run_dir),
                "worktree": str(result.worktree_path),
                "coordinator_launches": len(result.dispatch.launches),
                "git_receipts": list(result.git_receipts),
                "base_head": git(BASE_REPOSITORY, "rev-parse", "HEAD"),
                "base_status": git(BASE_REPOSITORY, "status", "--porcelain"),
            },
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
