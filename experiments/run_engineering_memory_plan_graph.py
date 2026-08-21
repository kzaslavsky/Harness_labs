#!/usr/bin/env python3
"""Engineering-memory port PlanGraph runner (EM-A..EM-D2).

Six nodes from the committed decomposition
docs/development/engineering-memory-decomposition.json, authored against
docs/development/engineering-memory-port-plan.md (revised 2026-08-20 after a
three-lens adversarial review: decomposition, source-binding, design/contract).

Graph shape: EM-A (impact core) || EM-B (finding history + public ledger
lineage accessor) || EM-C1 (decision schema/ADR data) -> EM-C2 (decision
registry) -> EM-D1 (admission wiring) -> EM-D2 (refinement + campaign driver
wiring). Grants are pairwise disjoint; effective width 3.

Adapted from run_cc08_plan_graph.py, the proven shape for this repository. A
copy rather than an import, by the same rationale recorded there.

Stages: prepare / issue / run / resume, with the same contracts and the same
flag spellings as scripts/run_plan_graph.py, so
scripts/plan_graph_autoresume.py can drive this runner unchanged.

Agent mixture (operator-fixed 2026-08-19 settings, unchanged):
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
from harness_labs.graphrun.escalation_judge import (  # noqa: E402
    GraphEscalationJudgeSeat,
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
    plan_from_registration,
    register_plan_graph,
)

REPO = ROOT
WORKTREE_ROOT = ROOT.parent
CLAUDE = os.environ.get("CC_CLAUDE_EXECUTABLE", "claude")

COORDINATOR_SPEC = "claude:claude-opus-4-8[1m]@medium"
# The coordinator sits silent inside task.dispatch while its worker runs, so
# this must exceed the longest worker runtime (600–900s killed real builds).
COORDINATOR_TIMEOUT_SECONDS = 7200.0
JUDGE_SPEC = COORDINATOR_SPEC
JUDGE_TIMEOUT_SECONDS = 900.0
IMPLEMENTER_SPEC = "claude:claude-sonnet-5@high"
IMPLEMENTER_MODEL = "claude-sonnet-5"
REVIEWER_MODEL = "claude-opus-5"

PLAN_PATH = "docs/development/engineering-memory-port-plan.md"
DECOMPOSITION_PATH = "docs/development/engineering-memory-decomposition.json"

LOGICAL_GRAPH_ID = "engineering-memory"

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files
or run commands. Use only typed controller tools. Follow the segment
instructions, inspect structured worker results, and open material artifacts
before advancing. Never claim work without evidence. Do not delegate beyond
the named workers.
"""

HARD_CONSTRAINTS = f"""\
Hard constraints from the plan ({PLAN_PATH}):
- Import by full module path. harness_labs/__init__.py is out of scope; never
  add a re-export there.
- The core layer must never import from the plangraph layer
  (tests/test_import_boundaries.py enforces). decision_registry lives in
  core and imports core only; impact_analysis and finding_history live in
  plangraph.
- No new state-ledger record kinds and no ledger writer changes. EM-B adds a
  PUBLIC READ-ONLY per-key lineage accessor plus named record-kind constants
  to convergence_ledger.py; existing journals stay readable unchanged. EM-B
  is the sole owner of convergence_ledger.py in this graph; every other node
  consumes the accessor and must not edit the ledger.
- No new plan payload fields: canonical_plan_graph_payload has a closed
  top-level key set. Gate evidence may gain ONE new optional key, "notices",
  with the validator extended in the same change (EM-D1 only).
- Determinism at issue: every admission-time signal derives ONLY from git
  blobs at base_commit of the repository prepare_approval already receives —
  never the working tree, never gitignored journals. issue_receipt re-derives
  warnings and refuses on drift; do not add prepare/issue parameters.
- gates["warnings"] receives ONLY high-severity actionable entries (the
  campaign driver refuses on any unacknowledged warning of any severity, and
  acknowledgements are only accepted for high ones). Informational output
  (unsupported-language impact, ACTIVE_DECISION_NOTICE) goes to
  gates["notices"], which no acknowledgement gate scans.
- warning_identity hashes kind, severity, runs, and paths; identities of the
  three pre-existing warning kinds must not change.
- The ruling text field on finding_ruled records is `statement` (not
  `reason`); disposition `waive` folds to terminal status `excluded`. Journal
  records carry no timestamps, no campaign id, no repository_id — callers
  declare campaign label and repository_id per journal entry.
- finding_history never writes: the ledger's own open CREATES a missing
  journal, so refuse a non-existent path before constructing a ledger object.
- No third-party dependencies (no jsonschema import; the repo has no
  dependency manifest). Hand-written structural checkers only.
- ADR backfill: Concerns-paths: header lines only, bodies byte-identical.
  NO Supersedes: backfill — no existing ADR supersedes another; the two 0006
  files are distinct active decisions; ADR 0007's amendment is body prose.
- plan_refinement._prepare stays a pure git-independent function of strings;
  the plan-refinement-judgment/1 protocol is unchanged. New inputs on
  refine_decomposition are keyword-with-default.
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
    """Stable approved-graph budget slot, byte-identical to run_plan_graph.py's."""
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
            "authors it; the operator records the approval decision.",
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
You are implementing the engineering-memory port (impact analysis, finding
history, decision lifecycle) inside harness_labs. Read, in this order:
{PLAN_PATH} — specifically your node's cited sections ({sections}), which
include your node's verbatim objective under [em-objectives] and every
acceptance criterion under [em-criteria] — then {DECOMPOSITION_PATH} (your
run entry), then the current code in and around your allowed paths.
You are implementing PlanGraph node {node.plan_node_id}: {run.objective}
Edit only these paths: {', '.join(run.allowed_paths)}.
The controller-owned gate is: {' '.join(run.verification_argv) if run.verification_argv else ' && '.join(' '.join(gate.argv) for gate in run.verification_gates)}
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
            role="em_implementer",
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


def _gate_argv(run) -> tuple[str, ...]:
    if run.verification_argv:
        return tuple(run.verification_argv)
    return tuple(run.verification_gates[0].argv)


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
                    "Adversarially review the engineering-memory candidate "
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
(2) layering: nothing under harness_labs/core imports harness_labs.plangraph;
decision_registry imports core only. (3) contract discipline: no new
state-ledger record kinds, no ledger writer changes, no new plan payload
fields, no new prepare_approval/issue_receipt parameters, gates["warnings"]
high-severity-only with informational output in gates["notices"], existing
warning_identity values unchanged, no third-party imports. (4) does the
change respect the plan's stated refusals — no Supersedes: ADR backfill, ADR
bodies byte-identical, _prepare pure, judgment protocol unchanged,
finding_history write-free (missing journal paths refused before any ledger
object exists)?
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
without feature growth. Run {' '.join(_gate_argv(node.run))}. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
Your structured result is part of the deliverable: summary and every
deliverable field must substantively describe what you did — at least four
characters, never a placeholder token (test, todo, tbd, n/a, na,
placeholder, wip, fixme, xxx, lorem ipsum), never one token repeated. A
placeholder summary hard-fails the whole run at this cycle.
{_operator_note(node.plan_node_id)}\
""",
            "verify": """\
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
                _gate_argv(node.run) if stage == "verify" else ()
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
The controller ran {' '.join(_gate_argv(node.run))} and it failed; the
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
                "The six-node decomposition was revised after a three-lens "
                "adversarial review (decomposition, source-binding, "
                "design/contract; ~50 findings folded in); the repo's own "
                "canonicalize+validate, extract_plan_section, "
                "check_objective_in_plan_text, and "
                "check_criteria_byte_identity all pass, and the conformance "
                "analyzer reports zero block violations, zero sibling "
                "overlaps, and zero unclaimed grants."
            ]
        },
        build_briefing={
            "allowed_paths": list(run.allowed_paths),
            "verification_argv": list(_gate_argv(run)),
            "plan_sections": list(run.plan_sections),
        },
        plan=request.plan,
        plan_base_commit=request.plan_base_commit,
        plan_sha256=request.plan_sha256,
        allowed_paths=tuple(run.allowed_paths),
        verification_argv=tuple(run.verification_argv),
        verification_gates=tuple(run.verification_gates),
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
            mixture={"em_implementer": IMPLEMENTER_SPEC},
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
        # Escalation on: EM-D1/EM-D2 reviewers finding defects in files a
        # predecessor sealed (plan_approval consuming EM-A/B/C modules) is
        # exactly the CC-08 escalation case, and the machinery landed on main
        # 2026-08-20.
        review_fix_policy=ReviewFixPolicy(escalation_enabled=True),
        base_repository=REPO,
        base_branch=base_branch,
        base_commit=request.base_commit,
        candidate_only=True,
        merge=False,
        feature_branch=f"plan-graph/{request.feature_run_id}",
        worktree_path=WORKTREE_ROOT / f"em-{request.feature_run_id}",
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


def _escalation_judge(registration) -> GraphEscalationJudgeSeat:
    """One graph-level judgment seat for the whole attempt (ADR 0007 / CC-08)."""

    return GraphEscalationJudgeSeat(
        JUDGE_SPEC,
        plan=plan_from_registration(registration),
        executable=CLAUDE,
        timeout_seconds=JUDGE_TIMEOUT_SECONDS,
    )


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
    judge = _escalation_judge(registration)
    graph = PlanGraph(
        REPO,
        registration,
        _launcher(base_branch),
        run_root=run_root,
        graph_run_id=graph_attempt_id,
        logical_graph_id=LOGICAL_GRAPH_ID,
        approval_validator=admission.approval_validator(),
        # EM-A || EM-B || EM-C1 is the widest tier, so the ceiling states the
        # graph's real shape.
        max_parallelism=3,
        escalation_judge=judge,
    )
    judge.sealed_nodes = graph.sealed_node_ids
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
    judge = _escalation_judge(registration)
    graph = PlanGraph.resume(
        REPO,
        registration,
        _launcher(base_branch),
        run_root=run_root,
        directive=directive,
        approval_validator=admission.approval_validator(),
        max_parallelism=3,
        escalation_judge=judge,
    )
    judge.sealed_nodes = graph.sealed_node_ids
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
    parser.add_argument("--run-id", default="em-1")
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
    run_root = arguments.run_root or (ROOT / "logs" / "runs" / "em-graph")
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
    graph_attempt_id = arguments.graph_attempt_id or f"em-graph-{arguments.run_id}"
    return run_graph(receipt, graph_attempt_id, run_root)


if __name__ == "__main__":
    raise SystemExit(main())
