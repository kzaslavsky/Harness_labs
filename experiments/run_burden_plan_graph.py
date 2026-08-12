#!/usr/bin/env python3
"""Contract-burden relaxation PlanGraph: the harness fixing its own contracts.

Seven nodes (CB-01 … CB-07), one per still-open finding cluster in
docs/development/contract-burden-reduction.md. Every node's controller-owned
gate is scripts/dev/red_green_check.py: its finding tests must FAIL against the
frozen pre-program harness (RED_BASE) and PASS on the candidate.

Stages:
  decompose — assemble docs/development/contract-burden-decomposition.json from
              the committed plan document (token-free, deterministic).
  approve   — deterministic admission gates (prepare_approval) then the
              operator attestation issues the immutable receipt.
  run       — the approved graph executes serially; each node is a
              PlanGraph-bound FeatureRun with a Fable coordinator (medium),
              Sonnet implementation workers, and Opus reviewers. The graph is
              registered WITH automatic recovery authority (resume,
              extend_budget) and the RB retry-budget ledger — this run is a
              live test of that recovery system.

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

from harness_labs.agent_mixture import build_coordinator_session  # noqa: E402
from harness_labs.agent_sessions import AgentSession  # noqa: E402
from harness_labs.claude_task_executor import (  # noqa: E402
    ClaudeSemanticTaskExecutor,
)
from harness_labs.controller_evidence import EvidenceCatalog  # noqa: E402
from harness_labs.controller_kernel import RunContract  # noqa: E402
from harness_labs.controller_scheduler import RoleProfile  # noqa: E402
from harness_labs.coordinator_dispatcher import CoordinatorLaunch  # noqa: E402
from harness_labs.feature_run import (  # noqa: E402
    PlanGraphFeatureRunBinding,
    ReviewFixPolicy,
    run_plan_graph_feature_worktree,
)
from harness_labs.feature_run_policy import (  # noqa: E402
    standard_feature_run_dispatch_schema,
)
from harness_labs.plan_approval import (  # noqa: E402
    PlanApprovalAdmission,
    issue_receipt,
    prepare_approval,
)
from harness_labs.plan_graph import (  # noqa: E402
    FeatureRunOutcome,
    FeatureRunRequest,
    PlanGraph,
    RepairResumeDirective,
    load_registration,
    persist_registration,
    register_plan_graph,
)
from harness_labs.plan_graph_contract import (  # noqa: E402
    canonical_plan_graph_payload,
)

REPO = ROOT
WORKTREE_ROOT = ROOT.parent
CLAUDE = os.environ.get("CB_CLAUDE_EXECUTABLE", "claude")

COORDINATOR_SPEC = "claude:claude-fable-5@medium"
IMPLEMENTER_MODEL = "claude-sonnet-5"
REVIEWER_MODEL = "claude-opus-5"

PLAN_PATH = "docs/development/CONTRACT_BURDEN_RELAXATION_PLAN.md"
DECOMPOSITION_PATH = "docs/development/contract-burden-decomposition.json"
DIAGNOSIS_PATH = "docs/development/contract-burden-reduction.md"

# The frozen pre-program harness every finding test must fail against.
RED_BASE = "6fb1fd1c748d65c65d06b3a115887e9045f3e5d0"

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files
or run commands. Use only typed controller tools. Follow the segment
instructions, inspect structured worker results, and open material artifacts
before advancing. Never claim work without evidence. Do not delegate beyond
the named workers. When dispatching tasks, acceptance_criteria entries must be
bare criterion ids (for example "AC-CB01-1"), never the full statement text. A
superseding or repair dispatch must keep the original task's
required_capabilities and details schema unchanged. These two rules are gates
in the harness version ORCHESTRATING this run; the run itself is building
their replacements.
"""

# ---------------------------------------------------------------------------
# Node specification. Objectives and criterion statements live in the plan
# document; this table binds structure only.
# ---------------------------------------------------------------------------
# Dependency edges exist only where nodes share owned files or consume another
# node's mechanism; CB-02 is parallel-eligible with the whole program and
# CB-07 with CB-06. Every node's green phase runs the full suite (~40s), so a
# node that breaks an unowned consumer fails its own gate, not the terminal
# graph functionality check.
NODES: tuple[dict[str, Any], ...] = (
    {
        "id": "CB-01",
        "finding_tests": ["tests/test_relax_kernel.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/controller_kernel.py",
            "harness_labs/controller_coordinator.py",
            "tests/test_relax_kernel.py",
            "tests/test_controller_kernel.py",
            "tests/test_controller_coordinator.py",
            "tests/test_feature_run.py",
        ],
        "creates": ["tests/test_relax_kernel.py"],
        "depends_on": [],
    },
    {
        "id": "CB-02",
        "finding_tests": ["tests/test_relax_plan_gates.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/plan_graph.py",
            "tests/test_relax_plan_gates.py",
            "tests/test_plan_graph.py",
        ],
        "creates": ["tests/test_relax_plan_gates.py"],
        "depends_on": [],
    },
    {
        "id": "CB-03",
        "finding_tests": ["tests/test_relax_verification_classes.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/feature_run.py",
            "tests/test_relax_verification_classes.py",
            "tests/test_feature_run.py",
        ],
        "creates": ["tests/test_relax_verification_classes.py"],
        "depends_on": ["CB-01"],
    },
    {
        "id": "CB-04",
        "finding_tests": ["tests/test_relax_delta_repair.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/feature_run.py",
            "harness_labs/plan_graph_budget.py",
            "tests/test_relax_delta_repair.py",
            "tests/test_feature_run.py",
            "tests/test_plan_graph_budget.py",
        ],
        "creates": ["tests/test_relax_delta_repair.py"],
        "depends_on": ["CB-03"],
    },
    {
        "id": "CB-05",
        "finding_tests": ["tests/test_relax_adoption.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/claude_task_executor.py",
            "harness_labs/controller_live.py",
            "harness_labs/feature_run.py",
            "harness_labs/agent_mixture.py",
            "tests/test_relax_adoption.py",
            "tests/test_claude_task_executor.py",
            "tests/test_controller_live.py",
            "tests/test_agent_mixture.py",
            "tests/test_feature_run.py",
        ],
        "creates": ["tests/test_relax_adoption.py"],
        "depends_on": ["CB-04"],
    },
    {
        "id": "CB-06",
        "finding_tests": ["tests/test_relax_gate_criteria.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/controller_kernel.py",
            "harness_labs/feature_run.py",
            "tests/test_relax_gate_criteria.py",
            "tests/test_controller_kernel.py",
            "tests/test_feature_run.py",
        ],
        "creates": ["tests/test_relax_gate_criteria.py"],
        "depends_on": ["CB-01", "CB-05"],
    },
    {
        "id": "CB-07",
        "finding_tests": ["tests/test_relax_semantic_floor.py"],
        "regression": ["tests/"],
        "allowed_paths": [
            "harness_labs/claude_task_executor.py",
            "harness_labs/controller_live.py",
            "harness_labs/controller_results.py",
            "tests/test_relax_semantic_floor.py",
            "tests/test_claude_task_executor.py",
            "tests/test_controller_live.py",
        ],
        "creates": ["tests/test_relax_semantic_floor.py"],
        "depends_on": ["CB-05"],
    },
    {
        "id": "CB-08",
        "finding_tests": [],
        "regression": [],
        "allowed_paths": [
            "experiments/run_burden_plan_graph.py",
            "docs/development/contract-burden-reduction.md",
        ],
        "creates": [],
        "depends_on": ["CB-02", "CB-03", "CB-06", "CB-07"],
        "verification_argv": [
            "python3", "scripts/dev/check_workaround_retirement.py",
        ],
        "verification_required_paths": [
            {"path": "scripts/dev/check_workaround_retirement.py",
             "availability": "base"},
        ],
    },
)


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# decompose
# ---------------------------------------------------------------------------
def parse_plan_document() -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Return (sections, objectives, criteria statements, node->criteria ids)."""
    text = (REPO / PLAN_PATH).read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for match in re.finditer(
        r"^## (CB-\d\d) — .*?$(.*?)(?=^## |\Z)", text, re.M | re.S
    ):
        sections[match.group(1)] = match.group(0).strip()
    objectives: dict[str, str] = {}
    criteria: dict[str, str] = {}
    node_criteria: dict[str, list[str]] = {}
    for node_id, body in sections.items():
        objective = re.search(r"^Objective: (.+)$", body, re.M)
        if objective is None:
            raise RuntimeError(f"{node_id}: no objective line in plan document")
        objectives[node_id] = objective.group(1).strip()
        ids: list[str] = []
        for ac in re.finditer(r"^- (AC-CB\d\d-\d+): (.+)$", body, re.M):
            criteria[ac.group(1)] = ac.group(2).strip()
            ids.append(ac.group(1))
        if not ids:
            raise RuntimeError(f"{node_id}: no acceptance criteria in plan document")
        node_criteria[node_id] = ids
    missing = {spec["id"] for spec in NODES} - set(sections)
    if missing:
        raise RuntimeError(f"plan document lacks sections for {sorted(missing)}")
    return sections, objectives, criteria, node_criteria


def assemble_decomposition() -> dict[str, Any]:
    sections, objectives, criteria, node_criteria = parse_plan_document()
    runs: list[dict[str, Any]] = []
    for spec in NODES:
        node_id = spec["id"]
        argv = spec.get("verification_argv") or [
            "python3", "scripts/dev/red_green_check.py",
            "--base", RED_BASE,
            "--finding-tests", *spec["finding_tests"],
            "--regression", *spec["regression"],
            "--timeout", "700",
        ]
        required = spec.get("verification_required_paths") or [
            {"path": "scripts/dev/red_green_check.py", "availability": "base"},
        ]
        runs.append(
            {
                "id": node_id,
                "objective": objectives[node_id],
                "plan_sections": [node_id],
                "criteria": list(node_criteria[node_id]),
                "depends_on": list(spec["depends_on"]),
                "allowed_paths": list(spec["allowed_paths"]),
                "path_intents": [
                    {
                        "path": path,
                        "action": "create" if path in spec["creates"] else "modify",
                    }
                    for path in spec["allowed_paths"]
                ],
                "verification_argv": argv,
                "verification_timeout_seconds": 1800,
                "verification_required_paths": required,
            }
        )
    # Registration (currently) requires each run's objective, criterion ids,
    # and statements verbatim in its cited sections; CB-02 removes that gate,
    # but this decomposition must pass the harness that registers it.
    plan_sections = dict(sections)
    for run in runs:
        cited = "\n".join(plan_sections[key] for key in run["plan_sections"])
        additions = []
        if run["objective"] not in cited:
            additions.append(f"Objective: {run['objective']}")
        for criterion in run["criteria"]:
            statement = criteria[criterion]
            if criterion not in cited or statement not in cited:
                additions.append(f"{criterion}: {statement}")
        if additions:
            key = run["plan_sections"][0]
            plan_sections[key] = plan_sections[key] + "\n" + "\n".join(additions)
    decomposition = {
        "protocol": "plan-graph-plan/1",
        "plan": PLAN_PATH,
        "plan_sections": plan_sections,
        "acceptance_criteria": criteria,
        "runs": runs,
        "functionality_tests": [
            {
                "argv": ["python3", "-m", "pytest", "tests/", "-q"],
                "timeout_seconds": 3600,
                "required_paths": [
                    {"path": "tests/__init__.py", "availability": "base"}
                ],
            }
        ],
        "referenced_artifacts": [
            DIAGNOSIS_PATH,
            "scripts/dev/red_green_check.py",
            "scripts/dev/check_workaround_retirement.py",
            "docs/development/plan-review-cb/frame.json",
            "docs/development/plan-review-cb/necessity.json",
            "docs/development/plan-review-cb/mechanism.json",
            "docs/development/plan-review-cb/adjudication.md",
        ],
    }
    canonical_plan_graph_payload(decomposition)
    return decomposition


def decompose() -> None:
    decomposition = assemble_decomposition()
    output = REPO / DECOMPOSITION_PATH
    output.write_text(
        json.dumps(decomposition, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"stage": "decompose", "written": str(output),
                      "runs": [run["id"] for run in decomposition["runs"]]}))


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
                    "Operator-directed contract-burden relaxation program. "
                    "Every node is bound to run evidence in the committed "
                    "diagnosis document and carries a red/green gate proving "
                    "the finding against the frozen base harness."
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
You are modifying the harness_labs harness itself. Read, in this order:
{PLAN_PATH} (your node's section), {DIAGNOSIS_PATH} (the evidence your node is
bound to), and the current code in your allowed paths. You are implementing
PlanGraph node {node.plan_node_id}: {node.run.objective}
Edit only these paths: {', '.join(node.run.allowed_paths)}.
The controller-owned gate is: {' '.join(node.run.verification_argv)}
Run it yourself before finishing and repair every failure. Your finding tests
must fail on the frozen base harness because of BEHAVIOR, not ImportError —
exercise entry points that already exist at the base commit. Do not commit.
Do not weaken anything on the diagnosis keep-list (receipt binding, write-grant
enforcement, controller-owned verification, hash-chained journals). A prior
failed attempt may have left uncommitted work in your allowed paths; inspect
and finish or replace it rather than starting blind. Your structured result is
part of the deliverable: summary and deliverable_markdown must substantively
describe what you changed and how the red/green gate proves it — placeholder
text fails the run.
"""
    return (
        RoleProfile(
            profile_id="implementer",
            role="harness_implementer",
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"cb-implementation/1"}),
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
                    "Adversarially review the harness candidate for this "
                    "PlanGraph node against the plan section, the diagnosis "
                    "evidence, and the red/green gate."
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
harness reviewer. Priorities: (1) does any change weaken the diagnosis
keep-list (receipt binding, write-grant enforcement, controller-owned
verification, hash-chained journals)? (2) do the finding tests fail on base
for behavioral reasons rather than ImportError? (3) is the relaxation minimal
— no new authority, no scope growth? Every finding needs file, stable subject,
score, fix_cost, and the exact acceptance clause in protects. Empty findings
means the candidate clears.
""",
            "fix": f"""\
Inspect the supplied ledger and fix_finding_keys. Modify only
{', '.join(writable)}, and only as needed to resolve those exact findings
without feature growth. Run {' '.join(node.run.verification_argv)}. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
Your structured claims must reference ONLY the supplied ledger finding keys.
Never place acceptance-criterion ids (like "AC-CB03-1") or annotated criterion
text anywhere in claims: this task has no assigned criteria and the
orchestrating harness rejects any claim naming an unassigned criterion,
failing the whole node.
""",
            "verify": """\
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
                "Repair the candidate so the deterministic red/green gate "
                "passes."
            ),
            "context": json.dumps(
                {**context, "artifact_kind": "verification-repair-report"},
                sort_keys=True,
            ),
            "details_schema": "cb-verification-repair/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read", "repo.write"],
        }
        instructions = f"""\
The controller ran {' '.join(node.run.verification_argv)} and it failed; the
failure evidence is in your task context. Read the red/green verdict JSON in
that evidence: "red-phase-passed-on-base" means your finding tests do not
actually demonstrate the old-harness failure; "green-phase-failed" means the
candidate does not satisfy them or broke a regression target. Modify only
{', '.join(writable)} to make that exact command pass without feature growth.
Run it yourself and confirm exit code zero. Do not commit.
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
            "source": "operator",
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
                "Every node is bound to run evidence in "
                f"{DIAGNOSIS_PATH} at the plan base commit; the red/green "
                "gate proves each finding against the frozen base harness "
                f"{RED_BASE}."
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
        base_branch="contract-burden-relaxation",
        base_commit=request.base_commit,
        candidate_only=True,
        merge=False,
        feature_branch=f"plan-graph/{request.feature_run_id}",
        worktree_path=WORKTREE_ROOT / f"cb-{request.feature_run_id}",
        run_dir=request.run_dir,
        session_factory=session_factory,
        profile_builder=profile_builder,
        commit_message=f"PlanGraph node {request.plan_node_id}",
        review_fix_executor_factory=review_fix,
        verification_repair_executor_factory=verification_repair,
        verification_repair_limit=3,
    )
    # Feed the node's structured verification facts into the RB ledger; without
    # this the retry-budget accounting never sees gate or repair evidence.
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
        logical_graph_id="contract-burden-relaxation",
        decomposition=approved.decomposition,
        base_commit=approved.base_commit,
        repository_id=approved.repository_id,
        automatic_recovery={
            "protocol": "plan-graph-automatic-recovery/1",
            "allowed_actions": ["resume", "extend_budget"],
            "max_extra_node_launches": 6,
            "max_structural_decisions": 2,
        },
    )
    # Persist the registration so scripts/plan_graph_recover.py can act on a
    # blocked graph, and key the run root to the LINEAGE (not the attempt) so
    # the RB ledger carries budget state across attempts.
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
        run_root=ROOT / "logs" / "runs" / "cb-graph",
        graph_run_id=f"cb-graph-{run_id}",
        approval_validator=admission.approval_validator(),
        # Ready-set execution: the CB DAG is width 2 (CB-01 ∥ CB-02 at the
        # roots, CB-06 ∥ CB-07 after CB-05). Each node still owns its own
        # worktree and run_dir; joins are controller-owned merge commits.
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
) -> int:
    admission = PlanApprovalAdmission(repository=REPO, receipt_path=receipt_path)
    admission.validate()
    registration = load_registration(
        ROOT / "logs" / "registration" / "contract-burden-relaxation.json"
    )
    directive = RepairResumeDirective(
        logical_graph_id="contract-burden-relaxation",
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
        run_root=ROOT / "logs" / "runs" / "cb-graph",
        directive=directive,
        graph_run_id=f"cb-graph-{run_id}",
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
    parser.add_argument("stage", choices=("decompose", "approve", "run", "resume"))
    parser.add_argument("--run-id")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--predecessor", help="prior graph attempt id to resume from")
    parser.add_argument("--frontier", nargs="+", help="node ids to retry")
    parser.add_argument("--blocker", help="artifact:sha256:… blocker evidence ref")
    arguments = parser.parse_args()
    # PlanGraph IDs must match ^[a-z0-9][a-z0-9-]{0,127}$ — lowercase, no T/Z.
    run_id = arguments.run_id or datetime.now(timezone.utc).strftime(
        "%Y%m%d-%H%M%S"
    )
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", run_id):
        parser.error(f"--run-id must match ^[a-z0-9][a-z0-9-]+$, got {run_id!r}")

    if arguments.stage == "decompose":
        decompose()
        return 0
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
            arguments.blocker,
        )
    return run_graph(run_id, receipt)


if __name__ == "__main__":
    raise SystemExit(main())
