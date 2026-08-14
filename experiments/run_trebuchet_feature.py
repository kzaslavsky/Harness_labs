#!/usr/bin/env python3
"""Run an audited FeatureRun that builds the trebuchet simulation."""

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
        "review-fix-ledger",
    )
)
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.core.codex_agent_session import CodexAppServerSession  # noqa: E402
from harness_labs.core.controller_evidence import EvidenceCatalog  # noqa: E402
from harness_labs.core.controller_kernel import RunContract  # noqa: E402
from harness_labs.core.controller_live import CodexSemanticTaskExecutor  # noqa: E402
from harness_labs.core.controller_scheduler import RoleProfile  # noqa: E402
from harness_labs.core.coordinator_dispatcher import CoordinatorLaunch  # noqa: E402
from harness_labs.core.coordinator_schema import (  # noqa: E402
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)
from harness_labs.feature_run import (  # noqa: E402
    ReviewFixPolicy,
    run_feature_worktree,
)


BASE_REPOSITORY = ROOT / "experiments" / "trebuchet"
WORKTREE_ROOT = ROOT.parent / "harness_labs_feature_worktrees" / "trebuchet-simulation"
WRITABLE_PATHS = ("index.html", "styles.css", "simulation.js")
CODEX_EXECUTABLE = os.environ.get(
    "TREBUCHET_CODEX_EXECUTABLE",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files or
run commands. Use only typed controller tools. Follow the segment instructions,
inspect structured worker results, and open material artifacts before advancing.
Never claim work without evidence. Do not delegate beyond the named worker.
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
        schema_id="trebuchet-simulation/1",
        segments=(
            CoordinatorSegment(
                id="build",
                phases=("implement",),
                coordinator_profile="build-coordinator",
                instructions=(
                    "Dispatch exactly one trebuchet_builder with repo.read and "
                    "repo.write, details schema trebuchet-implementation/1, criterion "
                    "implementation-complete, and artifact_kind implementation-summary. "
                    "Open its summary, then advance."
                ),
            ),
            CoordinatorSegment(
                id="verify",
                phases=("verify",),
                coordinator_profile="verification-coordinator",
                instructions=(
                    "Dispatch exactly one trebuchet_verifier with repo.read, details "
                    "schema trebuchet-verification/1, criterion verification-passed, "
                    "and artifact_kind verification-report. Require the verified "
                    "controller command to pass. Open the report, then request run "
                    "completion."
                ),
                context_artifact_kinds=("implementation-summary",),
                required_artifact_kinds=("implementation-summary",),
            ),
        ),
    )


def with_artifact_kind(task: dict[str, object], kind: str) -> dict[str, object]:
    raw = task.get("context", "")
    try:
        context = json.loads(str(raw)) if raw else {}
    except json.JSONDecodeError:
        context = {"supplied_context": str(raw)}
    if not isinstance(context, dict):
        context = {"supplied_context": context}
    context["artifact_kind"] = kind
    return {**task, "context": json.dumps(context, sort_keys=True)}


def profiles(worktree: Path, evidence: EvidenceCatalog) -> tuple[RoleProfile, ...]:
    builder_instructions = """\
Inspect README.md and verify_simulation.py, then implement index.html, styles.css,
and simulation.js only. Build a polished educational trebuchet lab with a large
responsive canvas and controls for counterweight mass, projectile mass, sling
length, and release angle. Animate counterweight fall, beam rotation, sling,
release, ballistic flight with quadratic drag, impact, and a persistent trajectory.
The numerical state—not a CSS keyframe path—must drive rendering.

Implement and export the exact CommonJS-compatible TrebuchetPhysics API required
by verify_simulation.py. Guard all DOM startup so Node can require simulation.js.
Use a stable small time step and safety bounds. Make simulateShot deterministic.
Increasing counterweight mass must increase release speed; increasing projectile
mass must reduce it. Report release speed, range, flight time, maximum height, and
potential/kinetic energy. Explain that the arm/sling model is an educational
approximation. Include fire, pause/resume, reset, shot history, keyboard focus,
aria-live status, responsive styling, and reduced-motion support. Use no external
assets. Run python3 verify_simulation.py and fix every failure.
"""
    verifier_instructions = """\
Treat controller_verified_command as authoritative. Inspect the implementation
and explain which structural and numerical behaviors it proves. Claim
verification-passed only when the command exit code is zero. The uncommitted
candidate is expected transaction state, not a finding.
"""
    return (
        RoleProfile(
            profile_id="builder",
            role="trebuchet_builder",
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"trebuchet-implementation/1"}),
            backend_id="codex-exec",
            executor_factory=lambda task: CodexSemanticTaskExecutor(
                task=with_artifact_kind(task, "implementation-summary"),
                repository=worktree,
                evidence=evidence,
                role_instructions=builder_instructions,
                executable=CODEX_EXECUTABLE,
                reasoning="medium",
                sandbox="workspace-write",
                require_repository_change=True,
                writable_paths=WRITABLE_PATHS,
                audit=evidence.audit,
            ),
        ),
        RoleProfile(
            profile_id="verifier",
            role="trebuchet_verifier",
            capabilities=frozenset({"repo.read"}),
            details_schemas=frozenset({"trebuchet-verification/1"}),
            backend_id="codex-exec",
            executor_factory=lambda task: CodexSemanticTaskExecutor(
                task=with_artifact_kind(task, "verification-report"),
                repository=worktree,
                evidence=evidence,
                role_instructions=verifier_instructions,
                executable=CODEX_EXECUTABLE,
                reasoning="medium",
                preflight_argv=("python3", "verify_simulation.py"),
                require_preflight_success=True,
                audit=evidence.audit,
            ),
        ),
    )


def review_fix_factory(worktree: Path, evidence: EvidenceCatalog):
    def factory(stage: str, attempt):
        context = json.loads(attempt.context)
        cycle = int(context["cycle"])
        schema_name = context["output_contract"]["details_schema"]
        capabilities = ["repo.read", "repo.write"] if stage == "fix" else ["repo.read"]
        task = {
            "id": f"review-fix-{stage}-c{cycle}",
            "objective": {
                "review": (
                    "Independently attack the candidate's physics, numerical stability, "
                    "animation geometry, teaching claims, interaction, accessibility, "
                    "and responsive behavior."
                ),
                "fix": "Repair only the exact ledger findings in fix_finding_keys.",
                "verify": (
                    "Verify every addressed ledger finding against the candidate and "
                    "the controller-owned deterministic command."
                ),
            }[stage],
            "context": json.dumps(
                {**context, "artifact_kind": f"review-fix-{stage}-report"},
                sort_keys=True,
            ),
            "details_schema": schema_name,
            "acceptance_criteria": [],
            "required_capabilities": capabilities,
        }
        instructions = {
            "review": """\
Inspect the actual candidate and act as an adversarial physics and product reviewer.
Return no finding merely for taste. Every finding needs file, stable subject, score,
fix_cost, and the exact README acceptance clause in protects. Set scope_expanding
when the proposed remedy adds behavior beyond the contract. Set contract_violation
for real correctness, accessibility, or shipped-behavior violations. On cycles after
one, obey the supplied ledger: use new_evidence to reopen a closed item and focus on
what the fix broke. Empty findings means the candidate clears review.
""",
            "fix": """\
Inspect the supplied ledger and fix_finding_keys. Modify only index.html, styles.css,
and simulation.js, and only as needed to resolve those exact findings without feature
growth. Run python3 verify_simulation.py. In details_json return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
""",
            "verify": """\
Treat controller_verified_command as authoritative. Inspect the repaired candidate
and check every supplied fix_finding_key. In details_json return
verified_finding_keys containing every key genuinely covered when the command passes.
Do not edit files.
""",
        }[stage]
        return CodexSemanticTaskExecutor(
            task=task,
            repository=worktree,
            evidence=evidence,
            role_instructions=instructions,
            executable=CODEX_EXECUTABLE,
            reasoning="medium",
            preflight_argv=(
                ("python3", "verify_simulation.py") if stage == "verify" else ()
            ),
            require_preflight_success=stage == "verify",
            sandbox="workspace-write" if stage == "fix" else "read-only",
            require_repository_change=stage == "fix",
            writable_paths=WRITABLE_PATHS if stage == "fix" else (),
            allow_dirty_baseline=stage == "fix",
            audit=evidence.audit,
        )

    return factory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    arguments = parser.parse_args()
    if git(BASE_REPOSITORY, "status", "--porcelain"):
        raise SystemExit("trebuchet repository must be clean before FeatureRun")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = arguments.run_id or f"{stamp}-trebuchet-feature"
    run_dir = ROOT / "logs" / "runs" / run_id
    worktree = WORKTREE_ROOT.with_name(f"{WORKTREE_ROOT.name}-{run_id}")

    def contract_factory(candidate: Path, receipt: dict[str, object]) -> RunContract:
        return RunContract(
            run_id=run_id,
            objective=(
                "Build and verify a polished, physically grounded browser animation "
                "of a counterweight trebuchet firing a projectile."
            ),
            phases=("implement", "verify"),
            criteria=(
                {
                    "id": "implementation-complete",
                    "statement": "The interactive trebuchet simulation is implemented.",
                    "source": "operator",
                },
                {
                    "id": "verification-passed",
                    "statement": "Structural and numerical verification passes.",
                    "source": "operator",
                },
            ),
            terminal_artifact_kinds=("verification-report",),
            repository={
                "path": str(candidate),
                "branch": receipt["feature_branch"],
                "base_branch": receipt["base_branch"],
                "base_commit": receipt["base_commit"],
            },
        )

    def session_factory(
        candidate: Path,
        launch: CoordinatorLaunch,
        evidence: EvidenceCatalog,
    ) -> CodexAppServerSession:
        return CodexAppServerSession(
            reasoning="medium",
            persistent_rollout=True,
            base_instructions=BASE_INSTRUCTIONS + "\n" + launch.instructions,
            audit=evidence.audit,
        )

    evidence_holder: dict[str, object] = {}

    def profile_builder(candidate: Path, evidence: EvidenceCatalog):
        evidence_holder["candidate"] = candidate
        evidence_holder["evidence"] = evidence
        return profiles(candidate, evidence)

    def outer_factory(stage, attempt):
        return review_fix_factory(
            evidence_holder["candidate"],
            evidence_holder["evidence"],
        )(stage, attempt)

    result = run_feature_worktree(
        base_repository=BASE_REPOSITORY,
        base_branch="main",
        feature_branch=f"feature/{run_id}",
        worktree_path=worktree,
        run_dir=run_dir,
        contract_factory=contract_factory,
        schema=schema(),
        session_factory=session_factory,
        profile_builder=profile_builder,
        allowed_paths=WRITABLE_PATHS,
        commit_message="Build realistic trebuchet simulation",
        merge=True,
        review_fix_executor_factory=outer_factory,
        review_fix_policy=ReviewFixPolicy(),
    )
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": result.status,
                "run_dir": str(run_dir),
                "worktree": str(result.worktree_path),
                "review_fix": (
                    result.review_fix.as_dict() if result.review_fix else None
                ),
                "git_receipts": list(result.git_receipts),
                "base_head": git(BASE_REPOSITORY, "rev-parse", "HEAD"),
            },
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
