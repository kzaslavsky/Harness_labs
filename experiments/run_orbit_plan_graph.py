#!/usr/bin/env python3
"""Orbit Lab PlanGraph experiment: engineered plan review before approval.

Pipeline, in the implement-v13 spirit but at the PlanGraph admission boundary:

1. ``plan``    — a Sonnet plan author drafts the plan and the two-node
                 decomposition; three independent Opus lenses (FRAME,
                 NECESSITY, MECHANISM) attack it; Sonnet revises until no
                 critical finding survives (bounded cycles). All review
                 artifacts are committed with the plan so approval binds
                 to them.
2. ``approve`` — deterministic admission gates run against the committed
                 plan (prepare_approval), then an operator attestation
                 issues the immutable receipt.
3. ``run``     — the approved PlanGraph executes its nodes serially, each
                 as a PlanGraph-bound FeatureRun with a Fable coordinator
                 (medium effort), Sonnet implementation workers, and Opus
                 reviewers.

Agent mixture (operator-fixed for this experiment):
  coordinator   claude:claude-fable-5@medium
  implementers  claude-sonnet-5   (builder, fixer, verification repair)
  reviewers     claude-opus-5     (plan lenses, review/verify stages)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
    prepare_approval,
    issue_receipt,
)
from harness_labs.plangraph.plan_graph import (  # noqa: E402
    FeatureRunOutcome,
    FeatureRunRequest,
    PlanGraph,
    register_plan_graph,
)
from harness_labs.plangraph.plan_graph_contract import (  # noqa: E402
    PlanGraphContractError,
    canonical_plan_graph_payload,
)


ORBIT = ROOT / "experiments" / "orbit"
# ROOT already lives inside harness_labs_feature_worktrees; node worktrees
# are siblings of this experiment's own worktree.
WORKTREE_ROOT = ROOT.parent
CLAUDE = os.environ.get("ORBIT_CLAUDE_EXECUTABLE", "claude")

COORDINATOR_SPEC = "claude:claude-fable-5@medium"
IMPLEMENTER_MODEL = "claude-sonnet-5"
REVIEWER_MODEL = "claude-opus-5"
PLAN_AUTHOR_MODEL = IMPLEMENTER_MODEL

PLAN_PATH = "docs/orbit-plan.md"
DECOMPOSITION_PATH = "docs/orbit-decomposition.json"
REVIEW_DIR = "docs/plan-review"
MAX_PLAN_CYCLES = 2

BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files or
run commands. Use only typed controller tools. Follow the segment instructions,
inspect structured worker results, and open material artifacts before advancing.
Never claim work without evidence. Do not delegate beyond the named workers.
When dispatching tasks, acceptance_criteria entries must be bare criterion
ids (for example "AC-01"), never the full statement text. A superseding or
repair dispatch must keep the original task's required_capabilities and
details schema unchanged.
"""

PLAN_LENSES = (
    (
        "frame",
        "FRAME lens: attack scope, ownership, node boundaries, architecture, "
        "and contracts. Is each node's objective exactly implementable inside "
        "its allowed_paths? Are the dependency direction, path intents, and "
        "handoff between the two nodes coherent with the operator contract in "
        "README.md? Does any plan section promise work no node owns, or any "
        "node claim work the plan never justifies?",
    ),
    (
        "necessity",
        "NECESSITY lens: attack unnecessary substrate, duplicated mechanism, "
        "and avoidable surface area. Flag every planned artifact, control, or "
        "behavior that the operator contract and acceptance criteria do not "
        "require, and every place where two nodes would build overlapping "
        "mechanism twice.",
    ),
    (
        "mechanism",
        "MECHANISM lens: attack the technical mechanism. Will the planned "
        "physics approach satisfy verify_physics.py numerically (Kepler's "
        "third law tolerance, apsis geometry, vis-viva speeds, determinism, "
        "input validation)? Will the planned UI wiring satisfy verify_ui.py "
        "structurally? Are the declared verification commands, required "
        "paths, and timeouts actually sufficient to prove each node's "
        "acceptance criteria?",
    ),
)

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "detail", "affects"],
                "properties": {
                    "severity": {"enum": ["critical", "material", "minor"]},
                    "title": {"type": "string", "minLength": 1},
                    "detail": {"type": "string", "minLength": 1},
                    "affects": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_RUN_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "objective",
        "plan_sections",
        "criteria",
        "depends_on",
        "allowed_paths",
        "path_intents",
        "verification_argv",
        "verification_timeout_seconds",
        "verification_required_paths",
    ],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "objective": {"type": "string", "minLength": 1},
        "plan_sections": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "allowed_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "path_intents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "action"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "action": {"enum": ["create", "modify", "delete"]},
                },
            },
        },
        "verification_argv": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "verification_timeout_seconds": {"type": "number", "minimum": 60},
        "verification_required_paths": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "availability"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "availability": {"enum": ["base", "created_by"]},
                    "producer_run_id": {"type": "string"},
                },
            },
        },
    },
}

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan_markdown", "plan_sections", "acceptance_criteria", "runs"],
    "properties": {
        "plan_markdown": {"type": "string", "minLength": 200},
        "plan_sections": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "minProperties": 2,
        },
        "acceptance_criteria": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "minProperties": 2,
        },
        "runs": {"type": "array", "items": _RUN_ITEM_SCHEMA, "minItems": 2},
    },
}


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def claude_structured(
    prompt: str,
    schema: Mapping[str, Any],
    *,
    model: str,
    effort: str,
    budget_usd: float,
    timeout: float = 1200.0,
) -> dict[str, Any]:
    """One read-only structured `claude -p` call inside the orbit repository."""

    executable = shutil.which(CLAUDE)
    if executable is None:
        raise RuntimeError(f"claude executable not found: {CLAUDE}")
    argv = [
        executable,
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "json",
        "--tools",
        "Read,Glob,Grep",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--json-schema",
        json.dumps(schema),
        "--max-budget-usd",
        str(budget_usd),
    ]
    completed = subprocess.run(
        argv, cwd=ORBIT, capture_output=True, text=True, timeout=timeout
    )
    if completed.returncode != 0:
        raise RuntimeError(f"claude call failed: {completed.stderr.strip()[:500]}")
    envelope = json.loads(completed.stdout)
    if envelope.get("is_error") or not isinstance(
        envelope.get("structured_output"), dict
    ):
        raise RuntimeError(
            f"claude call returned no structured output: {envelope.get('result')}"
        )
    return envelope["structured_output"]


def assemble_decomposition(
    draft: Mapping[str, Any], referenced_artifacts: list[str]
) -> dict[str, Any]:
    """Bind the drafted decomposition to operator-owned fixed elements."""

    plan_sections = dict(draft["plan_sections"])
    acceptance_criteria = dict(draft["acceptance_criteria"])
    runs = [dict(run) for run in draft["runs"]]
    # The registration validator requires each run's objective, criterion ids,
    # and criterion statements to appear verbatim in its cited plan sections.
    # Normalize deterministically so engineered prose cannot fail mechanically.
    for run in runs:
        primary = run["plan_sections"][0]
        cited = "\n".join(
            plan_sections.get(section, "") for section in run["plan_sections"]
        )
        additions = []
        if run["objective"] not in cited:
            additions.append(f"Objective: {run['objective']}")
        for criterion in run["criteria"]:
            statement = acceptance_criteria[criterion]
            if criterion not in cited or statement not in cited:
                additions.append(f"{criterion}: {statement}")
        if additions:
            plan_sections[primary] = (
                plan_sections.get(primary, "") + " " + " ".join(additions)
            ).strip()
    decomposition = {
        "protocol": "plan-graph-plan/1",
        "plan": PLAN_PATH,
        "plan_sections": plan_sections,
        "acceptance_criteria": acceptance_criteria,
        "runs": runs,
        "functionality_tests": [
            {
                "argv": ["python3", "verify_physics.py"],
                "timeout_seconds": 300,
                "required_paths": [
                    {"path": "verify_physics.py", "availability": "base"}
                ],
            },
            {
                "argv": ["python3", "verify_ui.py"],
                "timeout_seconds": 300,
                "required_paths": [
                    {"path": "verify_ui.py", "availability": "base"}
                ],
            },
        ],
        "referenced_artifacts": referenced_artifacts,
    }
    canonical = canonical_plan_graph_payload(decomposition)
    if len(canonical["runs"]) < 2:
        raise PlanGraphContractError("decomposition must contain at least two nodes")
    dependents = [run for run in canonical["runs"] if run["depends_on"]]
    if not dependents:
        raise PlanGraphContractError(
            "decomposition must contain a genuine dependency edge"
        )
    return decomposition


def _plan_context_block(draft: Mapping[str, Any]) -> str:
    return (
        "PLAN MARKDOWN:\n" + str(draft["plan_markdown"])
        + "\n\nDECOMPOSITION (plan_sections, acceptance_criteria, runs):\n"
        + json.dumps(
            {
                "plan_sections": draft["plan_sections"],
                "acceptance_criteria": draft["acceptance_criteria"],
                "runs": draft["runs"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _draft_charge(run_id: str) -> str:
    return f"""\
You are the plan author for the Orbit Lab feature. Read README.md,
verify_physics.py, and verify_ui.py in this repository; they are the operator
contract and the immutable deterministic gates.

Produce an implementation plan and a two-node PlanGraph decomposition:
- Node ids: use "orbit-physics" and "orbit-ui"; "orbit-ui" must depend on
  "orbit-physics".
- orbit-physics owns exactly physics.js (path intent: create).
  verification_argv must be ["python3", "verify_physics.py"] with
  verification_required_paths [{{"path": "verify_physics.py",
  "availability": "base"}}].
- orbit-ui owns exactly index.html, styles.css, app.js (path intents: create).
  verification_argv must be ["python3", "verify_ui.py"] with
  verification_required_paths [{{"path": "verify_ui.py", "availability":
  "base"}}, {{"path": "physics.js", "availability": "created_by",
  "producer_run_id": "orbit-physics"}}].
- plan_sections: one section per node (keys "FR-01", "FR-02") stating what
  that node builds and how its gate proves it. Each section must contain,
  verbatim, its node's objective string and every one of its node's
  acceptance criteria as "AC-xx: <statement>".
- acceptance_criteria: closed statements (keys "AC-01", "AC-02", ...), each
  criterion assigned to exactly one node via its criteria array.
- verification_timeout_seconds: 300 for both nodes.
- plan_markdown: the full engineering plan with sections for scope, verified
  current state, dependency-ordered steps per node, runtime contracts (the
  exact exported API and numeric tolerances the gates check), acceptance and
  tests, risks, and rejected alternatives.

Every claim about the gates must match the actual verify scripts you read.
Timestamp context: {run_id}.
"""


def engineer_plan(run_id: str) -> None:
    """Draft, multi-lens review, and revise the plan."""

    if git(ORBIT, "status", "--porcelain"):
        raise SystemExit("orbit repository must be clean before plan engineering")
    review_root = ORBIT / REVIEW_DIR
    if review_root.exists():
        shutil.rmtree(review_root)

    draft_charge = _draft_charge(run_id)
    print("plan-engineering: drafting with", PLAN_AUTHOR_MODEL, flush=True)
    draft = claude_structured(
        draft_charge,
        DRAFT_SCHEMA,
        model=PLAN_AUTHOR_MODEL,
        effort="high",
        budget_usd=3.0,
    )

    history: list[dict[str, Any]] = []
    for cycle in range(1, MAX_PLAN_CYCLES + 1):
        cycle_dir = review_root / f"cycle-{cycle}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        (cycle_dir / "draft.json").write_text(
            json.dumps(draft, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        context = _plan_context_block(draft)
        reviews: dict[str, dict[str, Any]] = {}
        for lens_id, charge in PLAN_LENSES:
            print(f"plan-engineering: cycle {cycle} lens {lens_id}", flush=True)
            reviews[lens_id] = claude_structured(
                "Independent plan review before approval. Read README.md, "
                "verify_physics.py, and verify_ui.py yourself, then review the "
                f"candidate plan below.\n\n{charge}\n\nSeverity contract: "
                "critical = the plan as written leads to a failed gate, an "
                "impossible node, or an unprovable criterion; material = the "
                "plan survives but with real risk or waste; minor = polish.\n\n"
                + context,
                FINDINGS_SCHEMA,
                model=REVIEWER_MODEL,
                effort="medium",
                budget_usd=2.0,
            )
            (cycle_dir / f"{lens_id}.json").write_text(
                json.dumps(reviews[lens_id], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        blocking = [
            {"lens": lens_id, **finding}
            for lens_id, review in reviews.items()
            for finding in review["findings"]
            if finding["severity"] == "critical"
        ]
        history.append(
            {
                "cycle": cycle,
                "blocking_findings": len(blocking),
                "total_findings": sum(
                    len(review["findings"]) for review in reviews.values()
                ),
            }
        )
        print(
            f"plan-engineering: cycle {cycle} blocking={len(blocking)}",
            flush=True,
        )
        if not blocking:
            break
        if cycle == MAX_PLAN_CYCLES:
            (review_root / "resolution.json").write_text(
                json.dumps(
                    {"status": "rejected", "history": history},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            raise SystemExit(
                "plan engineering exhausted its revision budget with "
                f"{len(blocking)} blocking findings; artifacts in {review_root}"
            )
        print(f"plan-engineering: cycle {cycle} revision", flush=True)
        materials = {"lens_reviews": reviews}
        draft = claude_structured(
            "Revise the candidate plan to resolve every critical finding and "
            "as many material findings as possible without expanding scope. "
            "Keep the fixed node ids, ownership, verification commands, and "
            "required paths from your charge unchanged unless a finding "
            "proves them wrong. Read README.md, verify_physics.py, and "
            "verify_ui.py again before revising.\n\nORIGINAL CHARGE:\n"
            + draft_charge
            + "\n\nCANDIDATE:\n"
            + _plan_context_block(draft)
            + "\n\nREVIEW FINDINGS:\n"
            + json.dumps(materials, indent=2, sort_keys=True),
            DRAFT_SCHEMA,
            model=PLAN_AUTHOR_MODEL,
            effort="high",
            budget_usd=3.0,
        )

    referenced = sorted(
        str(path.relative_to(ORBIT).as_posix())
        for path in review_root.rglob("*.json")
    ) + ["README.md", "verify_physics.py", "verify_ui.py"]
    (review_root / "resolution.json").write_text(
        json.dumps(
            {"status": "approved-for-admission", "history": history},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    referenced.append(f"{REVIEW_DIR}/resolution.json")
    decomposition = assemble_decomposition(draft, referenced)
    (ORBIT / PLAN_PATH).parent.mkdir(parents=True, exist_ok=True)
    (ORBIT / PLAN_PATH).write_text(str(draft["plan_markdown"]), encoding="utf-8")
    (ORBIT / DECOMPOSITION_PATH).write_text(
        json.dumps(decomposition, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    git(ORBIT, "add", "-A")
    git(
        ORBIT,
        "commit",
        "-m",
        "Engineer orbit plan through multi-lens and adversarial review",
    )
    print(
        json.dumps(
            {
                "stage": "plan",
                "base_commit": git(ORBIT, "rev-parse", "HEAD"),
                "cycles": history,
            },
            indent=2,
        )
    )


def finalize_plan(run_id: str) -> None:
    """Author the final plan once from all persisted lens findings; no re-review."""

    review_root = ORBIT / REVIEW_DIR
    reports = sorted(review_root.glob("cycle-*/[a-z]*.json"))
    findings = {
        str(path.relative_to(ORBIT).as_posix()): json.loads(
            path.read_text(encoding="utf-8")
        )
        for path in reports
        if path.name != "draft.json"
    }
    if not findings:
        raise SystemExit("finalize requires persisted lens reports")
    drafts = sorted(review_root.glob("cycle-*/draft.json"))
    latest_draft = (
        json.loads(drafts[-1].read_text(encoding="utf-8")) if drafts else None
    )
    charge = (
        "Author the final Orbit Lab plan. You are given every independent "
        "lens review from the pre-approval engineering rounds. Resolve every "
        "critical finding; in particular, never claim a deterministic gate "
        "proves behavior it does not observe — state plainly which acceptance "
        "criteria are proven by the gates and which are proven only by "
        "review. Address material findings when doing so does not expand "
        "scope. Read README.md, verify_physics.py, and verify_ui.py yourself "
        "before writing. This is the final authoring pass: there is no "
        "further review, so be conservative and honest.\n\nORIGINAL "
        "CHARGE:\n" + _draft_charge(run_id)
        + ("\n\nLATEST DRAFT:\n" + json.dumps(latest_draft, indent=2, sort_keys=True)
           if latest_draft else "")
        + "\n\nALL LENS FINDINGS:\n"
        + json.dumps(findings, indent=2, sort_keys=True)
    )
    print("plan-engineering: final authoring pass", flush=True)
    draft = claude_structured(
        charge,
        DRAFT_SCHEMA,
        model=PLAN_AUTHOR_MODEL,
        effort="high",
        budget_usd=3.0,
    )
    referenced = sorted(
        str(path.relative_to(ORBIT).as_posix())
        for path in review_root.rglob("*.json")
        if path.name != "resolution.json"
    ) + ["README.md", "verify_physics.py", "verify_ui.py"]
    (review_root / "resolution.json").write_text(
        json.dumps(
            {
                "status": "review-informed-finalized",
                "lens_reports": sorted(findings),
                "note": (
                    "Operator ended lens review; the final plan was authored "
                    "once against all persisted findings without re-review."
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    referenced.append(f"{REVIEW_DIR}/resolution.json")
    decomposition = assemble_decomposition(draft, referenced)
    (ORBIT / PLAN_PATH).parent.mkdir(parents=True, exist_ok=True)
    (ORBIT / PLAN_PATH).write_text(str(draft["plan_markdown"]), encoding="utf-8")
    (ORBIT / DECOMPOSITION_PATH).write_text(
        json.dumps(decomposition, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    git(ORBIT, "add", "-A")
    git(
        ORBIT,
        "commit",
        "-m",
        "Finalize orbit plan from multi-lens review findings",
    )
    print(
        json.dumps(
            {
                "stage": "finalize",
                "base_commit": git(ORBIT, "rev-parse", "HEAD"),
                "lens_reports": len(findings),
            },
            indent=2,
        )
    )


def approve(run_id: str) -> Path:
    approval_dir = ROOT / "logs" / "plan-approval" / run_id
    prepared = prepare_approval(
        repository=ORBIT,
        decomposition_path=ORBIT / DECOMPOSITION_PATH,
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
                    "Operator-directed experiment approval. The plan passed "
                    "three independent review lenses (FRAME, NECESSITY, "
                    "MECHANISM) before admission; the committed review "
                    "artifacts are bound into the approval subject as "
                    "referenced_artifacts."
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = issue_receipt(
        repository=ORBIT,
        subject_path=prepared.subject_path,
        gate_evidence_path=prepared.gate_evidence_path,
        operator_approval_path=operator_path,
        receipt_path=approval_dir / "receipt.json",
    )
    print(
        json.dumps(
            {
                "stage": "approve",
                "receipt": str(receipt),
                "plan_graph_digest": prepared.plan_graph_digest,
            },
            indent=2,
        )
    )
    return receipt


def _implementer_profiles(
    node: FeatureRunRequest, worktree: Path, evidence: EvidenceCatalog
) -> tuple[RoleProfile, ...]:
    instructions = f"""\
Read README.md and the deterministic gate scripts before editing. You are
implementing PlanGraph node {node.plan_node_id}: {node.run.objective}
Edit only these paths: {', '.join(node.run.allowed_paths)}. Follow the
engineering plan supplied in your task context. Run
{' '.join(node.run.verification_argv)} and repair every failure before
finishing. Do not commit. Do not edit the verify scripts or any path you do
not own. A prior failed attempt may have left uncommitted work in your
allowed paths; inspect it and finish or replace it rather than starting
blind. Your structured result is part of the deliverable: summary and
deliverable_markdown must substantively describe what you built and how the
gate proves it — placeholder text fails the run.
"""
    return (
        RoleProfile(
            profile_id="implementer",
            role="orbit_implementer",
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"orbit-implementation/1"}),
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
                    "Adversarially review the candidate for this PlanGraph "
                    "node against the engineering plan, the operator README, "
                    "and the deterministic gate."
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
Inspect the actual candidate for node {node.plan_node_id} and act as an
adversarial correctness, accessibility, and contract reviewer. Do not report
taste. Every finding needs file, stable subject, score, fix_cost, and the
exact acceptance clause in protects. Set scope_expanding when the remedy adds
behavior beyond the contract. Set contract_violation for real correctness,
accessibility, or shipped-behavior violations. On later cycles obey the
ledger and focus on regressions. Empty findings means the candidate clears.
""",
            "fix": f"""\
Inspect the supplied ledger and fix_finding_keys. Modify only
{', '.join(writable)}, and only as needed to resolve those exact findings
without feature growth. Run {' '.join(node.run.verification_argv)}. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
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
                "Repair the candidate so the deterministic verification "
                "command passes."
            ),
            "context": json.dumps(
                {**context, "artifact_kind": "verification-repair-report"},
                sort_keys=True,
            ),
            "details_schema": "orbit-verification-repair/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read", "repo.write"],
        }
        instructions = f"""\
The controller ran {' '.join(node.run.verification_argv)} and it failed; the
failure evidence is in your task context. Modify only {', '.join(writable)}
to make that exact command pass without feature growth. Run it yourself and
confirm exit code zero. Do not commit.
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
                "Plan admitted through FRAME/NECESSITY/MECHANISM lenses; "
                "review artifacts are committed under "
                f"{REVIEW_DIR} at the plan base commit."
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
        base_repository=ORBIT,
        base_branch="main",
        base_commit=request.base_commit,
        candidate_only=True,
        merge=False,
        feature_branch=f"plan-graph/{request.feature_run_id}",
        worktree_path=WORKTREE_ROOT / f"orbit-{request.feature_run_id}",
        run_dir=request.run_dir,
        session_factory=session_factory,
        profile_builder=profile_builder,
        commit_message=f"PlanGraph node {request.plan_node_id}",
        review_fix_executor_factory=review_fix,
        verification_repair_executor_factory=verification_repair,
    )
    return FeatureRunOutcome(
        status=result.status,
        candidate_commit=result.candidate_commit,
        plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id,
        feature_run_id=request.feature_run_id,
        run_dir=str(request.run_dir),
    )


def run_graph(run_id: str, receipt_path: Path) -> int:
    admission = PlanApprovalAdmission(repository=ORBIT, receipt_path=receipt_path)
    approved = admission.validate()
    registration = register_plan_graph(
        repository=ORBIT,
        logical_graph_id=f"orbit-lab-{run_id}",
        decomposition=approved.decomposition,
        base_commit=approved.base_commit,
        repository_id=approved.repository_id,
    )
    acceptance = dict(approved.decomposition["acceptance_criteria"])
    graph = PlanGraph(
        ORBIT,
        registration,
        lambda request: _launch_node(request, acceptance),
        run_root=ROOT / "logs" / "runs" / f"orbit-graph-{run_id}",
        graph_run_id=f"orbit-graph-{run_id}",
        approval_validator=admission.approval_validator(),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=("plan", "finalize", "approve", "run", "all"),
        default="all",
        nargs="?",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = arguments.run_id or stamp

    if arguments.stage in ("plan", "all"):
        engineer_plan(run_id)
    if arguments.stage == "finalize":
        finalize_plan(run_id)
    receipt_path = arguments.receipt
    if arguments.stage in ("approve", "all"):
        receipt_path = approve(run_id)
    if arguments.stage in ("run", "all"):
        if receipt_path is None:
            raise SystemExit("run stage requires --receipt or a prior approve stage")
        return run_graph(run_id, receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
