#!/usr/bin/env python3
"""Run an audited FeatureRun that designs and builds the Retinology demo."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = Path(os.environ.get("HARNESS_LABS_SOURCE", str(ROOT)))
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.agent_sessions import AgentSession  # noqa: E402
from harness_labs.codex_agent_session import CodexAppServerSession  # noqa: E402
from harness_labs.controller_evidence import EvidenceCatalog  # noqa: E402
from harness_labs.controller_kernel import RunContract  # noqa: E402
from harness_labs.controller_live import CodexSemanticTaskExecutor  # noqa: E402
from harness_labs.controller_scheduler import RoleProfile  # noqa: E402
from harness_labs.coordinator_dispatcher import CoordinatorLaunch  # noqa: E402
from harness_labs.coordinator_schema import (  # noqa: E402
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)
from harness_labs.feature_run import ReviewFixPolicy, run_feature_worktree  # noqa: E402


BASE_REPOSITORY = ROOT / "experiments" / "retinology-demo"
WORKTREE_ROOT = ROOT.parent / "harness_labs_feature_worktrees" / "retinology-demo"
WRITABLE_PATHS = ("index.html", "styles.css", "app.js")
CODEX_EXECUTABLE = os.environ.get(
    "RETINOLOGY_DEMO_CODEX_EXECUTABLE",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)
RETINOLOGY_REPOSITORY = Path(
    os.environ.get(
        "RETINOLOGY_REPOSITORY",
        "/Users/kirillzaslavsky/claudeprojects/Retinology",
    )
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
        schema_id="retinology-demo/1",
        segments=(
            CoordinatorSegment(
                id="design",
                phases=("plan",),
                coordinator_profile="design-coordinator",
                instructions=(
                    "Dispatch exactly one retinology_designer with repo.read, details "
                    "schema retinology-demo-design/1, criterion design-grounded, and "
                    "artifact_kind design-plan. Open the design-plan and advance only "
                    "when it cites inspected Retinology source surfaces and defines a "
                    "complete interaction and visual plan."
                ),
            ),
            CoordinatorSegment(
                id="build",
                phases=("implement",),
                coordinator_profile="build-coordinator",
                instructions=(
                    "Open the supplied design-plan. Dispatch exactly one "
                    "retinology_demo_builder with repo.read and repo.write, details "
                    "schema retinology-demo-implementation/1, criterion "
                    "implementation-complete, and artifact_kind implementation-summary. "
                    "Pass the material design decisions in task context. Open the "
                    "implementation summary, then advance."
                ),
                context_artifact_kinds=("design-plan",),
                required_artifact_kinds=("design-plan",),
            ),
            CoordinatorSegment(
                id="verify",
                phases=("verify",),
                coordinator_profile="verification-coordinator",
                instructions=(
                    "Dispatch exactly one retinology_demo_verifier with repo.read, "
                    "details schema retinology-demo-verification/1, criterion "
                    "verification-passed, and artifact_kind verification-report. "
                    "Require the controller-verified command to pass. Open the report, "
                    "then request run completion. If the verifier reports a genuine "
                    "noncritical issue that this read-only segment cannot repair, "
                    "record it as deferred with evidence so the subsequent independent "
                    "review/fix ledger can adjudicate it; never use accepted, because "
                    "accepted is intentionally an unresolved disposition. Reject only "
                    "a demonstrably false finding."
                ),
                context_artifact_kinds=("design-plan", "implementation-summary"),
                required_artifact_kinds=("design-plan", "implementation-summary"),
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
    designer_instructions = f"""\
Read the operator contract in README.md. Inspect the actual current Retinology
repository at {RETINOLOGY_REPOSITORY}, concentrating on design-system tokens and
components, the L2 shell, Import, Process/flow-editor, Review, and Data surfaces.
Bind every important visual or product claim to a file you actually inspected.
Design a single-page interactive teaching experience that explains Projects →
Import → Process → Review → Data, plus schema import, individual node runs,
chained node runs, pipeline runs, evidence-bound review, and structured output.
Decide the information architecture, visual tokens, realistic fictional sample,
responsive behavior, and interaction states. Be honest about which behaviors are
simulated. Do not edit files. Return a concise implementable design plan.
"""
    builder_instructions = """\
Read README.md and the controller-supplied design context before editing. Inspect
the referenced Retinology styling sources yourself when a visual decision remains
ambiguous. Implement only index.html, styles.css, and app.js.

Build a polished Retinology-styled guided demo, not a marketing landing page and
not a generic dashboard. The primary journey must let an operator move through
Projects, Import, Process, Review, and Data. Include local/mock document intake,
schema import, a visible flow graph, controls that separately demonstrate a
single node run, a chained node run, and a full pipeline run, visible progressive
run states, an evidence-bound low-confidence review decision, and table/JSON data
output. Use realistic fictional retinal-clinic data. Label the whole product as
an interactive simulation and repeat the qualification near actions that could
otherwise imply clinical persistence.

Use semantic HTML and buttons, keyboard-usable controls, an aria-live region,
responsive layouts, focus-visible treatment, and reduced-motion handling. No
external assets or network requests. Keep rendering stable on narrow screens.
Run python3 verify_demo.py and repair all failures. Do not commit.
"""
    verifier_instructions = """\
Treat controller_verified_command as authoritative. Inspect the candidate and
the design-plan evidence. Explain what the deterministic verifier proves and
separately evaluate whether the implementation visibly represents every required
Retinology workflow without overstating simulated behavior. Claim
verification-passed only when the command exit code is zero and the candidate
matches the operator contract. The uncommitted candidate is expected transaction
state, not a finding.
"""
    return (
        RoleProfile(
            profile_id="designer",
            role="retinology_designer",
            capabilities=frozenset({"repo.read"}),
            details_schemas=frozenset({"retinology-demo-design/1"}),
            backend_id="codex-exec",
            executor_factory=lambda task: CodexSemanticTaskExecutor(
                task=with_artifact_kind(task, "design-plan"),
                repository=worktree,
                evidence=evidence,
                role_instructions=designer_instructions,
                executable=CODEX_EXECUTABLE,
                reasoning="high",
                audit=evidence.audit,
            ),
        ),
        RoleProfile(
            profile_id="builder",
            role="retinology_demo_builder",
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"retinology-demo-implementation/1"}),
            backend_id="codex-exec",
            executor_factory=lambda task: CodexSemanticTaskExecutor(
                task=with_artifact_kind(task, "implementation-summary"),
                repository=worktree,
                evidence=evidence,
                role_instructions=builder_instructions,
                executable=CODEX_EXECUTABLE,
                reasoning="high",
                sandbox="workspace-write",
                require_repository_change=True,
                writable_paths=WRITABLE_PATHS,
                audit=evidence.audit,
            ),
        ),
        RoleProfile(
            profile_id="verifier",
            role="retinology_demo_verifier",
            capabilities=frozenset({"repo.read"}),
            details_schemas=frozenset({"retinology-demo-verification/1"}),
            backend_id="codex-exec",
            executor_factory=lambda task: CodexSemanticTaskExecutor(
                task=with_artifact_kind(task, "verification-report"),
                repository=worktree,
                evidence=evidence,
                role_instructions=verifier_instructions,
                executable=CODEX_EXECUTABLE,
                reasoning="medium",
                preflight_argv=("python3", "verify_demo.py"),
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
                    "Adversarially review product truthfulness, Retinology visual "
                    "fidelity, workflow completeness, interaction, accessibility, "
                    "responsive behavior, and code correctness."
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
            "review": f"""\
Inspect the actual candidate and compare it against README.md and the current
Retinology styling sources at {RETINOLOGY_REPOSITORY}. Act as an adversarial
product, interaction, accessibility, and code reviewer. Do not report taste.
Every finding needs file, stable subject, score, fix_cost, the exact acceptance
clause in protects, and concrete evidence. Set scope_expanding when the remedy
adds behavior beyond the contract. Set contract_violation for real correctness,
accessibility, visual-fidelity, or truthfulness violations. On later cycles obey
the ledger and focus on regressions. Empty findings means the candidate clears.
""",
            "fix": """\
Inspect the supplied ledger and fix_finding_keys. Modify only index.html,
styles.css, and app.js, and only as needed to resolve those exact findings
without feature growth. Run python3 verify_demo.py. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
""",
            "verify": """\
Treat controller_verified_command as authoritative. Inspect the repaired
candidate and check every supplied fix_finding_key. Return
verified_finding_keys containing every key genuinely covered when the command
passes. Do not edit files.
""",
        }[stage]
        return CodexSemanticTaskExecutor(
            task=task,
            repository=worktree,
            evidence=evidence,
            role_instructions=instructions,
            executable=CODEX_EXECUTABLE,
            reasoning="high" if stage == "review" else "medium",
            preflight_argv=(("python3", "verify_demo.py") if stage == "verify" else ()),
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
        raise SystemExit("retinology-demo repository must be clean before FeatureRun")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = arguments.run_id or f"{stamp}-retinology-demo"
    run_dir = ROOT / "logs" / "runs" / run_id
    worktree = WORKTREE_ROOT.with_name(f"{WORKTREE_ROOT.name}-{run_id}")

    def contract_factory(candidate: Path, receipt: dict[str, object]) -> RunContract:
        return RunContract(
            run_id=run_id,
            objective=(
                "Design, implement, and verify an interactive Retinology-styled "
                "website that teaches the product's key document-to-data workflow."
            ),
            phases=("plan", "implement", "verify"),
            criteria=(
                {
                    "id": "design-grounded",
                    "statement": "The design is grounded in inspected Retinology sources.",
                    "source": "operator",
                },
                {
                    "id": "implementation-complete",
                    "statement": "The interactive Retinology demo is implemented.",
                    "source": "operator",
                },
                {
                    "id": "verification-passed",
                    "statement": "Structural and independent verification passes.",
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
    ) -> AgentSession:
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
        commit_message="Build interactive Retinology product demo",
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
                "review_fix": result.review_fix.as_dict() if result.review_fix else None,
                "git_receipts": list(result.git_receipts),
                "base_head": git(BASE_REPOSITORY, "rev-parse", "HEAD"),
            },
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
