#!/usr/bin/env python3
"""Product-config-driven PlanGraph campaign launcher (DTR-F5 / DTR-LK-KIT).

Extracted from ``experiments/run_convergence_plan_graph.py`` at base
``8a13917``, the proven experiments-grade launcher for the convergence
campaign harness (CC-01 ... CC-05, CC-07). :func:`build_campaign_launch_config`
is the pinned product-config surface: every non-parameterized value in the
returned mapping transcribes that source file verbatim -- the coordinator
spec and its 7200.0s silence tolerance, implementer/reviewer specs and
models, the recovery/continuation/verification-repair limits, the worktree
policy booleans (``allow_dirty_baseline``, ``require_repository_change``,
``candidate_only``, ``merge``), and ``max_parallelism``. The remaining
values (plan/decomposition paths, logical graph id, agent-mixture specs,
profile-builder hook, operator-notes directory) are product-specific and
therefore parameterized, defaulting to the source's own values -- except the
operator-notes directory, which the source hardcoded to
``logs/plan-approval/operator-notes`` and this module exposes as a keyword
argument instead.

Two documented widenings sit outside that parity scope (tracked, not
accidental drift):

- :data:`ANTI_PLACEHOLDER_FLOOR` is now one shared constant folded into all
  four worker instruction texts (implementer, fix, review, verify). The
  source carried two divergently worded copies, in the implementer and fix
  instructions only, and said nothing to the reviewer or verifier.
- CC-08 wiring per ADR 0007
  (``docs/decisions/0007-in-graph-escalation-bounded-unsealing.md``): the
  config surface carries an ``escalation_judge`` seat and registers
  ``transfer_ownership`` in ``automatic_recovery.allowed_actions`` with a
  ``max_structural_decisions`` bound. The source launcher predates CC-08 and
  registered no escalation authority at all. This wiring only has an effect
  because ``_launch_node`` also passes
  ``ReviewFixPolicy(escalation_enabled=True)`` -- with that flag at its
  ``False`` default, the review/fix ledger never marks a finding escalated
  (``harness_labs/featurerun/review_fix.py``: ``_escalate_out_of_grant``,
  ``_apply_fixer_escalations``), so the graph's ``escalation_judge`` would be
  configured but never invoked. The sibling launcher
  (``experiments/run_dtr_plan_graph.py``) still passes the bare
  ``ReviewFixPolicy()`` because it wires no ``escalation_judge`` at all.

``experiments/run_convergence_plan_graph.py`` is now a thin shim over this
module: ``run_plan_graph_feature_worktree`` is called from here, not there.

This module is the DTR-F5 extraction of that proven experiments launcher
into a product-config-driven, reusable form for PlanGraph node DTR-LK-KIT.

Operator-notes folding (AC-LK-2): a note written under
``operator_notes_dir`` as ``<node_id>.md`` folds verbatim into the
implementer, review, and fix instruction texts via :func:`_operator_note` --
never into the verify instructions, since :func:`verify_instructions` takes
no ``operator_notes_dir`` argument at all. This mirrors the source
launcher's own asymmetry: it only ever consulted operator notes for the
stages that can act on repository content, not the read-only verify stage.
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
from typing import Mapping, Sequence

from harness_labs.core.agent_sessions import AgentSession
from harness_labs.core.claude_task_executor import ClaudeSemanticTaskExecutor
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import RunContract
from harness_labs.core.coordinator_dispatcher import CoordinatorLaunch
from harness_labs.featurerun.feature_run import (
    PlanGraphFeatureRunBinding,
    ReviewFixPolicy,
    run_plan_graph_feature_worktree,
)
from harness_labs.featurerun.feature_run_policy import (
    standard_feature_run_dispatch_schema,
)
from harness_labs.graphrun.agent_mixture import (
    WorkerRole,
    build_coordinator_session,
    build_role_profiles,
)
from harness_labs.graphrun.escalation_judge import (
    DEFAULT_JUDGE_IDENTITY,
    GraphEscalationJudgeSeat,
)
from harness_labs.plangraph.plan_approval import (
    UNCLAIMED_GRANT_WARNING,
    PlanApprovalAdmission,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_refinement import (
    refine_repository_decomposition,
)
from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    FeatureRunRequest,
    PlanGraph,
    PlanGraphError,
    RepairResumeDirective,
    load_registration,
    persist_registration,
    plan_from_registration,
    register_plan_graph,
)
from harness_labs.plangraph.plan_graph_authority import (
    AutomaticRecoveryAuthority,
    RecoveryAuthorityError,
    validate_plan_version_transition,
)

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_ROOT = ROOT.parent
CLAUDE = os.environ.get("CC_CLAUDE_EXECUTABLE", "claude")

# ---------------------------------------------------------------------------
# pinned product-config surface (AC-LK-1) -- verbatim transcription of
# experiments/run_convergence_plan_graph.py at base 8a13917
# ---------------------------------------------------------------------------
COORDINATOR_SPEC = "claude:claude-opus-4-8[1m]@medium"
# The coordinator sits silent inside task.dispatch while its worker runs, so
# this must exceed the longest worker runtime (600-900s killed real builds).
COORDINATOR_TIMEOUT_SECONDS = 7200.0
IMPLEMENTER_SPEC = "claude:claude-sonnet-5@high"
IMPLEMENTER_MODEL = "claude-sonnet-5"
REVIEWER_MODEL = "claude-opus-5"

RECOVERY_LIMIT = 5
CONTINUATION_RECOVERY_LIMIT = 3
VERIFICATION_REPAIR_LIMIT = 3
ALLOW_DIRTY_BASELINE = True
REQUIRE_REPOSITORY_CHANGE = True
CANDIDATE_ONLY = True
MERGE = False
MAX_PARALLELISM = 5

PLAN_PATH = "docs/development/convergence-campaign-plan.md"
DECOMPOSITION_PATH = "docs/development/convergence-campaign-decomposition.json"
LOGICAL_GRAPH_ID = "convergence-campaign-harness"
AGENT_MIXTURE = {"convergence_implementer": IMPLEMENTER_SPEC}
PROFILE_BUILDER_HOOK = build_role_profiles

# Parameterized in place of the source's hardcoded
# logs/plan-approval/operator-notes path.
DEFAULT_OPERATOR_NOTES_DIR = "logs/plan-approval/operator-notes"

# ---------------------------------------------------------------------------
# widening 1: one shared anti-placeholder floor, folded into all four worker
# instructions (AC-LK-3). The source carried two divergent copies, in the
# implementer and fix instructions only.
# ---------------------------------------------------------------------------
ANTI_PLACEHOLDER_FLOOR = """\
Your structured result is part of the deliverable: every summary and
deliverable field must substantively describe the actual work, at least four
characters, never a placeholder token (TODO, TBD, XXX, N/A, NA, placeholder,
WIP, FIXME, "fill in", lorem ipsum, template braces) and never one token
repeated. The harness hard-fails a result whose summary trips this floor,
even when you are interrupted or uncertain -- write concrete prose, never a
stub.\
"""

# ---------------------------------------------------------------------------
# widening 2: CC-08 escalation wiring, ADR 0007 (AC-LK-8). The source
# launcher predates CC-08 and registered no escalation authority. Paired with
# _launch_node's ReviewFixPolicy(escalation_enabled=True) below, which is
# what actually lets a review/fix ledger mark a finding escalated for the
# escalation_judge seat this wiring registers -- see the module docstring.
# ---------------------------------------------------------------------------
ESCALATION_JUDGE_SPEC = COORDINATOR_SPEC
ESCALATION_JUDGE_TIMEOUT_SECONDS = 900.0
AUTOMATIC_RECOVERY_ALLOWED_ACTIONS = ("resume", "extend_budget", "transfer_ownership")
MAX_EXTRA_NODE_LAUNCHES = 6
MAX_STRUCTURAL_DECISIONS = 2


def build_campaign_launch_config(
    *,
    plan_path: str = PLAN_PATH,
    decomposition_path: str = DECOMPOSITION_PATH,
    logical_graph_id: str = LOGICAL_GRAPH_ID,
    agent_mixture: Mapping[str, str] | None = None,
    profile_builder=PROFILE_BUILDER_HOOK,
    operator_notes_dir: str = DEFAULT_OPERATOR_NOTES_DIR,
    automatic_recovery: Mapping[str, object] | None = None,
) -> dict:
    """The pinned product-config surface (AC-LK-1).

    Every value not listed as a keyword argument here transcribes
    ``experiments/run_convergence_plan_graph.py`` at base ``8a13917``
    verbatim. The keyword arguments are the product-specific values the
    source hardcoded; each defaults to that same source value. The
    ``escalation_judge`` seat and the ``transfer_ownership``/
    ``max_structural_decisions`` entries in ``automatic_recovery`` are CC-08
    wiring per ADR 0007 -- a documented widening outside parity scope, since
    the source launcher predates CC-08 (AC-LK-8).
    """

    return {
        "coordinator": {
            "spec": COORDINATOR_SPEC,
            "timeout_seconds": COORDINATOR_TIMEOUT_SECONDS,
        },
        "implementer": {
            "spec": IMPLEMENTER_SPEC,
            "model": IMPLEMENTER_MODEL,
        },
        "reviewer": {
            "model": REVIEWER_MODEL,
        },
        "recovery_limit": RECOVERY_LIMIT,
        "continuation_recovery_limit": CONTINUATION_RECOVERY_LIMIT,
        "verification_repair_limit": VERIFICATION_REPAIR_LIMIT,
        "allow_dirty_baseline": ALLOW_DIRTY_BASELINE,
        "require_repository_change": REQUIRE_REPOSITORY_CHANGE,
        "candidate_only": CANDIDATE_ONLY,
        "merge": MERGE,
        "max_parallelism": MAX_PARALLELISM,
        "plan_path": plan_path,
        "decomposition_path": decomposition_path,
        "logical_graph_id": logical_graph_id,
        "agent_mixture": (
            dict(agent_mixture) if agent_mixture is not None else dict(AGENT_MIXTURE)
        ),
        "profile_builder_hook": profile_builder,
        "operator_notes_dir": operator_notes_dir,
        "escalation_judge": {
            "identity": DEFAULT_JUDGE_IDENTITY,
            "spec": ESCALATION_JUDGE_SPEC,
            "timeout_seconds": ESCALATION_JUDGE_TIMEOUT_SECONDS,
        },
        # Parameterized like the other product-specific values: recovery
        # authority is registration-immutable per lineage, so a campaign that
        # wants the plan-version transition path must grant its revision
        # action (e.g. "revise_acceptance") here at first registration. The
        # default stays the pinned CC-08 value.
        "automatic_recovery": (
            dict(automatic_recovery)
            if automatic_recovery is not None
            else {
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": AUTOMATIC_RECOVERY_ALLOWED_ACTIONS,
                "max_extra_node_launches": MAX_EXTRA_NODE_LAUNCHES,
                "max_structural_decisions": MAX_STRUCTURAL_DECISIONS,
            }
        ),
    }


# ---------------------------------------------------------------------------
# operator notes and worker instructions
# ---------------------------------------------------------------------------
def _operator_note(node_id: str, operator_notes_dir: str | Path) -> str:
    """Operator-relief guidance folded into a retried node's instructions.

    Written by the human-facing session operator after a blocked or failed
    attempt, anchored in reviewer findings; empty when no note exists. Lives
    under the (typically gitignored) notes directory so it never dirties the
    base.
    """
    # Anchored at ROOT, like this module's other relative config paths
    # (:409-410, :796): an absolute operator_notes_dir (e.g. a test tempdir)
    # still wins, since joining an absolute path onto ROOT replaces it.
    path = ROOT / operator_notes_dir / f"{node_id}.md"
    if not path.exists():
        return ""
    return (
        "\nOperator note for this retry (anchored in prior-attempt review "
        "findings; it clarifies the existing acceptance criteria and never "
        "widens your allowed paths):\n" + path.read_text(encoding="utf-8") + "\n"
    )


BASE_INSTRUCTIONS = """\
You are one phase coordinator in an audited FeatureRun. You cannot read files
or run commands. Use only typed controller tools. Follow the segment
instructions, inspect structured worker results, and open material artifacts
before advancing. Never claim work without evidence. Do not delegate beyond
the named workers.
"""


def _hard_constraints(plan_path: str) -> str:
    return f"""\
Hard constraints from the plan ({plan_path}):
- Consumers import by full module path; harness_labs/__init__.py is
  deliberately out of scope -- never touch it.
- The core layer must never import from the plangraph layer: the closed
  verdict and disposition vocabularies live in
  harness_labs/core/convergence_contract.py precisely so core-layer
  consumers never import harness_labs.plangraph
  (tests/test_import_boundaries.py enforces the boundary).
- No Playwright or any real-browser dependency may be added to harness CI:
  the capture path resolves its browser interpreter from --python (default
  sys.executable), the smoke test resolves UI_FIDELITY_PYTHON, and when no
  real browser is available the stub driver exercises the receipt and exit
  contract, recording a skip reason rather than passing silently.
- Reuse, don't rebuild: per-round attempt relaunch delegates to
  scripts/plan_graph_autoresume.py; evidence persistence reuses
  harness_labs/core/verification_images.py; conformance enforcement rides
  the existing warning-kind constants and acknowledgment gate in
  harness_labs/plangraph/plan_approval.py plus the
  harness_labs/plangraph/plan_refinement.py repair loop. Analyzers propose;
  they never mutate an approved decomposition in place.
- Findings/evidence contracts build on the existing semantic envelope in
  harness_labs/core/controller_results.py; per-round state stays in the
  existing review-ledger machinery.
"""


def implementer_instructions(
    *,
    node_id: str,
    objective: str,
    plan_sections: Sequence[str],
    allowed_paths: Sequence[str],
    verification_argv: Sequence[str],
    plan_path: str,
    decomposition_path: str,
    operator_notes_dir: str | Path,
) -> str:
    sections = ", ".join(plan_sections)
    return f"""\
You are building the convergence campaign harness inside harness_labs. Read,
in this order: {plan_path} -- specifically your node's cited sections
({sections}) -- then {decomposition_path} (your run entry and its
acceptance_criteria text), then the current code in and around your allowed
paths.
You are implementing PlanGraph node {node_id}: {objective}
Edit only these paths: {', '.join(allowed_paths)}.
The controller-owned gate is: {' '.join(verification_argv)}
Run it yourself before finishing and repair every failure.
{_hard_constraints(plan_path)}\
{_operator_note(node_id, operator_notes_dir)}\
Do not commit. A prior failed attempt may have left uncommitted work in your
allowed paths; inspect and finish or replace it rather than starting blind.
{ANTI_PLACEHOLDER_FLOOR}
"""


def review_instructions(
    *,
    node_id: str,
    plan_path: str,
    decomposition_path: str,
    operator_notes_dir: str | Path,
) -> str:
    return f"""\
Inspect the actual candidate for node {node_id} as an adversarial harness
reviewer. Priorities: (1) does every criterion in the node's
acceptance_criteria hold, byte-for-byte as written in
{decomposition_path}, with the gate proving it rather than asserting it?
(2) layering: nothing under harness_labs/core imports harness_labs.plangraph,
no real-browser dependency enters CI, and reused machinery
(plan_graph_autoresume.py, verification_images.py, plan_approval warning
kinds, plan_refinement loop) is delegated to, not reimplemented. (3) journal
and store disciplines: append-only flock+fsync where the plan demands it,
atomic replace with directory fsync for checkpoints, content-addressed seals.
(4) does anything violate the node's cited plan sections in {plan_path}?
Every finding needs file, stable subject, score, fix_cost, and the exact
acceptance clause in protects. Empty findings means the candidate clears.
Judge strictly against the node's acceptance criteria as written. When an
operator note below records a design ruling, that ruling is authoritative
acceptance-criteria interpretation: do not re-open it, and do not demand
work the ruling explicitly defers out of this node's scope.
{_operator_note(node_id, operator_notes_dir)}\
{ANTI_PLACEHOLDER_FLOOR}
"""


def fix_instructions(
    *,
    node_id: str,
    writable_paths: Sequence[str],
    verification_argv: Sequence[str],
    operator_notes_dir: str | Path,
) -> str:
    return f"""\
Inspect the supplied ledger and fix_finding_keys. Modify only
{', '.join(writable_paths)}, and only as needed to resolve those exact
findings without feature growth. Run {' '.join(verification_argv)}. Return
addressed_finding_keys as the exact subset actually fixed. Do not commit.
{ANTI_PLACEHOLDER_FLOOR}
{_operator_note(node_id, operator_notes_dir)}\
"""


def verify_instructions(*, verification_argv: Sequence[str]) -> str:
    return f"""\
Treat controller_verified_command as authoritative. Inspect the repaired
candidate and check every supplied fix_finding_key. Return
verified_finding_keys containing every key genuinely covered when the command
passes. Do not edit files.
{ANTI_PLACEHOLDER_FLOOR}
"""


# ---------------------------------------------------------------------------
# prepare / issue
# ---------------------------------------------------------------------------
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


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return completed.stdout.strip()


def prepare(config: Mapping[str, object], run_id: str) -> int:
    decomposition_path = config["decomposition_path"]
    approval_dir = _approval_dir(run_id)
    approval_dir.mkdir(parents=True, exist_ok=True)
    refinement = refine_repository_decomposition(
        repository=ROOT,
        decomposition_path=ROOT / decomposition_path,
    )
    (approval_dir / "refine-report.json").write_text(
        json.dumps(refinement.as_mapping(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    prepared = prepare_approval(
        repository=ROOT,
        decomposition_path=ROOT / decomposition_path,
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
            f"operator approval missing: {operator_path} -- this runner never "
            "authors it; the operator writes it by hand.",
            file=sys.stderr,
        )
        return 1
    receipt = issue_receipt(
        repository=ROOT,
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
def _implementer_roles(
    node: FeatureRunRequest, config: Mapping[str, object]
) -> tuple[WorkerRole, ...]:
    run = node.run
    instructions = implementer_instructions(
        node_id=node.plan_node_id,
        objective=run.objective,
        plan_sections=run.plan_sections,
        allowed_paths=run.allowed_paths,
        verification_argv=run.verification_argv,
        plan_path=config["plan_path"],
        decomposition_path=config["decomposition_path"],
        operator_notes_dir=config["operator_notes_dir"],
    )
    # The mixture key is the role's own identity, not a fixed literal, so a
    # caller who re-keys agent_mixture (e.g. {"other_implementer": ...})
    # still resolves via resolve_backend_spec's role-name lookup instead of
    # falling through to a KeyError at profile-build time.
    role_name = next(iter(config["agent_mixture"]), "convergence_implementer")
    return (
        WorkerRole(
            profile_id="implementer",
            role=role_name,
            capabilities=frozenset({"repo.read", "repo.write"}),
            details_schemas=frozenset({"cc-implementation/1"}),
            instructions=instructions,
            artifact_kind="implementation-summary",
            sandbox="workspace-write",
            writable_paths=tuple(run.allowed_paths),
            require_repository_change=config["require_repository_change"],
            allow_dirty_baseline=config["allow_dirty_baseline"],
        ),
    )


def _review_fix_factory(
    config: Mapping[str, object],
    node: FeatureRunRequest,
    worktree: Path,
    evidence: EvidenceCatalog,
):
    writable = tuple(node.run.allowed_paths)
    decomposition_path = config["decomposition_path"]
    plan_path = config["plan_path"]
    operator_notes_dir = config["operator_notes_dir"]
    implementer_model = config["implementer"]["model"]
    reviewer_model = config["reviewer"]["model"]

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
                    "Adversarially review the convergence-harness candidate "
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
            "review": review_instructions(
                node_id=node.plan_node_id,
                plan_path=plan_path,
                decomposition_path=decomposition_path,
                operator_notes_dir=operator_notes_dir,
            ),
            "fix": fix_instructions(
                node_id=node.plan_node_id,
                writable_paths=writable,
                verification_argv=node.run.verification_argv,
                operator_notes_dir=operator_notes_dir,
            ),
            "verify": verify_instructions(
                verification_argv=node.run.verification_argv,
            ),
        }[stage]
        return ClaudeSemanticTaskExecutor(
            task=task,
            repository=worktree,
            evidence=evidence,
            role_instructions=instructions,
            model=implementer_model if stage == "fix" else reviewer_model,
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
    config: Mapping[str, object],
    node: FeatureRunRequest,
    worktree: Path,
    evidence: EvidenceCatalog,
):
    writable = tuple(node.run.allowed_paths)
    plan_path = config["plan_path"]
    implementer_model = config["implementer"]["model"]

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
{_hard_constraints(plan_path)}\
Run the command yourself and confirm exit code zero. Do not commit.
"""
        return ClaudeSemanticTaskExecutor(
            task=task,
            repository=worktree,
            evidence=evidence,
            role_instructions=instructions,
            model=implementer_model,
            effort="high",
            executable=CLAUDE,
            sandbox="workspace-write",
            writable_paths=writable,
            audit=evidence.audit,
        )

    return factory


def _launch_node(
    config: Mapping[str, object],
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
                "The decomposition was adversarially reviewed (verdict "
                "DECOMPOSABLE-WITH-EDITS, edits applied) and surveyed "
                "against main's live machinery before approval; the "
                f"resolution is recorded in {config['plan_path']}."
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
            config["coordinator"]["spec"],
            base_instructions=BASE_INSTRUCTIONS + "\n" + launch.instructions,
            audit=evidence.audit,
            executable=CLAUDE,
            timeout_seconds=config["coordinator"]["timeout_seconds"],
        )

    holder: dict[str, object] = {}

    def profile_builder(candidate: Path, evidence: EvidenceCatalog):
        holder["candidate"] = candidate
        holder["evidence"] = evidence
        return config["profile_builder_hook"](
            mixture=dict(config["agent_mixture"]),
            roles=_implementer_roles(request, config),
            repository=candidate,
            evidence=evidence,
            audit=evidence.audit,
            executables={"claude": CLAUDE},
        )

    def review_fix(stage, attempt):
        return _review_fix_factory(
            config, request, holder["candidate"], holder["evidence"]
        )(stage, attempt)

    def verification_repair(attempt):
        return _verification_repair_factory(
            config, request, holder["candidate"], holder["evidence"]
        )(attempt)

    result = run_plan_graph_feature_worktree(
        binding=binding,
        schema=schema,
        contract_factory=contract_factory,
        # Widening 2 (module docstring, AC-LK-8): required so the review/fix
        # ledger can mark findings escalated for the escalation_judge seat
        # registered on the config surface -- with the False default here,
        # that seat would be wired but never invoked.
        review_fix_policy=ReviewFixPolicy(escalation_enabled=True),
        base_repository=ROOT,
        base_branch=base_branch,
        base_commit=request.base_commit,
        candidate_only=config["candidate_only"],
        merge=config["merge"],
        feature_branch=f"plan-graph/{request.feature_run_id}",
        worktree_path=WORKTREE_ROOT / f"cc-{request.feature_run_id}",
        run_dir=request.run_dir,
        session_factory=session_factory,
        profile_builder=profile_builder,
        commit_message=f"PlanGraph node {request.plan_node_id}",
        review_fix_executor_factory=review_fix,
        verification_repair_executor_factory=verification_repair,
        verification_repair_limit=config["verification_repair_limit"],
        # Attempt-4 died on budget, not substance: helper-task micro-failures
        # (no-change writable worker, one placeholder strike) exhausted the
        # default in-node recovery budget with the implement work already
        # delivered. Give real failures the same room, micro-stumbles more.
        recovery_limit=config["recovery_limit"],
        continuation_recovery_limit=config["continuation_recovery_limit"],
    )
    # ``outcome_evidence`` is the canonical shape: verification facts plus the
    # review-fix record. Omitting the review-fix half here silently dropped
    # transferred findings, still-open findings, and a parked fix worker's
    # out-of-fence disposition from every graph decision and escalation.
    evidence = result.outcome_evidence() or None
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
def _acceptance_criteria(config: Mapping[str, object]) -> dict[str, str]:
    return dict(
        json.loads((ROOT / config["decomposition_path"]).read_text(encoding="utf-8"))[
            "acceptance_criteria"
        ]
    )


def _launcher(config: Mapping[str, object], base_branch: str):
    acceptance = _acceptance_criteria(config)
    return lambda request: _launch_node(config, request, acceptance, base_branch)


def _escalation_judge(
    config: Mapping[str, object], registration
) -> GraphEscalationJudgeSeat:
    """One graph-level judgment seat for the whole attempt (ADR 0007 / CC-08).

    Built once per graph run, reused across every judgment, and bound to the
    live graph's sealed set by the caller so it judges against the graph as
    it is at that moment.
    """
    judge_config = config["escalation_judge"]
    return GraphEscalationJudgeSeat(
        judge_config["spec"],
        plan=plan_from_registration(registration),
        identity=judge_config["identity"],
        executable=CLAUDE,
        timeout_seconds=judge_config["timeout_seconds"],
    )


def _registration_root() -> Path:
    return ROOT / "logs" / "registration"


def load_validated_transition(
    config: Mapping[str, object],
    transition_path: Path,
    registration_root: Path,
) -> dict[str, object]:
    """Load a plan-version transition and fail closed before any registration.

    The record must be a JSON object validating as
    ``plan-graph-version-transition/1`` against the campaign's configured
    recovery authority (so the revision action must be granted there), and
    the campaign's persisted registration must exist and be the exact
    predecessor the transition names.  Both refusals happen before
    ``register_plan_graph`` or ``persist_registration`` can observe the
    transition, so a rejected record never strands the registration slot.
    """
    payload = json.loads(transition_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PlanGraphError("plan-version transition must be a JSON object")
    try:
        authority = AutomaticRecoveryAuthority.from_mapping(
            dict(
                config["automatic_recovery"],
                allowed_actions=list(config["automatic_recovery"]["allowed_actions"]),
            )
        )
        checked = validate_plan_version_transition(payload, authority)
    except RecoveryAuthorityError as exc:
        raise PlanGraphError(f"plan-version transition is invalid: {exc}") from exc
    registration_path = registration_root / f"{config['logical_graph_id']}.json"
    if not registration_path.exists():
        raise PlanGraphError(
            "plan-version transition requires an existing predecessor "
            f"registration at {registration_path}"
        )
    predecessor = load_registration(registration_path)
    if checked["predecessor_plan_sha256"] != predecessor.plan_sha256:
        raise PlanGraphError(
            "plan-version transition does not name the persisted "
            "registration's plan_sha256 as its exact predecessor"
        )
    return checked


def _register_campaign_graph(
    config: Mapping[str, object],
    approved,
    transition: Mapping[str, object] | None,
):
    """Register and persist the campaign graph, honoring a validated transition.

    Without a transition this is the pre-existing fail-closed registration.
    With one, ``persist_registration`` replaces the persisted predecessor
    under attestation.  After a transition has been consumed, the persisted
    registration keeps carrying it; a later transition-less invocation of the
    same approved plan adopts that persisted registration instead of failing
    on the digest difference the embedded transition causes.
    """
    automatic_recovery = config["automatic_recovery"]
    registration = register_plan_graph(
        repository=ROOT,
        logical_graph_id=config["logical_graph_id"],
        decomposition=approved.decomposition,
        base_commit=approved.base_commit,
        repository_id=approved.repository_id,
        plan_lineage_id=_approval_lineage_id(
            approved.repository_id, approved.decomposition_path
        ),
        automatic_recovery={
            "protocol": automatic_recovery["protocol"],
            "allowed_actions": list(automatic_recovery["allowed_actions"]),
            "max_extra_node_launches": automatic_recovery["max_extra_node_launches"],
            "max_structural_decisions": automatic_recovery["max_structural_decisions"],
        },
        plan_version_transition=transition,
    )
    registration_root = _registration_root()
    if transition is None:
        existing_path = registration_root / f"{registration.logical_graph_id}.json"
        if existing_path.exists():
            existing = load_registration(existing_path)
            if (
                existing.plan_version_transition is not None
                and existing.plan_sha256 == registration.plan_sha256
                and existing.base_commit == registration.base_commit
                and existing.definition_json == registration.definition_json
                and existing.plan_lineage_id == registration.plan_lineage_id
                and existing.automatic_recovery == registration.automatic_recovery
            ):
                return existing, existing_path
    registration_path = persist_registration(
        repository=ROOT,
        registration_root=registration_root,
        registration=registration,
    )
    return registration, registration_path


def run_graph(
    config: Mapping[str, object],
    receipt_path: Path,
    graph_attempt_id: str,
    run_root: Path,
    transition: Mapping[str, object] | None = None,
) -> int:
    admission = PlanApprovalAdmission(repository=ROOT, receipt_path=receipt_path)
    approved = admission.validate()
    base_branch = git(ROOT, "rev-parse", "--abbrev-ref", "HEAD")
    logical_graph_id = config["logical_graph_id"]
    registration, registration_path = _register_campaign_graph(
        config, approved, transition
    )
    print(json.dumps({"registration": str(registration_path)}))
    judge = _escalation_judge(config, registration)
    graph = PlanGraph(
        ROOT,
        registration,
        _launcher(config, base_branch),
        run_root=run_root,
        graph_run_id=graph_attempt_id,
        logical_graph_id=logical_graph_id,
        approval_validator=admission.approval_validator(),
        # Ready-set execution: CC-01 first; then the CC-02->CC-04 spine runs
        # parallel to CC-03 (file-disjoint lanes, S1); CC-05 joins both;
        # CC-07 last. Operator-preferred ceiling; graph width caps actual use.
        max_parallelism=config["max_parallelism"],
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
    config: Mapping[str, object],
    receipt_path: Path,
    graph_attempt_id: str | None,
    run_root: Path,
    logical_id: str,
    predecessor: str,
    frontier: list[str],
    blocker: str,
    transition: Mapping[str, object] | None = None,
) -> int:
    admission = PlanApprovalAdmission(repository=ROOT, receipt_path=receipt_path)
    approved = admission.validate()
    base_branch = git(ROOT, "rev-parse", "--abbrev-ref", "HEAD")
    if transition is not None:
        # An amendment mid-lineage: re-register the approved successor plan
        # under the validated transition (attested replace of the persisted
        # predecessor) instead of resuming the stale registration.
        registration, registration_path = _register_campaign_graph(
            config, approved, transition
        )
        print(json.dumps({"registration": str(registration_path)}))
    else:
        registration = load_registration(
            _registration_root() / f"{config['logical_graph_id']}.json"
        )
    directive = RepairResumeDirective(
        logical_graph_id=logical_id,
        predecessor_attempt_id=predecessor,
        retry_frontier=tuple(frontier),
        blocker_evidence_ref=blocker,
    )
    judge = _escalation_judge(config, registration)
    graph = PlanGraph.resume(
        ROOT,
        registration,
        _launcher(config, base_branch),
        run_root=run_root,
        directive=directive,
        approval_validator=admission.approval_validator(),
        max_parallelism=config["max_parallelism"],
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


def main(
    argv: Sequence[str] | None = None,
    *,
    config: Mapping[str, object] | None = None,
) -> int:
    if config is None:
        config = build_campaign_launch_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "issue", "run", "resume"))
    parser.add_argument("--run-id", default="convergence-cc-1")
    parser.add_argument("--receipt", type=Path)
    # Resume flags use scripts/run_plan_graph.py's spelling so
    # scripts/plan_graph_autoresume.py can append its directive unchanged.
    parser.add_argument("--graph-attempt-id")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--resume", action="store_true", help="autoresume compat no-op")
    parser.add_argument("--logical-graph-id", default=config["logical_graph_id"])
    parser.add_argument("--predecessor-attempt-id")
    parser.add_argument("--retry-frontier", action="append", default=[])
    parser.add_argument("--blocker-evidence-ref")
    parser.add_argument(
        "--transition",
        type=Path,
        help=(
            "path to a plan-graph-version-transition/1 JSON record "
            "authorizing a plan amendment on run or resume"
        ),
    )
    arguments = parser.parse_args(argv)
    # PlanGraph IDs must match ^[a-z0-9][a-z0-9-]{0,127}$ -- lowercase, no T/Z.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", arguments.run_id):
        parser.error(
            f"--run-id must match ^[a-z0-9][a-z0-9-]+$, got {arguments.run_id!r}"
        )

    if arguments.stage in ("prepare", "issue"):
        if arguments.transition is not None:
            parser.error("--transition applies only to the run and resume stages")
        if arguments.stage == "prepare":
            return prepare(config, arguments.run_id)
        return issue(arguments.run_id)
    receipt = arguments.receipt
    if receipt is None:
        receipt = _approval_dir(arguments.run_id) / "receipt.json"
    if not receipt.exists():
        parser.error(f"receipt not found: {receipt}")
    run_root = arguments.run_root or (ROOT / "logs" / "runs" / "cc-graph")
    transition = None
    if arguments.transition is not None:
        try:
            transition = load_validated_transition(
                config, arguments.transition, _registration_root()
            )
        except (OSError, ValueError) as exc:
            # PlanGraphError subclasses ValueError; a refused transition must
            # halt before registration, admission, or ledger state changes.
            print(f"plan-version transition refused: {exc}", file=sys.stderr)
            return 1
    if arguments.stage == "resume":
        if not (
            arguments.predecessor_attempt_id and arguments.blocker_evidence_ref
        ):
            parser.error(
                "resume requires --predecessor-attempt-id and "
                "--blocker-evidence-ref (plus optional --retry-frontier)"
            )
        return resume_graph(
            config,
            receipt,
            arguments.graph_attempt_id,
            run_root,
            arguments.logical_graph_id,
            arguments.predecessor_attempt_id,
            arguments.retry_frontier,
            arguments.blocker_evidence_ref,
            transition=transition,
        )
    graph_attempt_id = arguments.graph_attempt_id or f"cc-graph-{arguments.run_id}"
    return run_graph(config, receipt, graph_attempt_id, run_root, transition=transition)


if __name__ == "__main__":
    raise SystemExit(main())
