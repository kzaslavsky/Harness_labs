#!/usr/bin/env python3
"""Dashboard observability metrics PlanGraph runner.

Seven nodes (DM-01 … DM-07) from the committed decomposition
docs/development/dashboard-observability-metrics-decomposition.json, which
was hand-authored against docs/development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md
and revised after a three-lens (architecture, source-binding, product)
review. Unlike the contract-burden campaigns there is no decompose stage:
the decomposition is the reviewed artifact itself.

Stages:
  approve — deterministic admission gates (prepare_approval) then the
            operator attestation issues the immutable receipt.
  run     — the approved graph executes with max_parallelism=2; each node is
            a PlanGraph-bound FeatureRun with a Fable coordinator (medium),
            Sonnet implementation workers, and Opus reviewers. The graph is
            registered WITH automatic recovery authority (resume,
            extend_budget) and the retry-budget ledger.
  resume  — repair-successor attempt over a retry frontier.

Agent mixture (operator-fixed):
  coordinator   claude:claude-fable-5@medium
  implementers  claude-sonnet-5
  reviewers     claude-opus-5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = Path(os.environ.get("HARNESS_LABS_SOURCE", str(ROOT)))
sys.path.insert(0, str(HARNESS_SOURCE))

from harness_labs.graphrun.agent_mixture import build_coordinator_session  # noqa: E402
from harness_labs.core.agent_sessions import AgentSession  # noqa: E402
from harness_labs.core.claude_task_executor import (  # noqa: E402
    ClaudeSemanticTaskExecutor,
)
from harness_labs.core.controller_evidence import EvidenceCatalog  # noqa: E402
from harness_labs.core.controller_kernel import RunContract  # noqa: E402
from harness_labs.core.controller_scheduler import RoleProfile  # noqa: E402
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
    PlanApprovalAdmission,
    issue_receipt,
    prepare_approval,
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
CLAUDE = os.environ.get("DM_CLAUDE_EXECUTABLE", "claude")

COORDINATOR_SPEC = "claude:claude-fable-5@medium"
IMPLEMENTER_MODEL = "claude-sonnet-5"
REVIEWER_MODEL = "claude-opus-5"

PLAN_PATH = "docs/development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md"
DECOMPOSITION_PATH = (
    "docs/development/dashboard-observability-metrics-decomposition.json"
)

LOGICAL_GRAPH_ID = "dashboard-observability-metrics"

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files
or run commands. Use only typed controller tools. Follow the segment
instructions, inspect structured worker results, and open material artifacts
before advancing. Never claim work without evidence. Do not delegate beyond
the named workers.
"""


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------
def approve(run_id: str) -> Path:
    approval_dir = ROOT / "logs" / "plan-approval" / run_id
    prepared = prepare_approval(
        repository=REPO,
        decomposition_path=REPO / DECOMPOSITION_PATH,
        output_directory=approval_dir,
    )
    operator_path = approval_dir / "operator-approval.json"
    operator_path.write_text(
        json.dumps(
            {
                "protocol": "plan-operator-approval/1",
                "subject_sha256": prepared.subject_sha256,
                "actor": "kirill.zaslavsky@gmail.com",
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "statement": (
                    "Operator-directed dashboard observability metrics "
                    "program: graph-level rollups, cumulative retry "
                    "counters, completion snapshots, completed-graph "
                    "viewer with comparison, historical reconstruction, "
                    "and human-readable naming. Plan and decomposition "
                    "were revised after a three-lens (architecture, "
                    "source-binding, product) adversarial review; the "
                    "review resolution is recorded in the plan document."
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = issue_receipt(
        repository=REPO,
        subject_path=prepared.subject_path,
        gate_evidence_path=prepared.gate_evidence_path,
        operator_approval_path=operator_path,
        receipt_path=approval_dir / "receipt.json",
    )
    print(json.dumps({"stage": "approve", "receipt": str(receipt),
                      "plan_graph_digest": prepared.plan_graph_digest}, indent=2))
    return receipt


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def _implementer_profiles(
    node: FeatureRunRequest, worktree: Path, evidence: EvidenceCatalog
) -> tuple[RoleProfile, ...]:
    instructions = f"""\
You are modifying the harness_labs observability surface (metrics rollup,
run catalog, dashboard server, dashboard SPA, snapshot tooling). Read, in
this order: {PLAN_PATH} (your node's section, the shared Design section, and
the Review resolution section), then the current code in your allowed paths.
You are implementing PlanGraph node {node.plan_node_id}: {node.run.objective}
Edit only these paths: {', '.join(node.run.allowed_paths)}.
The controller-owned gate is: {' '.join(node.run.verification_argv)}
Run it yourself before finishing and repair every failure.
Hard constraints from the plan:
- Follow the tri-state availability convention everywhere: absent data is
  never rendered or summed as zero; degrade with a reason string.
- harness_labs/observability modules must not import harness_labs.plangraph
  (tests.test_import_boundaries enforces this).
- The dashboard server stays GET-only and read-only over journals; snapshot
  and registry writes live only in scripts and are best-effort.
- Graph totals are attempt-scoped; cross-attempt history goes only in the
  labelled lineage_totals block.
- Frontend nodes: rebuild dashboard/plan-graph/dist with npm run build,
  keep shared metric components standalone under src/components/, and keep
  tests/test_dashboard_e2e.py in sync with the DOM you ship.
Do not commit. A prior failed attempt may have left uncommitted work in your
allowed paths; inspect and finish or replace it rather than starting blind.
Your structured result is part of the deliverable: summary and
deliverable_markdown must substantively describe what you changed and how
the gate proves it — placeholder text fails the run.
"""
    return (
        RoleProfile(
            profile_id="implementer",
            role="observability_implementer",
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"dm-implementation/1"}),
            backend_id="claude-print",
            executor_factory=lambda task: ClaudeSemanticTaskExecutor(
                task=_with_artifact_kind(task, "implementation-summary"),
                repository=worktree,
                evidence=evidence,
                role_instructions=instructions,
                model=IMPLEMENTER_MODEL,
                effort="high",
                executable=CLAUDE,
                sandbox="workspace-write",
                require_repository_change=True,
                writable_paths=tuple(node.run.allowed_paths),
                allow_dirty_baseline=True,
                audit=evidence.audit,
            ),
        ),
    )


def _with_artifact_kind(task: Mapping[str, object], kind: str) -> dict[str, object]:
    raw = task.get("context", "")
    try:
        context = json.loads(str(raw)) if raw else {}
    except json.JSONDecodeError:
        context = {"supplied_context": str(raw)}
    if not isinstance(context, dict):
        context = {"supplied_context": context}
    context["artifact_kind"] = kind
    return {**task, "context": json.dumps(context, sort_keys=True)}


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
                    "Adversarially review the dashboard-observability "
                    "candidate for this PlanGraph node against the plan "
                    "section, its acceptance criteria, and the "
                    "controller-owned gate."
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
observability reviewer. Priorities: (1) does any metric ever render absent
data as zero, sum unavailable values, or mislabel an estimate as recorded?
(2) does the dashboard server stay GET-only and read-only over journals,
with the named bounds enforced and no exception path that degrades the
whole catalog? (3) are graph totals attempt-scoped with lineage history
kept separate, and cumulative retry counters labelled? (4) does anything
import harness_labs.plangraph from harness_labs/observability, or violate
the node's acceptance criteria in {PLAN_PATH}? Every finding needs file,
stable subject, score, fix_cost, and the exact acceptance clause in
protects. Empty findings means the candidate clears.
""",
            "fix": f"""\
Inspect the supplied ledger and fix_finding_keys. Modify only
{', '.join(writable)}, and only as needed to resolve those exact findings
without feature growth. Run {' '.join(node.run.verification_argv)}. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
""",
            "verify": f"""\
Treat controller_verified_command as authoritative. Inspect the repaired
candidate and check every supplied fix_finding_key. Return
verified_finding_keys containing every key genuinely covered when the command
passes. Do not edit files.
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
            allow_dirty_baseline=stage == "fix",
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
            "details_schema": "dm-verification-repair/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read", "repo.write"],
        }
        instructions = f"""\
The controller ran {' '.join(node.run.verification_argv)} and it failed; the
failure evidence is in your task context. Modify only
{', '.join(writable)} to make that exact command pass without feature
growth. Respect the plan's hard constraints (tri-state availability, no
harness_labs.plangraph imports from observability, GET-only server,
attempt-scoped totals). Run the command yourself and confirm exit code
zero. Do not commit.
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
            allow_dirty_baseline=True,
            audit=evidence.audit,
        )

    return factory


def _launch_node(
    request: FeatureRunRequest, acceptance_criteria: Mapping[str, str]
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
                "Every symbol, path, and behavioral claim in the plan was "
                "verified against the working tree by a source-binding "
                "review lens before approval; the review resolution is "
                f"recorded in {PLAN_PATH}."
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
            timeout_seconds=600.0,
        )

    holder: dict[str, object] = {}

    def profile_builder(candidate: Path, evidence: EvidenceCatalog):
        holder["candidate"] = candidate
        holder["evidence"] = evidence
        return _implementer_profiles(request, candidate, evidence)

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
        base_branch="dashboard_improve",
        base_commit=request.base_commit,
        candidate_only=True,
        merge=False,
        feature_branch=f"plan-graph/{request.feature_run_id}",
        worktree_path=WORKTREE_ROOT / f"dm-{request.feature_run_id}",
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


def run_graph(run_id: str, receipt_path: Path) -> int:
    admission = PlanApprovalAdmission(repository=REPO, receipt_path=receipt_path)
    approved = admission.validate()
    registration = register_plan_graph(
        repository=REPO,
        logical_graph_id=LOGICAL_GRAPH_ID,
        decomposition=approved.decomposition,
        base_commit=approved.base_commit,
        repository_id=approved.repository_id,
        automatic_recovery={
            "protocol": "plan-graph-automatic-recovery/1",
            "allowed_actions": ["resume", "extend_budget"],
            "max_extra_node_launches": 7,
            "max_structural_decisions": 2,
        },
    )
    registration_path = persist_registration(
        repository=REPO,
        registration_root=ROOT / "logs" / "registration",
        registration=registration,
    )
    print(json.dumps({"registration": str(registration_path)}))
    acceptance = dict(approved.decomposition["acceptance_criteria"])
    graph = PlanGraph(
        REPO,
        registration,
        lambda request: _launch_node(request, acceptance),
        run_root=ROOT / "logs" / "runs" / "dm-graph",
        graph_run_id=f"dm-graph-{run_id}",
        logical_graph_id=LOGICAL_GRAPH_ID,
        approval_validator=admission.approval_validator(),
        # Ready-set execution: DM-01 ∥ DM-02 are file-disjoint roots; then
        # the DM-03 -> DM-04 spine; then DM-05 -> DM-06 serialized on the
        # frontend fence with DM-07 parallel to both.
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
    run_id: str,
    receipt_path: Path,
    predecessor: str,
    frontier: list[str],
    blocker: str,
    logical_id: str,
) -> int:
    admission = PlanApprovalAdmission(repository=REPO, receipt_path=receipt_path)
    admission.validate()
    registration = load_registration(
        ROOT / "logs" / "registration" / f"{LOGICAL_GRAPH_ID}.json"
    )
    directive = RepairResumeDirective(
        logical_graph_id=logical_id,
        predecessor_attempt_id=predecessor,
        retry_frontier=tuple(frontier),
        blocker_evidence_ref=blocker,
    )
    acceptance = dict(
        json.loads(
            (REPO / DECOMPOSITION_PATH).read_text(encoding="utf-8")
        )["acceptance_criteria"]
    )
    graph = PlanGraph.resume(
        REPO,
        registration,
        lambda request: _launch_node(request, acceptance),
        run_root=ROOT / "logs" / "runs" / "dm-graph",
        directive=directive,
        approval_validator=admission.approval_validator(),
        max_parallelism=2,
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
    return 0 if result.status.startswith("completed") or result.status == "succeeded" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("approve", "run", "resume"))
    parser.add_argument("--run-id")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--predecessor", help="prior graph attempt id to resume from")
    parser.add_argument("--frontier", nargs="+", help="node ids to retry")
    parser.add_argument("--blocker", help="artifact:sha256:… blocker evidence ref")
    parser.add_argument(
        "--logical-id", default=LOGICAL_GRAPH_ID,
        help="stable logical graph id recorded by the root attempt",
    )
    arguments = parser.parse_args()
    # PlanGraph IDs must match ^[a-z0-9][a-z0-9-]{0,127}$ — lowercase, no T/Z.
    run_id = arguments.run_id or "dm-1"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", run_id):
        parser.error(f"--run-id must match ^[a-z0-9][a-z0-9-]+$, got {run_id!r}")

    if arguments.stage == "approve":
        approve(run_id)
        return 0
    receipt = arguments.receipt
    if receipt is None:
        parser.error("run/resume stages require --receipt")
    if arguments.stage == "resume":
        if not (arguments.predecessor and arguments.frontier and arguments.blocker):
            parser.error("resume requires --predecessor, --frontier, and --blocker")
        return resume_graph(
            run_id, receipt, arguments.predecessor, arguments.frontier,
            arguments.blocker, arguments.logical_id,
        )
    return run_graph(run_id, receipt)


if __name__ == "__main__":
    raise SystemExit(main())
