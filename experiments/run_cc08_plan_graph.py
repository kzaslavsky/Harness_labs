#!/usr/bin/env python3
"""CC-08 in-graph escalation and bounded unsealing PlanGraph runner.

Two nodes from the committed decomposition
docs/development/cc08-escalation-decomposition.json, hand-authored against
docs/development/in-graph-escalation-unsealing-plan.md, which implements
docs/decisions/0007-in-graph-escalation-bounded-unsealing.md.

The plan's [cc08-build-order] describes four build units; this graph registers
two nodes, split on the featurerun/plangraph layer boundary. The plan's claim
that its four units have disjoint path grants is false -- CC-08-1 and CC-08-2
both write review_fix.py, and CC-08-3 and CC-08-4 both write plan_graph.py --
and a reviewer on one finding a defect in the file its predecessor sealed is
exactly the escalation case this feature is being built to handle, which does
not exist yet. CC-08-1 -> CC-08-2 and CC-08-3 -> CC-08-4 survive as the
load-bearing order *inside* CC08-A and CC08-B. See SESSION_HANDOFF_CC08_
ESCALATION_20260819.md section 6.

Adapted from run_convergence_plan_graph.py, which is the proven shape for this
repository. It is a copy rather than an import: the only difference is
campaign constants and instruction prose, and the alternative -- refactoring
that script to take a config object -- would mean editing a file a live
campaign is currently running from.

Stages: prepare / issue / run / resume, with the same contracts and the same
flag spellings as scripts/run_plan_graph.py, so
scripts/plan_graph_autoresume.py can drive this runner unchanged.

Agent mixture (operator-fixed 2026-08-19, matching the convergence campaign so
the two are comparable):
  coordinator   claude:claude-opus-4-8[1m]@medium
  implementers  claude-sonnet-5 (high effort)
  reviewers     claude-opus-5 (high effort)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = Path(os.environ.get("HARNESS_LABS_SOURCE", str(ROOT)))
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.graphrun.agent_mixture import (  # noqa: E402
    WorkerRole,
    build_coordinator_session,
    build_role_profiles,
)
from harness_labs.core.agent_sessions import AgentSession  # noqa: E402
from harness_labs.core.claude_task_executor import (  # noqa: E402
    ClaudeSemanticTaskExecutor,
)
from harness_labs.core.controller_evidence import EvidenceCatalog  # noqa: E402
from harness_labs.core.controller_kernel import RunContract  # noqa: E402
from harness_labs.core.coordinator_dispatcher import CoordinatorLaunch  # noqa: E402
from harness_labs.featurerun.feature_run import (  # noqa: E402
    PlanGraphFeatureRunBinding,
    ReviewFixPolicy,
    run_plan_graph_feature_worktree,
)
from harness_labs.featurerun.feature_run_policy import (  # noqa: E402
    standard_feature_run_dispatch_schema,
)
from harness_labs.plangraph.plan_approval import (  # noqa: E402
    UNCLAIMED_GRANT_WARNING,
    PlanApprovalAdmission,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_refinement import (  # noqa: E402
    refine_repository_decomposition,
)
from harness_labs.plangraph.plan_graph import (  # noqa: E402
    FeatureRunOutcome,
    FeatureRunRequest,
    PlanGraph,
    RepairResumeDirective,
    load_registration,
    persist_registration,
    register_plan_graph,
)

REPO = ROOT
WORKTREE_ROOT = ROOT.parent
CLAUDE = os.environ.get("CC_CLAUDE_EXECUTABLE", "claude")

COORDINATOR_SPEC = "claude:claude-opus-4-8[1m]@medium"
# The coordinator sits silent inside task.dispatch while its worker runs, so
# this must exceed the longest worker runtime (600–900s killed real builds).
COORDINATOR_TIMEOUT_SECONDS = 7200.0
IMPLEMENTER_SPEC = "claude:claude-sonnet-5@high"
IMPLEMENTER_MODEL = "claude-sonnet-5"
REVIEWER_MODEL = "claude-opus-5"

PLAN_PATH = "docs/development/in-graph-escalation-unsealing-plan.md"
DECOMPOSITION_PATH = "docs/development/cc08-escalation-decomposition.json"

LOGICAL_GRAPH_ID = "cc08-escalation"

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files
or run commands. Use only typed controller tools. Follow the segment
instructions, inspect structured worker results, and open material artifacts
before advancing. Never claim work without evidence. Do not delegate beyond
the named workers.
"""

HARD_CONSTRAINTS = f"""\
Hard constraints from the plan ({PLAN_PATH}) and ADR 0007:
- Import by full module path. harness_labs/__init__.py is out of scope; never
  add a re-export there.
- The core layer must never import from the plangraph layer, and
  feature_run.py's static import closure must stay free of plangraph-layer
  modules (tests/test_import_boundaries.py enforces both). review_fix.py may
  only *emit* escalation data; all routing, judging and unsealing lives in
  harness_labs/plangraph/plan_graph.py.
- Do NOT add a retry-budget-ledger/1 event kind. plan_graph_budget.py's _fold
  is a closed chain ending in `else: raise ValueError`; a new kind makes every
  past and future read of that lineage raise. Reuse transfer_ownership ->
  recovery_decision + obligation_transferred, and put escalation-specific
  events on the PlanGraph audit journal.
- Do NOT widen _transfer_targets_for. Its dependents-only BFS is pinned by
  existing tests. _owner_for_paths is a separate, full-plan lookup.
- Escalate AFTER transfer, never before: transfer_scope_expanding keeps first
  claim on a finding with a legitimate downstream owner.
- Bounded means no ingest call. Implement the bound by running no review
  stage at all, not by instructing the reviewer to behave.
- The judge never routes, and never judges its own reviewer's finding. Both
  are hard refusals in code, not prompt guidance.
- Feature off by default: escalation_enabled=False, escalation_judge=None,
  and behaviour byte-identical in that configuration.
- scope_expanding (from required_paths) and anchor_out_of_grant (from the
  single file anchor) are different, and since commit b6fd71e there is a third
  term, fixable_in_grant at review_fix.py:426, which clears
  anchor_out_of_grant when every required path is inside the grant. Read that
  block before touching ingest.
- Every summary and deliverable field must clear the DeliverableFloor: at
  least 4 characters, not a placeholder token, not one token repeated. A
  summary of "test" fails and has already cost a whole graph attempt.
"""

def _operator_note(node_id: str) -> str:
    """Operator-relief guidance folded into a retried node's instructions.

    Written by the human-facing session operator after a blocked or failed
    attempt, anchored in reviewer findings; empty when no note exists. Lives
    under the gitignored approval directory so it never dirties the base.
    """
    path = ROOT / "logs" / "plan-approval" / "operator-notes" / f"{node_id}.md"
    if not path.exists():
        return ""
    return (
        "\nOperator note for this retry (anchored in prior-attempt review "
        "findings; it clarifies the existing acceptance criteria and never "
        "widens your allowed paths):\n" + path.read_text(encoding="utf-8") + "\n"
    )


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def _approval_dir(run_id: str) -> Path:
    return ROOT / "logs" / "plan-approval" / run_id


def _approval_lineage_id(repository_id: str, decomposition_path: str) -> str:
    """Stable approved-graph budget slot, byte-identical to run_plan_graph.py's.

    Approval digests include the plan revision and base commit, so using one
    as the ledger identity would mint a new retry allowance per re-approval.
    """
    identity = {
        "repository_id": repository_id,
        "decomposition_path": decomposition_path,
    }
    return "approval-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# prepare / issue
# ---------------------------------------------------------------------------
def prepare(run_id: str) -> int:
    approval_dir = _approval_dir(run_id)
    approval_dir.mkdir(parents=True, exist_ok=True)
    refinement = refine_repository_decomposition(
        repository=REPO,
        decomposition_path=REPO / DECOMPOSITION_PATH,
    )
    (approval_dir / "refine-report.json").write_text(
        json.dumps(refinement.as_mapping(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    prepared = prepare_approval(
        repository=REPO,
        decomposition_path=REPO / DECOMPOSITION_PATH,
        output_directory=approval_dir,
    )
    warnings = [
        {**dict(warning), "warning_sha256": warning_identity(warning)}
        for warning in prepared.warnings
    ]
    print(
        json.dumps(
            {
                "stage": "prepare",
                "subject": str(prepared.subject_path),
                "gate_evidence": str(prepared.gate_evidence_path),
                "subject_sha256": prepared.subject_sha256,
                "plan_graph_digest": prepared.plan_graph_digest,
                "refinement": {
                    "status": refinement.status,
                    "reason": refinement.reason,
                    "revised": refinement.revised,
                    "advisories": len(refinement.advisories),
                },
                "warnings": warnings,
                "high_severity_warnings": sum(
                    1 for warning in warnings if warning.get("severity") == "high"
                ),
                "unclaimed_grants": {
                    str(warning["runs"][0]): list(warning["paths"])
                    for warning in warnings
                    if warning.get("kind") == UNCLAIMED_GRANT_WARNING
                },
            },
            indent=2,
        )
    )
    print(
        "\nHALTED for operator approval. Write "
        f"{approval_dir / 'operator-approval.json'} by hand "
        "(protocol plan-operator-approval/1, binding subject_sha256 above; "
        "high-severity warnings require matching warning_acknowledgements), "
        f"then run: {sys.argv[0]} issue --run-id {run_id}",
        file=sys.stderr,
    )
    return 0


def issue(run_id: str) -> int:
    approval_dir = _approval_dir(run_id)
    operator_path = approval_dir / "operator-approval.json"
    if not operator_path.exists():
        print(
            f"operator approval missing: {operator_path} — this runner never "
            "authors it; the operator writes it by hand.",
            file=sys.stderr,
        )
        return 1
    receipt = issue_receipt(
        repository=REPO,
        subject_path=approval_dir / "subject.json",
        gate_evidence_path=approval_dir / "gate-evidence.json",
        operator_approval_path=operator_path,
        receipt_path=approval_dir / "receipt.json",
    )
    print(json.dumps({"stage": "issue", "receipt": str(receipt)}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# node launch
# ---------------------------------------------------------------------------
def _implementer_roles(node: FeatureRunRequest) -> tuple[WorkerRole, ...]:
    run = node.run
    sections = ", ".join(run.plan_sections)
    instructions = f"""\
You are implementing CC-08 (in-graph escalation and bounded unsealing)
inside harness_labs. Read,
in this order: {PLAN_PATH} — specifically your node's cited sections
({sections}) — then {DECOMPOSITION_PATH} (your run entry and its
acceptance_criteria text), then the current code in and around your allowed
paths.
You are implementing PlanGraph node {node.plan_node_id}: {run.objective}
Edit only these paths: {', '.join(run.allowed_paths)}.
The controller-owned gate is: {' '.join(run.verification_argv)}
Run it yourself before finishing and repair every failure.
{HARD_CONSTRAINTS}\
{_operator_note(node.plan_node_id)}\
Do not commit. A prior failed attempt may have left uncommitted work in your
allowed paths; inspect and finish or replace it rather than starting blind.
Your structured result is part of the deliverable: summary and
deliverable_markdown must substantively describe what you changed and how
the gate proves it — placeholder text fails the run.
"""
    return (
        WorkerRole(
            profile_id="implementer",
            role="cc08_implementer",
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"cc-implementation/1"}),
            instructions=instructions,
            artifact_kind="implementation-summary",
            sandbox="workspace-write",
            writable_paths=tuple(run.allowed_paths),
            require_repository_change=True,
            allow_dirty_baseline=True,
        ),
    )


def _review_fix_factory(
    node: FeatureRunRequest, worktree: Path, evidence: EvidenceCatalog
):
    writable = tuple(node.run.allowed_paths)

    def factory(stage: str, attempt):
        context = json.loads(attempt.context)
        cycle = int(context["cycle"])
        schema_name = context["output_contract"]["details_schema"]
        capabilities = (
            ["repo.read", "repo.write"] if stage == "fix" else ["repo.read"]
        )
        task = {
            "id": f"review-fix-{stage}-c{cycle}",
            "objective": {
                "review": (
                    "Adversarially review the CC-08 candidate "
                    "for this PlanGraph node against the plan sections it "
                    "cites, its acceptance criteria, and the controller-owned "
                    "gate."
                ),
                "fix": "Repair only the exact ledger findings in fix_finding_keys.",
                "verify": (
                    "Verify every addressed ledger finding against the "
                    "candidate and the controller-owned deterministic command."
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
Inspect the actual candidate for node {node.plan_node_id} as an adversarial
harness reviewer. Priorities: (1) does every criterion in the node's
acceptance_criteria hold, byte-for-byte as written in
{DECOMPOSITION_PATH}, with the gate proving it rather than asserting it?
(2) layering: nothing under harness_labs/core imports harness_labs.plangraph,
no real-browser dependency enters CI, and reused machinery
(plan_graph_autoresume.py, verification_images.py, plan_approval warning
kinds, plan_refinement loop) is delegated to, not reimplemented. (3) journal
and layering: does anything under harness_labs/core/ import the plangraph
layer, or reach feature_run's import closure? (4) does the change respect the
plan's stated refusals -- no new retry-budget event kind, no widening of
_transfer_targets_for, escalation after transfer, no review stage in the
bounded loop, and byte-identical behaviour with the feature off?
Every finding needs file, stable subject, score, fix_cost, and the exact
acceptance clause in protects. Empty findings means the candidate clears.
Your structured result is part of the deliverable: summary and every
deliverable field must substantively describe what you did — at least four
characters, never a placeholder token (test, todo, tbd, n/a, na,
placeholder, wip, fixme, xxx, lorem ipsum), never one token repeated. A
placeholder summary hard-fails the whole run at this cycle.
""",
            "fix": f"""\
Inspect the supplied ledger and fix_finding_keys. Modify only
{', '.join(writable)}, and only as needed to resolve those exact findings
without feature growth. Run {' '.join(node.run.verification_argv)}. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
Your structured result is part of the deliverable: summary and every
deliverable field must substantively describe what you did — at least four
characters, never a placeholder token (test, todo, tbd, n/a, na,
placeholder, wip, fixme, xxx, lorem ipsum), never one token repeated. A
placeholder summary hard-fails the whole run at this cycle.
{_operator_note(node.plan_node_id)}\
""",
            "verify": f"""\
Treat controller_verified_command as authoritative. Inspect the repaired
candidate and check every supplied fix_finding_key. Return
verified_finding_keys containing every key genuinely covered when the command
passes. Do not edit files.
Your structured result is part of the deliverable: summary and every
deliverable field must substantively describe what you did — at least four
characters, never a placeholder token (test, todo, tbd, n/a, na,
placeholder, wip, fixme, xxx, lorem ipsum), never one token repeated. A
placeholder summary hard-fails the whole run at this cycle.
""",
        }[stage]
        return ClaudeSemanticTaskExecutor(
            task=task,
            repository=worktree,
            evidence=evidence,
            role_instructions=instructions,
            model=IMPLEMENTER_MODEL if stage == "fix" else REVIEWER_MODEL,
            effort="high",
            executable=CLAUDE,
            preflight_argv=(
                tuple(node.run.verification_argv) if stage == "verify" else ()
            ),
            require_preflight_success=stage == "verify",
            sandbox="workspace-write" if stage == "fix" else "read-only",
            require_repository_change=stage == "fix",
            writable_paths=writable if stage == "fix" else (),
            audit=evidence.audit,
        )

    return factory


def _verification_repair_factory(
    node: FeatureRunRequest, worktree: Path, evidence: EvidenceCatalog
):
    writable = tuple(node.run.allowed_paths)

    def factory(attempt):
        context = json.loads(attempt.context)
        task = {
            "id": attempt.attempt_id.replace("/", "-"),
            "objective": (
                "Repair the candidate so the controller-owned deterministic "
                "verification command passes."
            ),
            "context": json.dumps(
                {**context, "artifact_kind": "verification-repair-report"},
                sort_keys=True,
            ),
            "details_schema": "cc-verification-repair/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read", "repo.write"],
        }
        instructions = f"""\
The controller ran {' '.join(node.run.verification_argv)} and it failed; the
failure evidence is in your task context. Modify only
{', '.join(writable)} to make that exact command pass without feature
growth.
{HARD_CONSTRAINTS}\
Run the command yourself and confirm exit code zero. Do not commit.
"""
        return ClaudeSemanticTaskExecutor(
            task=task,
            repository=worktree,
            evidence=evidence,
            role_instructions=instructions,
            model=IMPLEMENTER_MODEL,
            effort="high",
            executable=CLAUDE,
            sandbox="workspace-write",
            writable_paths=writable,
            audit=evidence.audit,
        )

    return factory


def _launch_node(
    request: FeatureRunRequest,
    acceptance_criteria: Mapping[str, str],
    base_branch: str,
) -> FeatureRunOutcome:
    run = request.run
    criteria = tuple(
        {
            "id": criterion,
            "statement": acceptance_criteria[criterion],
            "source": "plan",
        }
        for criterion in run.criteria
    )
    binding = PlanGraphFeatureRunBinding(
        plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id,
        objective=run.objective,
        acceptance_criteria=criteria,
        approved_plan={"path": request.plan, "sha256": request.plan_sha256},
        source_binding_report={
            "claims": [
                "The two-node decomposition was re-derived from the plan's "
                "four build units against the measured node-sizing criteria "
                "in docs/development/plangraph-node-sizing-review.md; the "
                "refinement loop reported it clean with zero overlap "
                "warnings and the approval packet reported zero "
                "high-severity warnings and no unclaimed grants."
            ]
        },
        build_briefing={
            "allowed_paths": list(run.allowed_paths),
            "verification_argv": list(run.verification_argv),
            "plan_sections": list(run.plan_sections),
        },
        plan=request.plan,
        plan_base_commit=request.plan_base_commit,
        plan_sha256=request.plan_sha256,
        allowed_paths=tuple(run.allowed_paths),
        verification_argv=tuple(run.verification_argv),
        verification_timeout_seconds=run.verification_timeout_seconds,
    )
    schema = standard_feature_run_dispatch_schema()
    normal_phases = tuple(
        phase for segment in schema.segments for phase in segment.phases
    )

    def contract_factory(candidate: Path, receipt: Mapping[str, object]) -> RunContract:
        return RunContract(
            run_id=request.feature_run_id,
            objective=binding.objective,
            phases=normal_phases,
            criteria=criteria,
            terminal_artifact_kinds=("implementation-summary",),
            repository={
                "path": str(candidate),
                "branch": receipt["feature_branch"],
                "base_branch": receipt["base_branch"],
                "base_commit": receipt["base_commit"],
            },
        )

    def session_factory(
        candidate: Path, launch: CoordinatorLaunch, evidence: EvidenceCatalog
    ) -> AgentSession:
        return build_coordinator_session(
            COORDINATOR_SPEC,
            base_instructions=BASE_INSTRUCTIONS + "\n" + launch.instructions,
            audit=evidence.audit,
            executable=CLAUDE,
            timeout_seconds=COORDINATOR_TIMEOUT_SECONDS,
        )

    holder: dict[str, object] = {}

    def profile_builder(candidate: Path, evidence: EvidenceCatalog):
        holder["candidate"] = candidate
        holder["evidence"] = evidence
        return build_role_profiles(
            mixture={"cc08_implementer": IMPLEMENTER_SPEC},
            roles=_implementer_roles(request),
            repository=candidate,
            evidence=evidence,
            audit=evidence.audit,
            executables={"claude": CLAUDE},
        )

    def review_fix(stage, attempt):
        return _review_fix_factory(
            request, holder["candidate"], holder["evidence"]
        )(stage, attempt)

    def verification_repair(attempt):
        return _verification_repair_factory(
            request, holder["candidate"], holder["evidence"]
        )(attempt)

    result = run_plan_graph_feature_worktree(
        binding=binding,
        schema=schema,
        contract_factory=contract_factory,
        review_fix_policy=ReviewFixPolicy(),
        base_repository=REPO,
        base_branch=base_branch,
        base_commit=request.base_commit,
        candidate_only=True,
        merge=False,
        feature_branch=f"plan-graph/{request.feature_run_id}",
        worktree_path=WORKTREE_ROOT / f"cc08-{request.feature_run_id}",
        run_dir=request.run_dir,
        session_factory=session_factory,
        profile_builder=profile_builder,
        commit_message=f"PlanGraph node {request.plan_node_id}",
        review_fix_executor_factory=review_fix,
        verification_repair_executor_factory=verification_repair,
        verification_repair_limit=3,
    )
    evidence: dict[str, object] | None = None
    if result.verification is not None:
        evidence = {
            "verification": {
                "command_attempts": list(result.verification.command_attempts),
                "repair_invocation_ids": list(
                    result.verification.repair_invocation_ids
                ),
                "repair_invocations": list(result.verification.repair_invocations),
            }
        }
    return FeatureRunOutcome(
        status=result.status,
        candidate_commit=result.candidate_commit,
        evidence=evidence,
        plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id,
        feature_run_id=request.feature_run_id,
        run_dir=str(request.run_dir),
    )


# ---------------------------------------------------------------------------
# run / resume
# ---------------------------------------------------------------------------
def _acceptance_criteria() -> dict[str, str]:
    return dict(
        json.loads((REPO / DECOMPOSITION_PATH).read_text(encoding="utf-8"))[
            "acceptance_criteria"
        ]
    )


def _launcher(base_branch: str):
    acceptance = _acceptance_criteria()
    return lambda request: _launch_node(request, acceptance, base_branch)


def run_graph(receipt_path: Path, graph_attempt_id: str, run_root: Path) -> int:
    admission = PlanApprovalAdmission(repository=REPO, receipt_path=receipt_path)
    approved = admission.validate()
    base_branch = git(REPO, "rev-parse", "--abbrev-ref", "HEAD")
    registration = register_plan_graph(
        repository=REPO,
        logical_graph_id=LOGICAL_GRAPH_ID,
        decomposition=approved.decomposition,
        base_commit=approved.base_commit,
        repository_id=approved.repository_id,
        plan_lineage_id=_approval_lineage_id(
            approved.repository_id, approved.decomposition_path
        ),
        automatic_recovery={
            "protocol": "plan-graph-automatic-recovery/1",
            "allowed_actions": ["resume", "extend_budget"],
            "max_extra_node_launches": 6,
            "max_structural_decisions": 2,
        },
    )
    registration_path = persist_registration(
        repository=REPO,
        registration_root=ROOT / "logs" / "registration",
        registration=registration,
    )
    print(json.dumps({"registration": str(registration_path)}))
    graph = PlanGraph(
        REPO,
        registration,
        _launcher(base_branch),
        run_root=run_root,
        graph_run_id=graph_attempt_id,
        logical_graph_id=LOGICAL_GRAPH_ID,
        approval_validator=admission.approval_validator(),
        # Two nodes with CC08-B depending on CC08-A, so the effective width
        # is 1 regardless; declared as 2 rather than 5 so the ceiling states
        # the graph's real shape.
        max_parallelism=2,
    )
    result = graph.run()
    print(
        json.dumps(
            {
                "stage": "run",
                "status": result.status,
                "candidate_commit": result.candidate_commit,
                "completed": dict(result.completed),
                "failed_run_id": result.failed_run_id,
                "functionality_failure": result.functionality_failure,
                "graph_attempt_id": graph.graph_run_id,
            },
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 1


def resume_graph(
    receipt_path: Path,
    graph_attempt_id: str | None,
    run_root: Path,
    logical_id: str,
    predecessor: str,
    frontier: list[str],
    blocker: str,
) -> int:
    admission = PlanApprovalAdmission(repository=REPO, receipt_path=receipt_path)
    admission.validate()
    base_branch = git(REPO, "rev-parse", "--abbrev-ref", "HEAD")
    registration = load_registration(
        ROOT / "logs" / "registration" / f"{LOGICAL_GRAPH_ID}.json"
    )
    directive = RepairResumeDirective(
        logical_graph_id=logical_id,
        predecessor_attempt_id=predecessor,
        retry_frontier=tuple(frontier),
        blocker_evidence_ref=blocker,
    )
    graph = PlanGraph.resume(
        REPO,
        registration,
        _launcher(base_branch),
        run_root=run_root,
        directive=directive,
        approval_validator=admission.approval_validator(),
        max_parallelism=5,
    )
    result = graph.run()
    print(
        json.dumps(
            {
                "stage": "resume",
                "status": result.status,
                "candidate_commit": result.candidate_commit,
                "completed": dict(result.completed),
                "failed_run_id": result.failed_run_id,
                "functionality_failure": result.functionality_failure,
                "graph_attempt_id": graph.graph_run_id,
            },
            indent=2,
        )
    )
    return (
        0
        if result.status == "succeeded" or result.status.startswith("completed")
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "issue", "run", "resume"))
    parser.add_argument("--run-id", default="cc08-1")
    parser.add_argument("--receipt", type=Path)
    # Resume flags use scripts/run_plan_graph.py's spelling so
    # scripts/plan_graph_autoresume.py can append its directive unchanged.
    parser.add_argument("--graph-attempt-id")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--resume", action="store_true", help="autoresume compat no-op")
    parser.add_argument("--logical-graph-id", default=LOGICAL_GRAPH_ID)
    parser.add_argument("--predecessor-attempt-id")
    parser.add_argument("--retry-frontier", action="append", default=[])
    parser.add_argument("--blocker-evidence-ref")
    arguments = parser.parse_args()
    # PlanGraph IDs must match ^[a-z0-9][a-z0-9-]{0,127}$ — lowercase, no T/Z.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", arguments.run_id):
        parser.error(
            f"--run-id must match ^[a-z0-9][a-z0-9-]+$, got {arguments.run_id!r}"
        )

    if arguments.stage == "prepare":
        return prepare(arguments.run_id)
    if arguments.stage == "issue":
        return issue(arguments.run_id)
    receipt = arguments.receipt
    if receipt is None:
        receipt = _approval_dir(arguments.run_id) / "receipt.json"
    if not receipt.exists():
        parser.error(f"receipt not found: {receipt}")
    run_root = arguments.run_root or (ROOT / "logs" / "runs" / "cc08-graph")
    if arguments.stage == "resume":
        if not (
            arguments.predecessor_attempt_id and arguments.blocker_evidence_ref
        ):
            parser.error(
                "resume requires --predecessor-attempt-id and "
                "--blocker-evidence-ref (plus optional --retry-frontier)"
            )
        return resume_graph(
            receipt,
            arguments.graph_attempt_id,
            run_root,
            arguments.logical_graph_id,
            arguments.predecessor_attempt_id,
            arguments.retry_frontier,
            arguments.blocker_evidence_ref,
        )
    graph_attempt_id = arguments.graph_attempt_id or f"cc08-graph-{arguments.run_id}"
    return run_graph(receipt, graph_attempt_id, run_root)


if __name__ == "__main__":
    raise SystemExit(main())
