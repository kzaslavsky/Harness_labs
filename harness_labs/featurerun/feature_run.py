"""Production FeatureRun entrypoint with controller-owned Git transactions."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic_ns
from typing import Any, Callable, Literal, Mapping, Protocol

from harness_labs.core.agent_sessions import AgentSession
from harness_labs.core.attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_commands import CommandActor, CommandEnvelope
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import ControllerKernel, RunContract
from harness_labs.core.controller_live import DirtyBaselineGrantVerification, verify_dirty_baseline_grant
from harness_labs.core.controller_projection import project_run_view
from harness_labs.core.controller_scheduler import CapabilityScheduler, RoleProfile
from harness_labs.core.coordinator_dispatcher import (
    CoordinatorDispatchResult,
    CoordinatorDispatcher,
    CoordinatorLaunch,
)
from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema
from harness_labs.core.git_transaction import (
    GitTransactionError,
    GitWorktreeTransaction,
    normalize_allowed_paths,
    paths_outside_scope,
    workspace_snapshot,
)
from harness_labs.core.test_output import failing_identifiers
from harness_labs.core.verification_images import (
    FAILURE_IMAGE_CONTEXT_KEY,
    SCOPE_FAILING_TESTS,
    capture_failure_images,
    pytest_basetemp_argv,
)
from harness_labs.featurerun.review_fix import (
    ReviewFixExecutorFactory,
    ReviewFixLoop,
    ReviewFixPolicy,
    ReviewFixResult,
)


FeatureContractFactory = Callable[
    [Path, Mapping[str, object]],
    RunContract,
]
FeatureSessionFactory = Callable[
    [Path, CoordinatorLaunch, EvidenceCatalog],
    AgentSession,
]
FeatureProfileBuilder = Callable[
    [Path, EvidenceCatalog],
    tuple[RoleProfile, ...],
]
RecoveryCondition = Literal["blocked", "failed", "interrupted"]


_NORMAL_FEATURE_PHASES = (
    "orient",
    "plan",
    "implement",
    "verify",
    "review",
    "integrate",
    "report",
)


@dataclass(frozen=True)
class FeatureRunHandoffArtifact:
    """One controller-owned artifact available before coordinator dispatch."""

    kind: str
    content: object
    media_type: str = "application/json"
    producer_task_id: str = "plan-graph"

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.kind, self.media_type, self.producer_task_id)
        ):
            raise ValueError("handoff artifact metadata must be non-empty")


@dataclass(frozen=True)
class VerificationGate:
    """One named, independently timed command within a node's gate tuple.

    An ordered tuple of these replaces a single flat ``verification_argv``
    when a node's deterministic verification is decomposed: each gate runs,
    is classified, and is repaired independently, while a full re-run after
    any repair always restarts at the first gate. A flat ``verification_argv``
    remains valid and byte-identical in behavior when no gates are declared.
    """

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 1200.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("verification gate name must be non-empty")
        if not self.argv or any(
            not isinstance(value, str) or not value for value in self.argv
        ):
            raise ValueError("verification gate argv must contain non-empty strings")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("verification gate timeout_seconds must be positive")


@dataclass(frozen=True)
class PlanGraphFeatureRunBinding:
    """Approved PlanGraph handoff replacing only FeatureRun orient and plan."""

    plan_graph_id: str
    plan_node_id: str
    objective: str
    acceptance_criteria: tuple[Mapping[str, object], ...]
    approved_plan: Mapping[str, object]
    source_binding_report: Mapping[str, object]
    build_briefing: Mapping[str, object]
    plan: str
    plan_base_commit: str
    plan_sha256: str
    allowed_paths: tuple[str, ...] = ()
    verification_argv: tuple[str, ...] = ()
    verification_timeout_seconds: float = 1200.0
    parent_candidate_commit: str | None = None
    lane_branch: str | None = None
    lane_worktree: Path | None = None
    logical_attempt: int | None = None
    allocation_id: str | None = None
    checkpoint_revision: int | None = None
    expected_staging_head: str | None = None
    batch_id: str | None = None
    dependency_candidates: tuple[Mapping[str, object], ...] | None = None
    writable_paths: tuple[str, ...] | None = None
    finding_obligations: tuple[Mapping[str, object], ...] = ()
    finding_transfer_targets: Mapping[str, str] | None = None
    origin_node_id: str = ""
    inherited_ledger_frozen: bool = False
    verification_gates: tuple[VerificationGate, ...] = ()

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.plan_graph_id,
                self.plan_node_id,
                self.objective,
                self.plan,
                self.plan_base_commit,
                self.plan_sha256,
            )
        ):
            raise ValueError("PlanGraph FeatureRun binding identity must be non-empty")
        if not self.acceptance_criteria:
            raise ValueError("PlanGraph FeatureRun binding requires acceptance criteria")
        for name in ("approved_plan", "source_binding_report", "build_briefing"):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or not value:
                raise ValueError(f"PlanGraph FeatureRun binding {name} must be non-empty")
        if not self.allowed_paths or any(not value for value in self.allowed_paths):
            raise ValueError("PlanGraph FeatureRun binding requires allowed_paths")
        if self.verification_argv and self.verification_gates:
            raise ValueError(
                "PlanGraph FeatureRun binding may declare verification_argv or "
                "verification_gates, not both"
            )
        if self.verification_argv:
            if any(not value for value in self.verification_argv):
                raise ValueError(
                    "PlanGraph FeatureRun binding requires verification argv"
                )
            if self.verification_timeout_seconds <= 0:
                raise ValueError(
                    "PlanGraph FeatureRun binding requires a positive timeout"
                )
        elif self.verification_gates:
            gate_names = [gate.name for gate in self.verification_gates]
            if len(gate_names) != len(set(gate_names)):
                raise ValueError(
                    "PlanGraph FeatureRun binding verification_gates must have "
                    "unique names"
                )
        else:
            raise ValueError(
                "PlanGraph FeatureRun binding requires verification argv or "
                "verification_gates"
            )
        if not isinstance(self.finding_obligations, tuple) or not all(
            isinstance(item, Mapping) for item in self.finding_obligations
        ):
            raise ValueError("PlanGraph finding_obligations must be a tuple of objects")
        if self.finding_transfer_targets is not None and (
            not isinstance(self.finding_transfer_targets, Mapping)
            or not all(
                isinstance(path, str) and path
                and isinstance(node_id, str) and node_id
                for path, node_id in self.finding_transfer_targets.items()
            )
        ):
            raise ValueError("PlanGraph finding_transfer_targets must map paths to node IDs")
        if not isinstance(self.origin_node_id, str):
            raise ValueError("PlanGraph origin_node_id must be a string")
        if not isinstance(self.inherited_ledger_frozen, bool):
            raise ValueError("PlanGraph inherited_ledger_frozen must be a boolean")
        briefing_paths = self.build_briefing.get("allowed_paths")
        if briefing_paths is not None and tuple(briefing_paths) != self.allowed_paths:
            raise ValueError("build briefing allowed_paths do not match approved grant")
        if (
            self.approved_plan.get("path") != self.plan
            or self.approved_plan.get("sha256") != self.plan_sha256
        ):
            raise ValueError("PlanGraph approved-plan artifact does not match its source binding")
        if len(self.plan_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.plan_sha256
        ):
            raise ValueError("PlanGraph plan_sha256 must be a lowercase SHA-256")
        child_values = (
            self.parent_candidate_commit,
            self.lane_branch,
            self.lane_worktree,
            self.logical_attempt,
            self.allocation_id,
            self.checkpoint_revision,
            self.expected_staging_head,
            self.batch_id,
            self.dependency_candidates,
            self.writable_paths,
        )
        if any(value is not None for value in child_values):
            if not all(value is not None for value in child_values):
                raise ValueError(
                    "PlanGraph child binding requires one complete allocated lane"
                )
            assert self.parent_candidate_commit is not None
            assert self.expected_staging_head is not None
            if not _is_plan_graph_id(self.plan_graph_id) or not _is_plan_graph_id(
                self.plan_node_id
            ):
                raise ValueError("PlanGraph child binding identifiers must be schema-valid")
            if not _is_full_commit(self.parent_candidate_commit) or not _is_full_commit(
                self.expected_staging_head
            ):
                raise ValueError(
                    "PlanGraph child commits must be full lowercase Git commits"
                )
            if self.expected_staging_head != self.parent_candidate_commit:
                raise ValueError(
                    "PlanGraph child expected_staging_head must match its parent candidate"
                )
            if not isinstance(self.lane_branch, str) or not self.lane_branch.strip():
                raise ValueError("PlanGraph child lane_branch must be non-empty")
            if not isinstance(self.lane_worktree, Path):
                raise ValueError("PlanGraph child lane_worktree must be a Path")
            if type(self.logical_attempt) is not int or self.logical_attempt < 1:
                raise ValueError("PlanGraph child logical_attempt must be positive")
            if type(self.checkpoint_revision) is not int or self.checkpoint_revision < 1:
                raise ValueError("PlanGraph child checkpoint_revision must be positive")
            if not _is_plan_graph_id(self.allocation_id):
                raise ValueError("PlanGraph child allocation_id must be a valid identifier")
            if not isinstance(self.batch_id, str) or not _is_plan_graph_id(self.batch_id):
                raise ValueError("PlanGraph child batch_id must be a valid identifier")
            if not isinstance(self.dependency_candidates, tuple):
                raise ValueError(
                    "PlanGraph child dependency_candidates must be an ordered tuple"
                )
            _validate_dependency_candidates(self.dependency_candidates)
            if not isinstance(self.writable_paths, tuple):
                raise ValueError("PlanGraph child writable_paths must be an ordered tuple")
            if not self.writable_paths:
                raise ValueError("PlanGraph child writable_paths must not be empty")
            normalize_allowed_paths(self.writable_paths)

    @property
    def is_child_lane(self) -> bool:
        return self.parent_candidate_commit is not None

    def child_descriptor(self) -> dict[str, object]:
        """Return the allocation-bound descriptor preserved with child evidence."""

        if not self.is_child_lane:
            raise ValueError("only an allocated PlanGraph child has a lane descriptor")
        assert self.parent_candidate_commit is not None
        assert self.lane_branch is not None
        assert self.lane_worktree is not None
        assert self.logical_attempt is not None
        assert self.allocation_id is not None
        assert self.checkpoint_revision is not None
        assert self.expected_staging_head is not None
        assert self.batch_id is not None
        assert self.dependency_candidates is not None
        assert self.writable_paths is not None
        return {
            "protocol": "harness-plan-graph-parallel-child-request/1",
            "graph_id": self.plan_graph_id,
            "node_id": self.plan_node_id,
            "allocation": {
                "batch_id": self.batch_id,
                "logical_attempt": self.logical_attempt,
                "allocation_id": self.allocation_id,
                "checkpoint_revision": self.checkpoint_revision,
                "expected_staging_head": self.expected_staging_head,
            },
            "parent_candidate_commit": self.parent_candidate_commit,
            "dependency_candidates": [
                dict(candidate) for candidate in self.dependency_candidates
            ],
            "lane": {
                "branch": self.lane_branch,
                "worktree": str(self.lane_worktree.resolve()),
                "may_advance_staging": False,
            },
            "writable_paths": list(normalize_allowed_paths(self.writable_paths)),
        }

    def handoff_artifacts(self) -> tuple[FeatureRunHandoffArtifact, ...]:
        def envelope(content: Mapping[str, object]) -> dict[str, object]:
            return {
                "protocol": "plan-graph-feature-handoff/1",
                "plan_graph_id": self.plan_graph_id,
                "plan_node_id": self.plan_node_id,
                "objective": self.objective,
                "acceptance_criteria": [dict(item) for item in self.acceptance_criteria],
                "parent_candidate_commit": self.parent_candidate_commit,
                "lane_branch": self.lane_branch,
                "content": dict(content),
            }

        artifacts = (
            FeatureRunHandoffArtifact(
                "engineering-plan", envelope(self.approved_plan)
            ),
            FeatureRunHandoffArtifact(
                "source-binding-report", envelope(self.source_binding_report)
            ),
            FeatureRunHandoffArtifact(
                "build-briefing", envelope(self.build_briefing)
            ),
        )
        if self.is_child_lane:
            artifacts += (
                FeatureRunHandoffArtifact(
                    "plan-graph-child-request",
                    self.child_descriptor(),
                    producer_task_id=self.plan_graph_id,
                ),
            )
        return artifacts


class VerificationRepairExecutorFactory(Protocol):
    """Construct the fixer for one failed deterministic verification attempt."""

    def __call__(self, attempt: TaskAttempt) -> Executor:
        """Return an executor that repairs the current candidate worktree."""


@dataclass(frozen=True)
class RecoveryContext:
    """Bounded context supplied after an abnormal FeatureRun stage outcome."""

    run_id: str
    stage: str
    condition: RecoveryCondition
    reason: str
    attempt: int
    checkpoint: Mapping[str, object]
    objective: str
    acceptance_criteria: tuple[Mapping[str, object], ...]
    worktree_path: str
    allowed_paths: tuple[str, ...]
    workspace: Mapping[str, object]
    prior_decisions: tuple[Mapping[str, object], ...]
    plan_adjustments: tuple[Mapping[str, object], ...]
    # Stage-local evidence the agent needs to judge whether continuing is
    # worth its cost -- e.g. for "review": cycles spent, the cycle limit, the
    # still-open finding keys, and the per-cycle yields already observed.
    stage_detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryDecision:
    """One recovery-agent decision understood by the FeatureRun controller."""

    action: Literal["retry", "adjust_plan", "stop"]
    reason: str
    plan_adjustment: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.action not in {"retry", "adjust_plan", "stop"}:
            raise ValueError("recovery action must be retry, adjust_plan, or stop")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("recovery decision reason must be non-empty")
        if self.action == "adjust_plan":
            if not isinstance(self.plan_adjustment, Mapping) or not self.plan_adjustment:
                raise ValueError("adjust_plan requires a non-empty plan adjustment")
        elif self.plan_adjustment is not None:
            raise ValueError("plan adjustment is allowed only for adjust_plan")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "plan_adjustment": (
                dict(self.plan_adjustment)
                if self.plan_adjustment is not None
                else None
            ),
        }


class RecoveryAgent(Protocol):
    """Return one bounded decision after inspecting an abnormal run outcome."""

    def __call__(self, context: RecoveryContext) -> RecoveryDecision:
        """Perform any authorized recovery work and select the next action."""


# Reason substrings that identify infrastructure-transient abnormal outcomes:
# backend/coordinator stream deaths and connection failures that a bounded
# in-run retry absorbs without any change of strategy. Deliberately narrow —
# review/verification outcomes ("cycle limit reached", test failures) must
# escalate, not retry.
TRANSIENT_RECOVERY_SIGNATURES: tuple[str, ...] = (
    "aborted_streaming",
    "request interrupted",
    "broken pipe",
    "connection reset",
    "connection aborted",
    "connection refused",
    "backend process terminated",
    "backend_process_terminated",
    "stream disconnected",
    "server disconnected",
    "socket hang up",
    "overloaded_error",
    "rate_limit",
    "timed out reading",
    # A coordinator/backend process that dies without recording a blocker
    # surfaces only the dispatcher's generic terminal line (observed live:
    # codex backend_process_terminated -> "dispatcher ended with status
    # blocked"). A genuine content block records an explicit blocker and
    # takes the specific-reason path instead, so this generic form is
    # treated as transient; recovery_limit still bounds the retries.
    "dispatcher ended with status",
    # Local DNS/network resolution failure reaching the backend API
    # (observed live: codex websocket connect to chatgpt.com failing with
    # "failed to lookup address information: nodename nor servname
    # provided, or not known") -- a transient host/network condition
    # unrelated to the worker's actual task, not a content-level failure.
    "failed to lookup address information",
    "failed to connect to websocket",
)


def deterministic_recovery_agent(context: RecoveryContext) -> RecoveryDecision:
    """Rule-based default RecoveryAgent: retry transients, stop classified.

    Retries (bounded by ``recovery_limit`` and the unchanged-strategy guard)
    when the abnormal outcome is an interruption or matches a transient
    infrastructure signature; otherwise stops immediately with a classified
    reason so the block escalates with evidence instead of grinding. The
    recovery attempt ordinal is part of the retry reason so consecutive
    retries of a recurring transient remain distinct decisions.
    """

    reason = context.reason.lower()
    transient = context.condition == "interrupted" or any(
        signature in reason for signature in TRANSIENT_RECOVERY_SIGNATURES
    )
    if transient:
        return RecoveryDecision(
            "retry",
            f"transient backend interruption (recovery attempt {context.attempt}): "
            + context.reason[:160],
        )
    return RecoveryDecision(
        "stop",
        f"non-transient {context.condition} at stage {context.stage!r}; "
        "escalating with classified evidence: " + context.reason[:200],
    )


# Recovery classes.  A FeatureRun recovers from two unrelated kinds of
# abnormal ending, and they must not compete for one counter: infrastructure
# transients are noise whose frequency says nothing about the work, while a
# review continuation is a content decision that happens at most once or twice
# per run and only after the whole implement/verify/review chain has been paid
# for.  Sharing a budget lets a run's stream deaths silently deny its one
# continuation, which then reads as "the continuation policy never fired".
RECOVERY_CLASS_GENERAL = "general"
RECOVERY_CLASS_REVIEW_CONTINUATION = "review_continuation"


def _recovery_class(
    stage: str,
    condition: RecoveryCondition,
    detail: Mapping[str, object],
) -> str:
    """Classify an abnormal ending by *opportunity*, not by agent verdict.

    The budget is checked before the agent is consulted, so the class has to
    come from the situation itself.  ``review``/``blocked``/``cycle_limit`` is
    exactly the shape a review continuation is offered for; every other
    ending -- including a review that stopped on its own futility detectors --
    is charged to the general budget, because no continuation was on offer.
    """

    if (
        stage == "review"
        and condition == "blocked"
        and str(detail.get("stop_reason", "")) == "cycle_limit"
    ):
        return RECOVERY_CLASS_REVIEW_CONTINUATION
    return RECOVERY_CLASS_GENERAL


@dataclass
class _RecoveryState:
    """Per-class recovery budgets plus a total ceiling for the whole run."""

    limit: int
    continuation_limit: int = 0
    decisions: list[Mapping[str, object]] = field(default_factory=list)

    def class_limit(self, recovery_class: str) -> int:
        if recovery_class == RECOVERY_CLASS_REVIEW_CONTINUATION:
            return self.continuation_limit
        return self.limit

    def class_used(self, recovery_class: str) -> int:
        return sum(
            1
            for item in self.decisions
            if item.get("recovery_class") == recovery_class
        )

    @property
    def total_limit(self) -> int:
        # Kept as an explicit ceiling rather than left implicit in the sum of
        # the class limits, so a future class cannot widen the total by
        # accident and a pathological run stays bounded.
        return self.limit + self.continuation_limit


@dataclass(frozen=True)
class DeterministicVerificationResult:
    """Controller-observed command attempts and bounded recovery outcome."""

    status: str
    reason: str
    command_attempts: tuple[Mapping[str, object], ...]
    repair_attempts: int
    repair_invocation_ids: tuple[str, ...] = ()
    repair_invocations: tuple[Mapping[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "command_attempts": [dict(item) for item in self.command_attempts],
            "repair_attempts": self.repair_attempts,
            "repair_invocation_ids": list(self.repair_invocation_ids),
            "repair_invocations": [dict(item) for item in self.repair_invocations],
        }


_VERIFICATION_FAILURE_CLASSES = frozenset({
    "product", "infrastructure_transient", "harness_or_configuration",
    "policy_violation", "indeterminate",
})


_DRIVER_CRASH_MARKERS = (
    "walk driver",
    "driver crashed",
    "browser crashed",
    "webdriver crashed",
    "browser has disconnected",
    "target closed",
)
_PYTEST_PASSED_RE = re.compile(r"\d+ passed\b")
_PYTEST_FAILING_RE = re.compile(r"\b(?!0\b)\d+ (?:failed|errors?)\b")
_PYTEST_FAILED_NODE_RE = re.compile(r"^failed ", re.MULTILINE)


def _is_driver_crash_with_green_pytest(lowered_full_text: str) -> bool:
    """True when a browser/driver crash marker sits next to a clean pytest run.

    Distinguishes a live-browser walk-driver crash (environment fault) from a
    genuine assertion failure inside the same pytest invocation: pytest's own
    summary line is the ground truth for whether the product code failed.
    """
    if not any(marker in lowered_full_text for marker in _DRIVER_CRASH_MARKERS):
        return False
    if not _PYTEST_PASSED_RE.search(lowered_full_text):
        return False
    if _PYTEST_FAILING_RE.search(lowered_full_text):
        return False
    if _PYTEST_FAILED_NODE_RE.search(lowered_full_text):
        return False
    return True


def classify_verification_failure(command: Mapping[str, object]) -> dict[str, str]:
    """Classify only evidence present in one deterministic command result.

    Structured fields are checked before any output-text heuristic: a timeout
    (the `timed_out` flag or exit code 124) or a signal termination (a
    negative exit code) is an environment fault by construction, so it must
    not depend on what the command happened to print. The conservative
    default still treats unrecognized output as indeterminate: neither proves
    a product defect nor transient infrastructure.
    """
    full_text = "\n".join(str(command.get(key, "")) for key in ("stderr", "stdout"))
    excerpt = full_text[:500]
    lowered = excerpt.lower()
    if command.get("timed_out") is True:
        return {"classification": "infrastructure_transient", "rule_id": "timeout-flag", "evidence_excerpt": excerpt}
    exit_code = command.get("exit_code")
    if isinstance(exit_code, int):
        if exit_code == 124:
            return {"classification": "infrastructure_transient", "rule_id": "timeout-exit-124", "evidence_excerpt": excerpt}
        if exit_code < 0:
            return {
                "classification": "infrastructure_transient",
                "rule_id": f"signal-terminated-{-exit_code}",
                "evidence_excerpt": excerpt,
            }
    if _is_driver_crash_with_green_pytest(full_text.lower()):
        return {"classification": "infrastructure_transient", "rule_id": "driver-crash-pytest-green", "evidence_excerpt": excerpt}
    if any(marker in lowered for marker in ("connection reset", "temporary failure", "dns", "network is unreachable")):
        return {"classification": "infrastructure_transient", "rule_id": "transient-network", "evidence_excerpt": excerpt}
    if any(marker in lowered for marker in ("permission denied", "outside_allowed_paths", "outside allowed paths")):
        return {"classification": "policy_violation", "rule_id": "policy-boundary", "evidence_excerpt": excerpt}
    if any(marker in lowered for marker in ("no such file", "module not found", "command not found", "configuration")):
        return {"classification": "harness_or_configuration", "rule_id": "harness-configuration", "evidence_excerpt": excerpt}
    if any(marker in lowered for marker in ("selector", "browser", "webdriver", "playwright", "puppeteer")):
        return {"classification": "indeterminate", "rule_id": "conservative-default", "evidence_excerpt": excerpt}
    if any(marker in lowered for marker in ("assertionerror", "assertion failed", "failed")):
        return {"classification": "product", "rule_id": "product-assertion", "evidence_excerpt": excerpt}
    return {"classification": "indeterminate", "rule_id": "conservative-default", "evidence_excerpt": excerpt}


@dataclass(frozen=True)
class FeatureRunResult:
    """Terminal semantic, Git, and audit outcome for one FeatureRun."""

    status: str
    contract: RunContract
    dispatch: CoordinatorDispatchResult
    run_view: Mapping[str, object]
    git_receipts: tuple[Mapping[str, object], ...]
    manifest: Mapping[str, object]
    run_dir: Path
    worktree_path: Path
    review_fix: ReviewFixResult | None = None
    verification: DeterministicVerificationResult | None = None
    seal_receipt: Mapping[str, object] | None = None

    @property
    def candidate_commit(self) -> str | None:
        """Return the sealed candidate named by the controller's Git receipt."""

        for receipt in reversed(self.git_receipts):
            if receipt.get("operation") == "commit":
                candidate = receipt.get("candidate_commit")
                return candidate if isinstance(candidate, str) else None
        return None

    def outcome_evidence(self) -> dict[str, object]:
        """Build the canonical PlanGraph outcome evidence for this run.

        One source of truth for what a launcher must hand back to the graph:
        the verification facts the retry-budget ledger accounts against, and
        the review-fix record the graph reads transferred findings and — for a
        blocked node — still-open findings out of.  A launcher that omits the
        review-fix half silently drops both.
        """

        evidence: dict[str, object] = {}
        if self.verification is not None:
            evidence["verification"] = {
                "command_attempts": list(self.verification.command_attempts),
                "repair_invocation_ids": list(
                    self.verification.repair_invocation_ids
                ),
                "repair_invocations": list(self.verification.repair_invocations),
            }
        if self.review_fix is not None:
            evidence["review_fix"] = self.review_fix.as_dict()
        return evidence

    @property
    def canonical_manifest_ref(self) -> str:
        """Content address for the terminal manifest that owns this outcome."""

        manifest_hash = self.manifest.get("manifest_hash")
        if not isinstance(manifest_hash, str):
            raise ValueError("FeatureRun terminal manifest has no manifest hash")
        return f"artifact:sha256:{manifest_hash}"


def run_feature_worktree(
    *,
    base_repository: Path,
    base_branch: str,
    feature_branch: str,
    worktree_path: Path,
    run_dir: Path,
    contract_factory: FeatureContractFactory,
    schema: CoordinatorDispatchSchema,
    session_factory: FeatureSessionFactory,
    profile_builder: FeatureProfileBuilder,
    allowed_paths: tuple[str, ...],
    commit_message: str,
    merge: bool = False,
    base_commit: str | None = None,
    candidate_only: bool = False,
    review_fix_executor_factory: ReviewFixExecutorFactory | None = None,
    review_fix_policy: ReviewFixPolicy = ReviewFixPolicy(enabled=False),
    review_finding_obligations: tuple[Mapping[str, object], ...] = (),
    review_finding_transfer_targets: Mapping[str, str] | None = None,
    review_origin_node_id: str = "",
    review_inherited_ledger_frozen: bool = False,
    verification_argv: tuple[str, ...] = (),
    verification_gates: tuple[VerificationGate, ...] = (),
    verification_repair_executor_factory: (
        VerificationRepairExecutorFactory | None
    ) = None,
    verification_repair_limit: int = 1,
    verification_timeout_seconds: float | None = 1200,
    verification_gate_slot: AbstractContextManager[None] | None = None,
    recovery_agent: RecoveryAgent | None = deterministic_recovery_agent,
    recovery_limit: int = 3,
    continuation_recovery_limit: int = 2,
    evidence_classification: str = "production_lifecycle",
    initial_evidence: tuple[FeatureRunHandoffArtifact, ...] = (),
    descriptor_correlation: Mapping[str, str] | None = None,
    descriptor_plan: Mapping[str, str] | None = None,
) -> FeatureRunResult:
    """Create, execute, commit, and optionally merge one isolated FeatureRun.

    ``recovery_limit`` bounds general recoveries -- infrastructure transients
    and every other abnormal stage ending.  ``continuation_recovery_limit``
    bounds review continuations separately, so a run whose backend dropped
    three streams still reaches its first continuation with budget to spend;
    set it to 0 to refuse continuations outright.  The two together are the
    run's total ceiling.
    """

    if review_fix_policy.enabled and review_fix_executor_factory is None:
        raise ValueError("enabled review_fix_policy requires an executor factory")
    if verification_argv and verification_gates:
        raise ValueError(
            "verification_argv and verification_gates are mutually exclusive"
        )
    has_verification = bool(verification_argv) or bool(verification_gates)
    if has_verification:
        if any("verify" in segment.phases for segment in schema.segments):
            raise ValueError(
                "controller-owned verification cannot be combined with a "
                "coordinator verify phase"
            )
        if verification_argv:
            if any(
                not isinstance(value, str) or not value for value in verification_argv
            ):
                raise ValueError("verification_argv must contain non-empty strings")
            if (
                verification_timeout_seconds is not None
                and verification_timeout_seconds <= 0
            ):
                raise ValueError(
                    "verification_timeout_seconds must be positive or None"
                )
        else:
            gate_names = [gate.name for gate in verification_gates]
            if len(gate_names) != len(set(gate_names)):
                raise ValueError("verification_gates must have unique names")
        if verification_repair_executor_factory is None:
            raise ValueError(
                "deterministic verification requires a repair executor factory"
            )
        if verification_repair_limit < 1:
            raise ValueError("verification_repair_limit must be positive")
    elif verification_repair_executor_factory is not None:
        raise ValueError(
            "verification repair requires verification_argv or verification_gates"
        )
    if recovery_limit < 1:
        raise ValueError("recovery_limit must be positive")
    if continuation_recovery_limit < 0:
        raise ValueError("continuation_recovery_limit must not be negative")
    handoff_kinds = [artifact.kind for artifact in initial_evidence]
    if len(set(handoff_kinds)) != len(handoff_kinds):
        raise ValueError("handoff artifact kinds must be unique")
    if candidate_only and merge:
        raise ValueError("candidate-only FeatureRun cannot merge")
    transaction = GitWorktreeTransaction.create(
        base_repository=base_repository,
        base_branch=base_branch,
        feature_branch=feature_branch,
        worktree_path=worktree_path,
        base_commit=base_commit,
    )
    creation = transaction.creation_receipt()
    contract = contract_factory(transaction.worktree_path, creation)
    _validate_repository_binding(contract, creation)
    gate_criterion_ids = tuple(
        sorted(
            str(item.get("id"))
            for item in contract.criteria
            if item.get("adjudication") == "deterministic_verification"
        )
    )
    if gate_criterion_ids and not verification_argv and not verification_gates:
        raise ValueError(
            "run-contract criteria declare deterministic-verification "
            "adjudication but no verification_argv or verification_gates was "
            "supplied: " + ", ".join(gate_criterion_ids)
        )
    audit = AuditJournal(
        run_dir,
        contract.run_id,
        actor=AuditActor("kernel", "controller_kernel"),
        evidence_classification=evidence_classification,
        # This process is the run's controller from here until the journal is
        # finalized, so it owns the run's liveness lease.  Without one the run
        # catalog reports every non-terminal FeatureRun as
        # ``liveness_unavailable``, and a PlanGraph node inherits that state
        # from its correlated child.
        controller_kind="feature_run",
    )
    # Bind a run descriptor so the catalog can correlate this run (dashboard
    # metrics join graph nodes to FeatureRuns through parent_correlation);
    # without it the run projects as an uncorrelated legacy record.
    descriptor_raw = (
        json.dumps(
            {
                "protocol": "harness-run-descriptor/1",
                "run_kind": "feature_run",
                "run_id": contract.run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "objective": contract.objective,
                "evidence_classification": evidence_classification,
                "repository": {
                    "path": str(transaction.worktree_path),
                    "base_branch": base_branch,
                    "base_commit": str(creation["base_commit"]),
                },
                "approved_plan": dict(descriptor_plan) if descriptor_plan else None,
                "parent_correlation": (
                    dict(descriptor_correlation) if descriptor_correlation else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    (audit.run_dir / "descriptor.json").write_bytes(descriptor_raw)
    audit.append(
        "run_descriptor_bound",
        status="succeeded",
        payload={"descriptor_sha256": hashlib.sha256(descriptor_raw).hexdigest()},
    )
    evidence = EvidenceCatalog(audit=audit)
    if verification_repair_executor_factory is not None:
        verification_repair_executor_factory = _grant_aware_repair_factory(
            verification_repair_executor_factory,
            evidence=evidence,
            worktree_path=transaction.worktree_path,
            audit=audit,
        )
    if review_fix_executor_factory is not None:
        review_fix_executor_factory = _grant_aware_review_fix_factory(
            review_fix_executor_factory,
            evidence=evidence,
            worktree_path=transaction.worktree_path,
            audit=audit,
        )
    recovery = _RecoveryState(recovery_limit, continuation_recovery_limit)
    handoff_records = []
    for handoff in initial_evidence:
        record = evidence.add(
            kind=handoff.kind,
            content=handoff.content,
            media_type=handoff.media_type,
            producer_task_id=handoff.producer_task_id,
        )
        audit.append(
            "feature_run_handoff_bound",
            status="succeeded",
            payload={"kind": handoff.kind, "evidence_ref": record.ref},
            actor=AuditActor("plan-graph", "parent_controller"),
        )
        handoff_records.append(record.as_dict())
    creation_artifact = evidence.add(
        kind="git-worktree-receipt",
        content=creation,
        media_type="application/json",
        producer_task_id="integration-owner",
    )
    audit.append(
        "git_worktree_created",
        status="succeeded",
        payload={**creation, "evidence_ref": creation_artifact.ref},
        actor=AuditActor("integration-owner", "integration_owner"),
    )
    kernel = ControllerKernel(
        contract,
        evidence=evidence,
        audit=audit,
        initial_artifacts=handoff_records,
    )
    scheduler = CapabilityScheduler(
        profile_builder(transaction.worktree_path, evidence)
    )
    dispatcher = CoordinatorDispatcher(
        kernel,
        evidence,
        scheduler,
        schema,
        lambda launch, catalog: session_factory(
            transaction.worktree_path,
            launch,
            catalog,
        ),
    )
    while True:
        try:
            dispatch = dispatcher.run()
        except InterruptedError as exc:
            if not _recover_abnormal(
                agent=recovery_agent,
                recovery=recovery,
                audit=audit,
                contract=contract,
                worktree_path=transaction.worktree_path,
                allowed_paths=allowed_paths,
                stage="dispatch",
                condition="interrupted",
                reason=str(exc) or "coordinator dispatch interrupted",
            ):
                dispatch = dispatcher._result(None)
                break
            if not dispatcher.recover_interrupted_state():
                dispatch = dispatcher._result(None)
                break
            continue
        if dispatch.result.status not in {"blocked", "failed", "interrupted"}:
            break
        if not _recover_abnormal(
            agent=recovery_agent,
            recovery=recovery,
            audit=audit,
            contract=contract,
            worktree_path=transaction.worktree_path,
            allowed_paths=allowed_paths,
            stage="dispatch",
            condition=dispatch.result.status,
            reason=_dispatch_reason(dispatch, kernel),
        ):
            break
        if project_run_view(kernel)["status"] == "blocked":
            _resume_kernel_after_recovery(kernel, recovery.decisions[-1])
    receipts: list[Mapping[str, object]] = [creation]
    status = dispatch.result.status
    verification_result = None
    review_fix_result = None
    review_transfers: dict[str, Mapping[str, object]] = {}
    pre_review_workspace = None
    if status == "succeeded" and project_run_view(kernel)["status"] == "succeeded":
        if has_verification:
            assert verification_repair_executor_factory is not None
            # A graph-owned exclusive gate slot, when supplied by a PlanGraph
            # ready-set run, serializes this whole verification stage —
            # including its recovery retries and their recovery-agent
            # invocations below, which run for an unbounded duration —
            # across concurrently admitted siblings; dispatch above and
            # review/fix below stay parallel. Solo FeatureRuns and
            # max_parallelism=1 pass no slot, so `nullcontext()` makes this a
            # no-op identical to before.
            with (verification_gate_slot or nullcontext()):
                verification_result = _run_verification_stage(
                    run_id=contract.run_id,
                    objective=contract.objective,
                    acceptance_criteria=contract.criteria,
                    worktree_path=transaction.worktree_path,
                    allowed_paths=allowed_paths,
                    verification_argv=verification_argv,
                    verification_gates=verification_gates,
                    repair_executor_factory=verification_repair_executor_factory,
                    repair_limit=verification_repair_limit,
                    timeout_seconds=verification_timeout_seconds,
                    evidence=evidence,
                    audit=audit,
                )
                status = verification_result.status
                while status in {"blocked", "failed", "interrupted"}:
                    if not _recover_abnormal(
                        agent=recovery_agent,
                        recovery=recovery,
                        audit=audit,
                        contract=contract,
                        worktree_path=transaction.worktree_path,
                        allowed_paths=allowed_paths,
                        stage="verification",
                        condition=status,
                        reason=verification_result.reason,
                    ):
                        break
                    retried_verification = _run_verification_stage(
                        run_id=contract.run_id,
                        objective=contract.objective,
                        acceptance_criteria=contract.criteria,
                        worktree_path=transaction.worktree_path,
                        allowed_paths=allowed_paths,
                        verification_argv=verification_argv,
                        verification_gates=verification_gates,
                        repair_executor_factory=verification_repair_executor_factory,
                        repair_limit=verification_repair_limit,
                        timeout_seconds=verification_timeout_seconds,
                        evidence=evidence,
                        audit=audit,
                        stage="recovery",
                    )
                    verification_result = _combine_verification_results(
                        verification_result,
                        retried_verification,
                    )
                    status = retried_verification.status
    if status == "succeeded" and project_run_view(kernel)["status"] == "succeeded":
        if review_fix_policy.enabled:
            assert review_fix_executor_factory is not None
            snapshot = workspace_snapshot(transaction.worktree_path)
            pre_review_workspace = snapshot
            review_loop = ReviewFixLoop(
                run_id=contract.run_id,
                objective=contract.objective,
                acceptance_criteria=contract.criteria,
                allowed_paths=allowed_paths,
                changed_paths=tuple(snapshot["changed_paths"]),
                executor_factory=review_fix_executor_factory,
                evidence=evidence,
                audit=audit,
                policy=review_fix_policy,
                inherited_findings=review_finding_obligations,
                retained_transfers=(),
                finding_transfer_targets=review_finding_transfer_targets or {},
                origin_node_id=review_origin_node_id,
                inherited_ledger_frozen=review_inherited_ledger_frozen,
            )
            review_fix_result = review_loop.run()
            _remember_review_transfers(
                review_transfers, review_fix_result.transferred_findings
            )
            status = review_fix_result.status
            while status in {"blocked", "failed", "interrupted"}:
                if not _recover_abnormal(
                    agent=recovery_agent,
                    recovery=recovery,
                    audit=audit,
                    contract=contract,
                    worktree_path=transaction.worktree_path,
                    allowed_paths=allowed_paths,
                    stage="review",
                    condition=status,
                    reason=review_fix_result.reason,
                    detail=_review_stage_detail(review_loop, review_fix_result),
                ):
                    break
                # Continue the same ledger rather than restarting discovery:
                # the recovered loop resumes after the cycle that stopped and
                # spends an explicit additional cycle grant on findings whose
                # identity, fix attempts, and dispositions all carry over.
                # Only a ledger the loop never produced (a loop that raised
                # before its first checkpoint) forces a cold restart.
                resumed = (
                    review_loop.ledger if review_fix_result.cycles >= 1 else None
                )
                review_loop = ReviewFixLoop(
                    run_id=contract.run_id,
                    objective=contract.objective,
                    acceptance_criteria=contract.criteria,
                    allowed_paths=allowed_paths,
                    changed_paths=tuple(
                        workspace_snapshot(transaction.worktree_path)["changed_paths"]
                    ),
                    executor_factory=review_fix_executor_factory,
                    evidence=evidence,
                    audit=audit,
                    policy=review_fix_policy,
                    inherited_findings=(
                        () if resumed is not None else review_finding_obligations
                    ),
                    retained_transfers=(
                        ()
                        if resumed is not None
                        else tuple(
                            review_transfers[key] for key in sorted(review_transfers)
                        )
                    ),
                    finding_transfer_targets=review_finding_transfer_targets or {},
                    origin_node_id=review_origin_node_id,
                    inherited_ledger_frozen=review_inherited_ledger_frozen,
                    resumed_ledger=resumed,
                    resume_from_cycle=(
                        review_fix_result.cycles if resumed is not None else 0
                    ),
                    additional_cycles=(
                        review_fix_policy.continuation_cycles
                        if resumed is not None
                        else 0
                    ),
                )
                review_fix_result = review_loop.run()
                _remember_review_transfers(
                    review_transfers, review_fix_result.transferred_findings
                )
                status = review_fix_result.status
            review_fix_result = replace(
                review_fix_result,
                transferred_findings=tuple(
                    review_transfers[key] for key in sorted(review_transfers)
                ),
            )
    if (
        status == "succeeded"
        and project_run_view(kernel)["status"] == "succeeded"
        and has_verification
        and pre_review_workspace is not None
        and workspace_snapshot(transaction.worktree_path) != pre_review_workspace
    ):
        assert verification_repair_executor_factory is not None
        post_review = _run_verification_stage(
            run_id=contract.run_id,
            objective=contract.objective,
            acceptance_criteria=contract.criteria,
            worktree_path=transaction.worktree_path,
            allowed_paths=allowed_paths,
            verification_argv=verification_argv,
            verification_gates=verification_gates,
            repair_executor_factory=verification_repair_executor_factory,
            repair_limit=verification_repair_limit,
            timeout_seconds=verification_timeout_seconds,
            evidence=evidence,
            audit=audit,
            stage="post_review_repair",
        )
        verification_result = _combine_verification_results(
            verification_result,
            post_review,
        )
        status = post_review.status
        while status in {"blocked", "failed", "interrupted"}:
            if not _recover_abnormal(
                agent=recovery_agent,
                recovery=recovery,
                audit=audit,
                contract=contract,
                worktree_path=transaction.worktree_path,
                allowed_paths=allowed_paths,
                stage="post_review_verification",
                condition=status,
                reason=post_review.reason,
            ):
                break
            retried_post_review = _run_verification_stage(
                run_id=contract.run_id,
                objective=contract.objective,
                acceptance_criteria=contract.criteria,
                worktree_path=transaction.worktree_path,
                allowed_paths=allowed_paths,
                verification_argv=verification_argv,
                verification_gates=verification_gates,
                repair_executor_factory=verification_repair_executor_factory,
                repair_limit=verification_repair_limit,
                timeout_seconds=verification_timeout_seconds,
                evidence=evidence,
                audit=audit,
                stage="post_review_recovery",
            )
            verification_result = _combine_verification_results(
                verification_result,
                retried_post_review,
            )
            post_review = retried_post_review
            status = post_review.status
    if (
        status == "succeeded"
        and project_run_view(kernel)["status"] == "succeeded"
        and verification_result is not None
        and verification_result.status == "succeeded"
    ):
        _promote_gate_criteria(kernel, verification_result)
    if status == "succeeded" and project_run_view(kernel)["status"] == "succeeded":
        while True:
            try:
                if transaction.candidate_commit is None:
                    commit = transaction.commit_candidate(
                        allowed_paths=allowed_paths,
                        message=commit_message,
                    )
                    receipts.append(commit)
                    _record_git_receipt(audit, evidence, commit)
                if not candidate_only:
                    integration = transaction.integrate(merge=merge)
                    receipts.append(integration)
                    _record_git_receipt(audit, evidence, integration)
                break
            except (GitTransactionError, InterruptedError) as exc:
                condition = (
                    "interrupted" if isinstance(exc, InterruptedError) else "failed"
                )
                status = condition
                audit.append(
                    "git_transaction_failed",
                    status=condition,
                    payload={"error": str(exc)},
                    actor=AuditActor("integration-owner", "integration_owner"),
                )
                if _recover_abnormal(
                    agent=recovery_agent,
                    recovery=recovery,
                    audit=audit,
                    contract=contract,
                    worktree_path=transaction.worktree_path,
                    allowed_paths=allowed_paths,
                    stage="integration",
                    condition=condition,
                    reason=str(exc),
                ):
                    status = "succeeded"
                    continue
                dispatch = CoordinatorDispatchResult(
                    TaskResult(
                        attempt_id=f"{contract.run_id}/integration-owner",
                        status="failed",
                        payload={
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    ),
                    dispatch.launches,
                )
                break

    view = project_run_view(kernel)
    if (
        view["status"] == "succeeded"
        and status != "succeeded"
        and verification_result is not None
    ):
        _record_gate_verification_failure(kernel, verification_result)
        view = project_run_view(kernel)
    terminal_status = (
        "succeeded"
        if status == "succeeded" and view["status"] == "succeeded"
        else "blocked"
        if status == "blocked" or view["status"] == "blocked"
        else "failed"
    )
    review_fix_payload = (
        review_fix_result.as_dict() if review_fix_result is not None else None
    )
    verification_payload = (
        verification_result.as_dict() if verification_result is not None else None
    )
    manifest = audit.finalize(
        terminal_status,
        result={
            "dispatcher_result": {
                "attempt_id": dispatch.result.attempt_id,
                "status": dispatch.result.status,
                "payload": dict(dispatch.result.payload),
            },
            "run_view": view,
            "state_digest": kernel.state_digest(),
            "git_receipts": list(receipts),
            "verification": verification_payload,
            "review_fix": review_fix_payload,
            "recovery_decisions": list(recovery.decisions),
        },
        state={
            "controller": kernel.snapshot(),
            "verification": verification_payload,
            "review_fix": review_fix_payload,
            "recovery": {
                "decisions": list(recovery.decisions),
            },
        },
    )
    return FeatureRunResult(
        terminal_status,
        contract,
        dispatch,
        view,
        tuple(receipts),
        manifest,
        run_dir,
        transaction.worktree_path,
        review_fix_result,
        verification_result,
    )


def _review_stage_detail(
    loop: ReviewFixLoop,
    result: ReviewFixResult,
) -> Mapping[str, object]:
    """Summarize a stopped review loop for the recovery agent.

    Carries the loop's own ``stop_reason`` verdict -- the field the default
    policy decides on -- plus the cycle history and open findings an agent
    with a different policy would need to decide for itself.
    """

    ledger = loop.ledger
    cycles = list(ledger.cycles) if ledger is not None else []
    return {
        "protocol": "review-stage-detail/1",
        "status": result.status,
        "reason": result.reason,
        "stop_reason": result.stop_reason,
        "risk_tier": result.risk_tier,
        "cycles_spent": result.cycles,
        "cycle_limit": loop.cycle_budget(result.risk_tier),
        "continuation_cycles_granted": loop.additional_cycles,
        "resumed_from_cycle": loop.resume_from_cycle,
        "open_finding_keys": list(result.open_finding_keys),
        "open_required_finding_keys": (
            list(ledger.open_required()) if ledger is not None else []
        ),
        "technical_debt_keys": list(result.technical_debt_keys),
        "cycle_history": [
            {
                "cycle": entry.get("cycle"),
                "yield": entry.get("yield"),
                "addressed_finding_keys": list(
                    entry.get("addressed_finding_keys", ())
                ),
                "verified_finding_keys": list(entry.get("verified_finding_keys", ())),
            }
            for entry in cycles
        ],
        "findings_discharged": sum(
            len(entry.get("verified_finding_keys", ())) for entry in cycles
        ),
    }


def _remember_review_transfers(
    retained: dict[str, Mapping[str, object]],
    transfers: tuple[Mapping[str, object], ...],
) -> None:
    """Retain transfer records across a recovered review-loop replacement."""

    for transfer in transfers:
        key = str(transfer.get("key", ""))
        if not key:
            raise ValueError("review transfer record requires a stable finding key")
        retained[key] = dict(transfer)


def run_plan_graph_feature_worktree(
    *,
    binding: PlanGraphFeatureRunBinding,
    schema: CoordinatorDispatchSchema,
    contract_factory: FeatureContractFactory,
    review_fix_policy: ReviewFixPolicy,
    **feature_run_options: object,
) -> FeatureRunResult:
    """Run normal FeatureRun machinery with only orient and plan pre-satisfied.

    This is a launch profile over :func:`run_feature_worktree`, not a second
    lifecycle engine.  The approved PlanGraph packet becomes the normal planning
    handoff, while verification, ledger-backed review/fix, Git custody, recovery,
    and reporting continue through the existing FeatureRun implementation.
    """

    repository = feature_run_options.get("base_repository")
    if not isinstance(repository, Path):
        raise ValueError("PlanGraph-bound FeatureRun requires base_repository")
    approved_plan = subprocess.run(
        ["git", "show", f"{binding.plan_base_commit}:{binding.plan}"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if approved_plan.returncode:
        error = approved_plan.stderr.decode("utf-8", "replace").strip()
        raise ValueError(
            error or "PlanGraph-bound FeatureRun could not read its registered plan"
        )
    if hashlib.sha256(approved_plan.stdout).hexdigest() != binding.plan_sha256:
        raise ValueError("PlanGraph-bound FeatureRun registered plan hash mismatch")

    phases = tuple(phase for segment in schema.segments for phase in segment.phases)
    if phases != _NORMAL_FEATURE_PHASES:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires the normal seven-phase schema"
        )
    if schema.segments[0].phases != ("orient", "plan"):
        raise ValueError(
            "PlanGraph-bound FeatureRun may omit only the orient-plan segment"
        )
    required_review_guards = (
        review_fix_policy.enabled,
        review_fix_policy.ledger_enabled,
        review_fix_policy.scope_expansion_guard_enabled,
        review_fix_policy.targeted_verification_enabled,
        review_fix_policy.regression_review_enabled,
        review_fix_policy.cycle_limit_enabled,
    )
    if not all(required_review_guards):
        raise ValueError(
            "PlanGraph-bound FeatureRun requires the normal ledger-backed review guards"
        )
    reserved = {
        "schema",
        "contract_factory",
        "review_fix_policy",
        "initial_evidence",
        "allowed_paths",
        "verification_argv",
        "verification_timeout_seconds",
        "verification_gates",
    }
    if binding.is_child_lane:
        reserved.update({"base_commit", "candidate_only"})
    overlap = sorted(reserved.intersection(feature_run_options))
    if overlap:
        raise ValueError(
            "PlanGraph-bound FeatureRun options override controller-owned values: "
            + ", ".join(overlap)
        )

    implementation_segments = tuple(
        segment for segment in schema.segments if segment.phases == ("implement",)
    )
    if len(implementation_segments) != 1:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires one normal implement segment"
        )
    if feature_run_options.get("verification_repair_executor_factory") is None:
        raise ValueError(
            "PlanGraph-bound FeatureRun requires normal verification recovery"
        )
    if "recovery_agent" not in feature_run_options:
        # Without an agent every `_recover_abnormal` call site is inert, so a
        # review loop that exhausts its cycles blocks the node and the next
        # graph attempt re-runs implementation and verification from scratch.
        # Bind the composed default; a launcher may pass its own agent, or
        # `recovery_agent=None` to keep the old block-immediately behaviour.
        #
        # It must be the *composed* agent, not the continuation policy alone.
        # `run_feature_worktree`'s own default is deterministic_recovery_agent
        # (transient retry), and binding the continuation policy here would
        # silently take that away from every PlanGraph campaign that does not
        # pass an agent explicitly -- an infrastructure blip that costs a
        # bounded retry today would instead block the node and burn a whole
        # graph attempt. The two policies cover disjoint conditions; the
        # PlanGraph path needs both.
        from harness_labs.featurerun.feature_run_policy import (
            standard_composed_recovery_agent,
        )

        feature_run_options["recovery_agent"] = standard_composed_recovery_agent()
    if binding.is_child_lane:
        supplied_branch = feature_run_options.get("feature_branch")
        if supplied_branch != binding.lane_branch:
            raise ValueError(
                "PlanGraph child FeatureRun feature_branch must match its allocated lane_branch"
            )
        if feature_run_options.get("merge", False):
            raise ValueError("PlanGraph child FeatureRun cannot merge shared integration state")
        supplied_worktree = feature_run_options.get("worktree_path")
        assert binding.lane_worktree is not None
        if not isinstance(supplied_worktree, Path) or (
            supplied_worktree.resolve() != binding.lane_worktree.resolve()
        ):
            raise ValueError(
                "PlanGraph child FeatureRun worktree_path must match its allocated lane_worktree"
            )
        assert binding.writable_paths is not None
        if normalize_allowed_paths(binding.allowed_paths) != normalize_allowed_paths(
            binding.writable_paths
        ):
            raise ValueError(
                "PlanGraph child FeatureRun allowed_paths must match its allocated writable_paths"
            )

    bound_implementation = replace(
        implementation_segments[0],
        instructions=(
            "Implement or repair the accepted PlanGraph node from its frozen "
            "handoff and produce the implementation summary. Dispatch only "
            "implementation or implementation-repair tasks. Do not dispatch a "
            "verification-only task or require a worker to run the declared "
            "deterministic verification command; the parent FeatureRun owns and "
            "runs that gate immediately after this segment completes."
        ),
    )
    bound_schema = CoordinatorDispatchSchema(
        schema_id=f"{schema.schema_id}/plan-graph-bound",
        segments=(bound_implementation,),
    )
    bound_phases = ("implement",)

    def bound_contract_factory(
        worktree: Path, creation: Mapping[str, object]
    ) -> RunContract:
        contract = contract_factory(worktree, creation)
        if contract.phases != _NORMAL_FEATURE_PHASES:
            raise ValueError(
                "PlanGraph-bound FeatureRun contract must start as a normal FeatureRun"
            )
        if contract.objective != binding.objective:
            raise ValueError("PlanGraph binding objective does not match FeatureRun")
        if contract.criteria != binding.acceptance_criteria:
            raise ValueError(
                "PlanGraph binding acceptance criteria do not match FeatureRun"
            )
        return replace(contract, phases=bound_phases)

    child_options: dict[str, object] = {}
    if binding.is_child_lane:
        assert binding.parent_candidate_commit is not None
        child_options = {
            "base_commit": binding.parent_candidate_commit,
            "candidate_only": True,
            "merge": False,
        }

    result = run_feature_worktree(
        schema=bound_schema,
        contract_factory=bound_contract_factory,
        review_fix_policy=review_fix_policy,
        initial_evidence=binding.handoff_artifacts(),
        allowed_paths=binding.allowed_paths,
        review_finding_obligations=binding.finding_obligations,
        review_finding_transfer_targets=binding.finding_transfer_targets,
        review_origin_node_id=binding.origin_node_id or binding.plan_node_id,
        review_inherited_ledger_frozen=binding.inherited_ledger_frozen,
        verification_argv=binding.verification_argv,
        verification_timeout_seconds=binding.verification_timeout_seconds,
        verification_gates=binding.verification_gates,
        descriptor_correlation={
            "plan_graph_id": binding.plan_graph_id,
            "plan_node_id": binding.plan_node_id,
            "parent_run_id": binding.plan_graph_id,
        },
        descriptor_plan=(
            {"path": binding.plan, "sha256": binding.plan_sha256}
            if binding.plan and binding.plan_sha256
            else None
        ),
        **child_options,
        **feature_run_options,
    )
    if binding.is_child_lane and isinstance(result, FeatureRunResult):
        return replace(result, seal_receipt=_child_seal_receipt(binding, result))
    return result


def _child_seal_receipt(
    binding: PlanGraphFeatureRunBinding,
    result: FeatureRunResult,
) -> Mapping[str, object] | None:
    """Produce the sole child success-adoption input after a sealed candidate.

    This receipt is deliberately unavailable for failed or blocked child runs.
    Its allocation descriptor is a persisted handoff artifact; the remaining
    references are content addresses already retained in the FeatureRun audit.
    """

    if result.status != "succeeded" or result.candidate_commit is None:
        return None
    if result.verification is None or result.verification.status != "succeeded":
        return None
    verification_ref = _last_verification_evidence_ref(result.verification)
    candidate_receipt_ref = _candidate_receipt_ref(
        result.git_receipts,
        parent_candidate_commit=binding.parent_candidate_commit,
        candidate_commit=result.candidate_commit,
        writable_paths=binding.writable_paths,
    )
    manifest_hash = result.manifest.get("manifest_hash")
    terminal_event_hash = result.manifest.get("head_hash")
    if not all(
        _is_sha256(value)
        for value in (verification_ref, candidate_receipt_ref, manifest_hash, terminal_event_hash)
    ):
        raise ValueError("sealed PlanGraph child is missing canonical audit evidence")
    assert binding.parent_candidate_commit is not None
    assert binding.logical_attempt is not None
    assert binding.allocation_id is not None
    return {
        "protocol": "harness-plan-graph-parallel-seal-receipt/1",
        "status": "sealed",
        "graph_id": binding.plan_graph_id,
        "node_id": binding.plan_node_id,
        "logical_attempt": binding.logical_attempt,
        "allocation_id": binding.allocation_id,
        "parent_candidate_commit": binding.parent_candidate_commit,
        "candidate_commit": result.candidate_commit,
        "canonical_manifest_ref": f"artifact:sha256:{manifest_hash}",
        "descriptor_ref": _content_ref(binding.child_descriptor()),
        "verification_evidence_ref": f"artifact:sha256:{verification_ref}",
        "candidate_receipt_ref": f"artifact:sha256:{candidate_receipt_ref}",
        "terminal_journal_event_ref": f"artifact:sha256:{terminal_event_hash}",
    }


def _recover_abnormal(
    *,
    agent: RecoveryAgent | None,
    recovery: _RecoveryState,
    audit: AuditJournal,
    contract: RunContract,
    worktree_path: Path,
    allowed_paths: tuple[str, ...],
    stage: str,
    condition: RecoveryCondition,
    reason: str,
    detail: Mapping[str, object] | None = None,
) -> bool:
    """Ask for one bounded recovery decision and record the disposition.

    Every recorded decision carries the budget it was charged against and,
    when it stops, *why* it stopped: a stop the agent chose is not the same
    event as a stop the controller imposed because the budget ran out, and
    reading the two apart from a reason string alone is guesswork.
    """

    if agent is None:
        return False
    stage_detail = dict(detail or {})
    attempt = len(recovery.decisions) + 1
    recovery_class = _recovery_class(stage, condition, stage_detail)
    class_limit = recovery.class_limit(recovery_class)
    class_attempt = recovery.class_used(recovery_class) + 1
    budget = {
        "class": recovery_class,
        "class_attempt": class_attempt,
        "class_limit": class_limit,
        "total_attempt": attempt,
        "total_limit": recovery.total_limit,
    }
    workspace = _safe_workspace_snapshot(worktree_path)
    checkpoint = audit.merge_checkpoint(
        status="recovering",
        updates={
            "recovery": {
                "stage": stage,
                "condition": condition,
                "reason": reason,
                "attempt": attempt,
                "budget": budget,
                "decisions": list(recovery.decisions),
                "workspace": workspace,
                "stage_detail": stage_detail,
            }
        },
    )
    base_record: dict[str, object] = {
        "attempt": attempt,
        "stage": stage,
        "condition": condition,
        "blocked_reason": reason,
        "recovery_class": recovery_class,
        "budget": budget,
        "checkpoint_revision": checkpoint["revision"],
        "checkpoint_head_hash": checkpoint["head_hash"],
    }
    actor = AuditActor("recovery-agent", "recovery")

    def record(
        decision: RecoveryDecision,
        *,
        status: str | None = None,
        stop_cause: str | None = None,
    ) -> bool:
        value = {
            **base_record,
            **decision.as_dict(),
            "stop_cause": stop_cause if decision.action == "stop" else None,
        }
        recovery.decisions.append(value)
        audit.append(
            "recovery_decision",
            status=status or ("blocked" if decision.action == "stop" else "succeeded"),
            payload=value,
            actor=actor,
        )
        return decision.action != "stop"

    if class_attempt > class_limit:
        # Named per class so the evidence says which allowance ran out; a
        # continuation denied here was never shown to the policy at all.
        label = (
            "review-continuation recovery limit"
            if recovery_class == RECOVERY_CLASS_REVIEW_CONTINUATION
            else "recovery limit"
        )
        return record(
            RecoveryDecision("stop", f"{label} of {class_limit} exhausted"),
            stop_cause="budget_exhausted",
        )
    if attempt > recovery.total_limit:
        return record(
            RecoveryDecision(
                "stop",
                f"total recovery limit of {recovery.total_limit} exhausted",
            ),
            stop_cause="budget_exhausted",
        )

    context = RecoveryContext(
        run_id=contract.run_id,
        stage=stage,
        condition=condition,
        reason=reason,
        attempt=attempt,
        checkpoint=checkpoint,
        objective=contract.objective,
        acceptance_criteria=contract.criteria,
        worktree_path=str(worktree_path),
        allowed_paths=allowed_paths,
        workspace=workspace,
        prior_decisions=tuple(recovery.decisions),
        stage_detail=stage_detail,
        plan_adjustments=tuple(
            item["plan_adjustment"]
            for item in recovery.decisions
            if isinstance(item.get("plan_adjustment"), Mapping)
        ),
    )
    try:
        decision = agent(context)
        if not isinstance(decision, RecoveryDecision):
            raise TypeError("recovery agent must return RecoveryDecision")
    except Exception as exc:
        return record(
            RecoveryDecision(
                "stop",
                f"recovery agent failed: {type(exc).__name__}: {exc}",
            ),
            status="failed",
            stop_cause="agent_error",
        )

    proposal = decision.as_dict()
    if recovery.decisions and proposal == {
        key: recovery.decisions[-1].get(key)
        for key in ("action", "reason", "plan_adjustment")
    }:
        return record(
            RecoveryDecision(
                "stop",
                "recovery proposal repeated an unchanged strategy",
            ),
            stop_cause="repeated_strategy",
        )
    return record(decision, stop_cause="policy")


def _safe_workspace_snapshot(worktree_path: Path) -> Mapping[str, object]:
    try:
        return workspace_snapshot(worktree_path)
    except Exception as exc:
        return {"error": str(exc), "error_type": type(exc).__name__}


def _dispatch_reason(
    dispatch: CoordinatorDispatchResult,
    kernel: ControllerKernel,
) -> str:
    blocker = project_run_view(kernel).get("blocker")
    if isinstance(blocker, str) and blocker:
        return blocker
    last = dispatch.result.payload.get("last_coordinator_result")
    if isinstance(last, Mapping):
        payload = last.get("payload")
        if isinstance(payload, Mapping):
            value = payload.get("error") or payload.get("text")
            if isinstance(value, str) and value:
                return value
    return f"dispatcher ended with status {dispatch.result.status}"


def _resume_kernel_after_recovery(
    kernel: ControllerKernel,
    decision: Mapping[str, object],
) -> None:
    attempt = int(decision["attempt"])
    receipt = kernel.handle(
        CommandEnvelope(
            command_id=(
                f"{kernel.contract.run_id}/recovery-agent/run.resume/{attempt}"
            ),
            run_id=kernel.contract.run_id,
            type="run.resume",
            actor=CommandActor("recovery-agent", "operator"),
            expected_revision=kernel.revision,
            idempotency_key=(
                f"{kernel.contract.run_id}/recovery-agent/resume/{attempt}"
            ),
            payload={"reason": str(decision["reason"])},
        )
    )
    if not receipt.accepted:
        raise ValueError(f"recovery could not resume run: {receipt.message}")


def _dirty_baseline_receipt_ref(
    evidence: EvidenceCatalog,
    dirty_paths: list[str],
    dirty_files: Mapping[str, Any],
) -> tuple[str | None, DirtyBaselineGrantVerification | None]:
    """Return a workspace-change receipt covering every dirty path's path and content.

    A candidate receipt qualifies only when the shared
    :func:`~harness_labs.core.controller_live.verify_dirty_baseline_grant` accepts
    it -- changed-path coverage *and* per-file content-state match against
    ``dirty_files`` -- the same check the executor runs at preflight, so a
    grant issued from the selection here can never be journaled as granted
    against a workspace state that would fail preflight. Among qualifying
    receipts the tightest-covering one is preferred (fewest paths beyond
    what's dirty), ties broken by evidence ref for determinism, so a stale
    receipt from before further edits is passed over once a newer one covers
    everything.

    When no candidate qualifies, the second element carries the
    closest-covering candidate's failed verification (fewest uncovered and
    mismatched paths combined), or a receipt-less verification against every
    dirty path when the catalog holds no ``workspace-change-receipt`` at all
    -- so a caller can journal exactly which paths defeated the grant.
    """

    dirty = set(dirty_paths)
    if not dirty:
        return None, None
    best_ref: str | None = None
    best_extra: int | None = None
    best_failure: DirtyBaselineGrantVerification | None = None
    best_defects: int | None = None
    for record in evidence.list():
        if record.kind != "workspace-change-receipt":
            continue
        verification = verify_dirty_baseline_grant(
            evidence=evidence,
            grant={"receipt_ref": record.ref},
            dirty_paths=dirty_paths,
            dirty_files=dirty_files,
        )
        if verification.ok:
            extra = len(verification.receipted_paths) - len(dirty)
            if best_extra is None or extra < best_extra or (
                extra == best_extra and (best_ref is None or record.ref < best_ref)
            ):
                best_ref = record.ref
                best_extra = extra
        elif best_ref is None:
            defects = len(verification.uncovered_paths) + len(
                verification.mismatched_paths
            )
            if best_defects is None or defects < best_defects:
                best_failure = verification
                best_defects = defects
    if best_ref is not None:
        return best_ref, None
    if best_failure is not None:
        return None, best_failure
    return None, verify_dirty_baseline_grant(
        evidence=evidence,
        grant=None,
        dirty_paths=dirty_paths,
        dirty_files=dirty_files,
    )


def _attach_dirty_baseline_grant(
    executor: Executor,
    *,
    evidence: EvidenceCatalog,
    worktree_path: Path,
    audit: AuditJournal,
    attempt: TaskAttempt,
    actor: AuditActor,
) -> None:
    """Supply a repair/fix executor an audited grant for the current dirty baseline.

    Only executors that expose a settable ``dirty_baseline_grant`` attribute
    (the two live semantic executors) participate; anything else is left
    untouched. A grant is only ever attached when an existing
    ``workspace-change-receipt`` in this run's evidence catalog truthfully
    covers every path currently dirty *and* matches its on-disk content, per
    the shared ``verify_dirty_baseline_grant`` check -- the same check the
    executor re-runs at preflight -- so a grant journaled here as granted
    cannot then be refused there for the same workspace state, and that
    decision is recorded in the audit journal so the adoption is provable,
    not merely asserted. When no candidate qualifies, the decline is
    journaled too (status ``"refused"``, naming the uncovered and
    content-mismatched paths) so a workspace that drifted between a prior
    receipt and now is diagnosable from the journal instead of surfacing
    only as the executor's later generic clean-baseline refusal.
    """

    if not hasattr(executor, "dirty_baseline_grant"):
        return
    snapshot = workspace_snapshot(worktree_path)
    dirty_paths = list(snapshot["changed_paths"])
    if not dirty_paths:
        return
    receipt_ref, failure = _dirty_baseline_receipt_ref(
        evidence, dirty_paths, snapshot["files"]
    )
    if receipt_ref is None:
        if failure is not None:
            audit.append(
                "dirty_baseline_adoption_grant_supplied",
                status="refused",
                payload={
                    "dirty_paths": sorted(dirty_paths),
                    "uncovered_paths": list(failure.uncovered_paths),
                    "mismatched_paths": list(failure.mismatched_paths),
                },
                actor=actor,
                attempt_id=attempt.attempt_id,
            )
        return
    grant = {"receipt_ref": receipt_ref}
    executor.dirty_baseline_grant = grant
    audit.append(
        "dirty_baseline_adoption_grant_supplied",
        status="granted",
        payload={
            "receipt_ref": receipt_ref,
            "dirty_paths": sorted(dirty_paths),
        },
        actor=actor,
        attempt_id=attempt.attempt_id,
    )


def _grant_aware_repair_factory(
    factory: VerificationRepairExecutorFactory,
    *,
    evidence: EvidenceCatalog,
    worktree_path: Path,
    audit: AuditJournal,
) -> VerificationRepairExecutorFactory:
    """Wrap a repair executor factory to auto-supply the adoption grant.

    Converts the constructor-frozen ``allow_dirty_baseline`` escape hatch into
    a per-dispatch runtime grant: the factory owned outside this module still
    decides everything else about the repair executor, but every dispatch it
    produces is offered whatever receipted baseline this run's evidence
    catalog can prove covers the current dirty workspace.
    """

    def wrapped(attempt: TaskAttempt) -> Executor:
        executor = factory(attempt)
        _attach_dirty_baseline_grant(
            executor,
            evidence=evidence,
            worktree_path=worktree_path,
            audit=audit,
            attempt=attempt,
            actor=AuditActor("verification-owner", "verification_owner"),
        )
        return executor

    return wrapped


def _grant_aware_review_fix_factory(
    factory: ReviewFixExecutorFactory,
    *,
    evidence: EvidenceCatalog,
    worktree_path: Path,
    audit: AuditJournal,
) -> ReviewFixExecutorFactory:
    """Wrap a review-fix executor factory to auto-supply the adoption grant.

    Only the writable ``"fix"`` stage can face a dirty baseline; ``"review"``
    and ``"verify"`` stay read-only and are passed through unchanged.
    """

    def wrapped(stage: str, attempt: TaskAttempt) -> Executor:
        executor = factory(stage, attempt)
        if stage == "fix":
            _attach_dirty_baseline_grant(
                executor,
                evidence=evidence,
                worktree_path=worktree_path,
                audit=audit,
                attempt=attempt,
                actor=AuditActor("review-fix-controller", "controller"),
            )
        return executor

    return wrapped


def _run_verification_stage(
    *,
    run_id: str,
    objective: str,
    acceptance_criteria: tuple[Mapping[str, object], ...],
    worktree_path: Path,
    allowed_paths: tuple[str, ...],
    verification_argv: tuple[str, ...],
    verification_gates: tuple[VerificationGate, ...],
    repair_executor_factory: VerificationRepairExecutorFactory,
    repair_limit: int,
    timeout_seconds: float | None,
    evidence: EvidenceCatalog,
    audit: AuditJournal,
    stage: str = "post_implementation",
) -> DeterministicVerificationResult:
    """Route to the flat or gate-tuple verification loop for one call site.

    A declared gate tuple always takes this branch; the flat path below is
    untouched code exercised exactly as before, so a node declaring only
    ``verification_argv`` gets byte-identical events and budget accounting.
    """
    if verification_gates:
        return _verify_gates_with_recovery(
            run_id=run_id,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            worktree_path=worktree_path,
            allowed_paths=allowed_paths,
            gates=verification_gates,
            repair_executor_factory=repair_executor_factory,
            repair_limit=repair_limit,
            evidence=evidence,
            audit=audit,
            stage=stage,
        )
    return _verify_with_recovery(
        run_id=run_id,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        worktree_path=worktree_path,
        allowed_paths=allowed_paths,
        argv=verification_argv,
        repair_executor_factory=repair_executor_factory,
        repair_limit=repair_limit,
        timeout_seconds=timeout_seconds,
        evidence=evidence,
        audit=audit,
        stage=stage,
    )


def _verify_with_recovery(
    *,
    run_id: str,
    objective: str,
    acceptance_criteria: tuple[Mapping[str, object], ...],
    worktree_path: Path,
    allowed_paths: tuple[str, ...],
    argv: tuple[str, ...],
    repair_executor_factory: VerificationRepairExecutorFactory,
    repair_limit: int,
    timeout_seconds: float | None,
    evidence: EvidenceCatalog,
    audit: AuditJournal,
    stage: str = "post_implementation",
) -> DeterministicVerificationResult:
    command_attempts: list[Mapping[str, object]] = []
    repair_attempts = 0
    # Repair allowance actually charged: distinct from repair_attempts (the
    # dispatch count) so a renewal can give an allowance unit back without
    # making repair_attempts contradict repair_invocation_ids.
    repair_budget_consumed = 0
    repair_invocation_ids: list[str] = []
    repair_invocations: list[Mapping[str, object]] = []
    env_retries = 0
    # The failing-identifier set that motivated the most recently dispatched
    # repair; compared against each rerun's set to decide renewal. ``None``
    # can mean either "no repair dispatched yet" or "the dispatching rerun's
    # output was unparseable", so a separate flag disambiguates those cases
    # for the audit-emission guard below.
    previous_failing_ids: frozenset[str] | None = None
    awaiting_repair_delta = False
    runner = AttemptRunner()
    actor = AuditActor("verification-owner", "verification_owner")
    basetemp = _verification_basetemp(audit, stage)

    # Two environment-only retries are separate from the bounded repair budget.
    for ordinal in range(1, repair_limit + 4):
        command = _run_verification_command(
            worktree_path,
            argv,
            timeout_seconds,
            ordinal,
            stage,
            basetemp=basetemp,
        )
        artifact = evidence.add(
            kind="deterministic-verification-output",
            content=command,
            media_type="application/json",
            producer_task_id="verification-owner",
        )
        recorded = {
            **command,
            "evidence_ref": artifact.ref,
            "invocation_id": f"{run_id}:verification-command:{stage}:{ordinal}",
        }
        if command["exit_code"] != 0:
            recorded["failure"] = classify_verification_failure(command)
            _attach_failure_images(
                recorded,
                command=command,
                basetemp=basetemp,
                evidence=evidence,
                audit=audit,
                stage=stage,
                ordinal=ordinal,
            )
        command_attempts.append(recorded)
        audit.append(
            "deterministic_verification_completed",
            status="succeeded" if command["exit_code"] == 0 else "failed",
            payload=recorded,
            actor=actor,
        )
        if command["exit_code"] == 0:
            return DeterministicVerificationResult(
                "succeeded",
                "declared verification command passed",
                tuple(command_attempts),
                repair_attempts,
                tuple(repair_invocation_ids),
                tuple(repair_invocations),
            )
        failure = recorded.get("failure")
        if (
            isinstance(failure, Mapping)
            and failure.get("classification") == "infrastructure_transient"
            and env_retries < 2
        ):
            # Infrastructure retries consume neither a repair dispatch nor its
            # repair allowance; their invocation evidence remains explicit.
            env_retries += 1
            continue
        current_failing_ids = failing_identifiers(command)
        if awaiting_repair_delta:
            # A strict subset (not merely a smaller count) proves every
            # currently-failing test was already failing and at least one
            # previously-failing test is now gone; an unparseable baseline or
            # rerun, an equal/larger set, or a set with a new member are all
            # non-improving and must not renew the allowance.
            renewed = (
                current_failing_ids is not None
                and previous_failing_ids is not None
                and bool(current_failing_ids)
                and current_failing_ids < previous_failing_ids
            )
            audit.append(
                "deterministic_verification_repair_budget_delta",
                status="renewed" if renewed else "consumed",
                payload={
                    "repair_attempt": ordinal,
                    "previous_failing_ids": (
                        sorted(previous_failing_ids)
                        if previous_failing_ids is not None
                        else None
                    ),
                    "current_failing_ids": (
                        sorted(current_failing_ids)
                        if current_failing_ids is not None
                        else None
                    ),
                    "renewed": renewed,
                },
                actor=actor,
            )
            if renewed:
                # A repair that strictly shrank the observed failing set does
                # not spend the declared repair limit; give the unit back.
                repair_budget_consumed -= 1
        if repair_budget_consumed >= repair_limit:
            return DeterministicVerificationResult(
                "blocked",
                "declared verification command still fails after repair budget",
                tuple(command_attempts),
                repair_attempts,
                tuple(repair_invocation_ids),
                tuple(repair_invocations),
            )

        attempt = TaskAttempt(
            attempt_id=f"{run_id}/verification-repair/{ordinal}",
            task_ref="verification-repair",
            context_ref=artifact.ref,
            grant_ref="verification-repair-write-grant",
            context=json.dumps(
                {
                    "objective": objective,
                    "acceptance_criteria": list(acceptance_criteria),
                    "allowed_paths": list(allowed_paths),
                    "failed_verification": recorded,
                    "repair_attempt": ordinal,
                    "repair_limit": repair_limit,
                },
                sort_keys=True,
            ),
        )
        assert isinstance(failure, Mapping)
        classification = failure.get("classification")
        assert isinstance(classification, str)
        repair_invocation_id = f"{run_id}:verification-repair:{stage}:{ordinal}"
        # Record the dispatch before execution so an interrupted worker is still
        # visible to the parent ledger and can be idempotently reconciled.
        previous_failing_ids = current_failing_ids
        awaiting_repair_delta = True
        repair_attempts += 1
        repair_budget_consumed += 1
        repair_invocation_ids.append(repair_invocation_id)
        repair_invocations.append({
            "invocation_id": repair_invocation_id,
            "classification": classification,
            # Carried on the structured evidence so the parent ledger's
            # import_child_evidence sees the same failure_keys substrate
            # reserve() uses, instead of the loop being the only place that
            # knows which tests motivated this dispatch.
            "failure_keys": sorted(current_failing_ids) if current_failing_ids else [],
        })
        try:
            repair = runner.run(attempt, repair_executor_factory(attempt))
        except InterruptedError as exc:
            audit.append(
                "deterministic_verification_repair_completed",
                status="interrupted",
                payload={
                    "repair_attempt": ordinal,
                    "error": str(exc),
                    "failed_command_evidence_ref": artifact.ref,
                },
                actor=actor,
                attempt_id=attempt.attempt_id,
            )
            return DeterministicVerificationResult(
                "interrupted",
                str(exc) or "verification repair interrupted",
                tuple(command_attempts),
                repair_attempts,
                tuple(repair_invocation_ids),
                tuple(repair_invocations),
            )
        repaired_workspace = workspace_snapshot(worktree_path)
        prior_workspace = command["workspace"]
        assert isinstance(prior_workspace, Mapping)
        outside_scope = paths_outside_scope(
            repaired_workspace["changed_paths"],
            allowed_paths,
        )
        identity_changed = any(
            repaired_workspace[key] != prior_workspace[key]
            for key in ("head", "branch")
        )
        repair_status = (
            "failed"
            if outside_scope or identity_changed
            else repair.status
        )
        audit.append(
            "deterministic_verification_repair_completed",
            status=repair_status,
            payload={
                "repair_attempt": ordinal,
                "result": dict(repair.payload),
                "evidence_refs": list(repair.evidence),
                "failed_command_evidence_ref": artifact.ref,
                "workspace": repaired_workspace,
                "outside_allowed_paths": list(outside_scope),
                "repository_identity_changed": identity_changed,
            },
            actor=actor,
            attempt_id=attempt.attempt_id,
        )
        if repair_status != "succeeded":
            if (
                not outside_scope
                and not identity_changed
                and repair.status == "failed"
                and repair.payload.get("error")
                == "writable worker completed without changing the repository"
            ):
                recovery_attempt = TaskAttempt(
                    attempt_id=(
                        f"{run_id}/verification-repair/"
                        f"{ordinal}-recovery-1"
                    ),
                    task_ref="verification-repair",
                    context_ref=artifact.ref,
                    grant_ref="verification-repair-write-grant",
                    context=json.dumps(
                        {
                            "objective": objective,
                            "acceptance_criteria": list(acceptance_criteria),
                            "allowed_paths": list(allowed_paths),
                            "failed_verification": recorded,
                            "repair_attempt": ordinal,
                            "repair_limit": repair_limit,
                            "recovery": {
                                "attempt": 1,
                                "reason": repair.payload["error"],
                                "instruction": (
                                    "Use a changed implementation method for "
                                    "the same failed verification; preserve "
                                    "scope and candidate identity."
                                ),
                            },
                        },
                        sort_keys=True,
                    ),
                )
                audit.append(
                    "deterministic_verification_recovery_triggered",
                    status="recovering",
                    payload={
                        "repair_attempt": ordinal,
                        "recovery_attempt": 1,
                        "reason": repair.payload["error"],
                        "failed_command_evidence_ref": artifact.ref,
                    },
                    actor=actor,
                    attempt_id=recovery_attempt.attempt_id,
                )
                recovery_invocation_id = (
                    f"{run_id}:verification-repair:{stage}:{ordinal}:recovery-1"
                )
                repair_attempts += 1
                repair_budget_consumed += 1
                repair_invocation_ids.append(recovery_invocation_id)
                repair_invocations.append({
                    "invocation_id": recovery_invocation_id,
                    "classification": classification,
                    "failure_keys": sorted(current_failing_ids) if current_failing_ids else [],
                })
                recovery = runner.run(
                    recovery_attempt,
                    repair_executor_factory(recovery_attempt),
                )
                recovered_workspace = workspace_snapshot(worktree_path)
                recovery_outside = paths_outside_scope(
                    recovered_workspace["changed_paths"],
                    allowed_paths,
                )
                recovery_identity_changed = any(
                    recovered_workspace[key] != prior_workspace[key]
                    for key in ("head", "branch")
                )
                recovery_status = (
                    "failed"
                    if recovery_outside or recovery_identity_changed
                    else recovery.status
                )
                audit.append(
                    "deterministic_verification_repair_completed",
                    status=recovery_status,
                    payload={
                        "repair_attempt": ordinal,
                        "recovery_attempt": 1,
                        "result": dict(recovery.payload),
                        "evidence_refs": list(recovery.evidence),
                        "failed_command_evidence_ref": artifact.ref,
                        "workspace": recovered_workspace,
                        "outside_allowed_paths": list(recovery_outside),
                        "repository_identity_changed": recovery_identity_changed,
                    },
                    actor=actor,
                    attempt_id=recovery_attempt.attempt_id,
                )
                if recovery_status == "succeeded":
                    continue
            return DeterministicVerificationResult(
                "blocked",
                (
                    "verification repair escaped its grant"
                    if outside_scope or identity_changed
                    else f"verification repair {repair.status}"
                ),
                tuple(command_attempts),
                repair_attempts,
                tuple(repair_invocation_ids),
                tuple(repair_invocations),
            )

    # A renewed allowance can let the loop dispatch more repairs than the
    # non-delta-scoped design assumed, so the fixed iteration bound above can
    # now be reached while still mid-repair rather than only through the
    # defensive branch it was originally written for. That is a legitimate
    # exhaustion of the loop's own hard bound (AC-CB04-2), not a programming
    # error, so it must resolve to the same terminal `blocked` outcome every
    # other bound in this loop returns rather than escape as an exception.
    return DeterministicVerificationResult(
        "blocked",
        "verification did not converge within the loop's bounded iteration limit",
        tuple(command_attempts),
        repair_attempts,
        tuple(repair_invocation_ids),
        tuple(repair_invocations),
    )


def _verify_gates_with_recovery(
    *,
    run_id: str,
    objective: str,
    acceptance_criteria: tuple[Mapping[str, object], ...],
    worktree_path: Path,
    allowed_paths: tuple[str, ...],
    gates: tuple[VerificationGate, ...],
    repair_executor_factory: VerificationRepairExecutorFactory,
    repair_limit: int,
    evidence: EvidenceCatalog,
    audit: AuditJournal,
    stage: str = "post_implementation",
) -> DeterministicVerificationResult:
    """Run an ordered named-gate tuple with per-gate classification and repair.

    Each gate runs, is classified, and is evidenced independently — an
    ``infrastructure_transient`` failure on one gate resumes only that gate
    (no tree mutation) and never voids an earlier gate's passing evidence.
    A repair dispatch is scoped to the motivating gate's own evidence and
    failing-identifier delta (the same strict-subset renewal rule
    :func:`_verify_with_recovery` uses), but because a repair mutates the
    tree, the next re-verification always restarts at the first gate so a
    passing certification reflects one consistent tree state.
    """
    command_attempts: list[Mapping[str, object]] = []
    repair_attempts = 0
    repair_budget_consumed = 0
    repair_invocation_ids: list[str] = []
    repair_invocations: list[Mapping[str, object]] = []
    env_retries: dict[str, int] = {gate.name: 0 for gate in gates}
    previous_failing_ids: dict[str, frozenset[str] | None] = {}
    awaiting_repair_delta: dict[str, bool] = {}
    runner = AttemptRunner()
    actor = AuditActor("verification-owner", "verification_owner")
    basetemp = _verification_basetemp(audit, stage)

    gate_index = 0
    for full_attempt in range(1, repair_limit + 4):
        while gate_index < len(gates):
            gate = gates[gate_index]
            ordinal = len(command_attempts) + 1
            command = _run_verification_command(
                worktree_path,
                gate.argv,
                gate.timeout_seconds,
                ordinal,
                stage,
                basetemp=basetemp,
            )
            artifact = evidence.add(
                kind="deterministic-verification-output",
                content=command,
                media_type="application/json",
                producer_task_id="verification-owner",
            )
            recorded = {
                **command,
                "evidence_ref": artifact.ref,
                "invocation_id": f"{run_id}:verification-command:{stage}:{ordinal}",
                "gate": gate.name,
            }
            if command["exit_code"] != 0:
                recorded["failure"] = classify_verification_failure(command)
                _attach_failure_images(
                    recorded,
                    command=command,
                    basetemp=basetemp,
                    evidence=evidence,
                    audit=audit,
                    stage=stage,
                    ordinal=ordinal,
                )
            command_attempts.append(recorded)
            audit.append(
                "deterministic_verification_completed",
                status="succeeded" if command["exit_code"] == 0 else "failed",
                payload=recorded,
                actor=actor,
            )
            if command["exit_code"] == 0:
                gate_index += 1
                continue

            failure = recorded.get("failure")
            if (
                isinstance(failure, Mapping)
                and failure.get("classification") == "infrastructure_transient"
                and env_retries[gate.name] < 2
            ):
                # Infrastructure retries consume neither a repair dispatch nor
                # its allowance, and resume at exactly this gate — the tree
                # was never mutated, so earlier gates' passing evidence in
                # command_attempts above stands untouched.
                env_retries[gate.name] += 1
                continue

            current_failing_ids = failing_identifiers(command)
            if awaiting_repair_delta.get(gate.name):
                previous = previous_failing_ids.get(gate.name)
                renewed = (
                    current_failing_ids is not None
                    and previous is not None
                    and bool(current_failing_ids)
                    and current_failing_ids < previous
                )
                audit.append(
                    "deterministic_verification_repair_budget_delta",
                    status="renewed" if renewed else "consumed",
                    payload={
                        "repair_attempt": ordinal,
                        "gate": gate.name,
                        "previous_failing_ids": (
                            sorted(previous) if previous is not None else None
                        ),
                        "current_failing_ids": (
                            sorted(current_failing_ids)
                            if current_failing_ids is not None
                            else None
                        ),
                        "renewed": renewed,
                    },
                    actor=actor,
                )
                if renewed:
                    repair_budget_consumed -= 1
            if repair_budget_consumed >= repair_limit:
                return DeterministicVerificationResult(
                    "blocked",
                    "declared verification command still fails after repair budget",
                    tuple(command_attempts),
                    repair_attempts,
                    tuple(repair_invocation_ids),
                    tuple(repair_invocations),
                )

            attempt = TaskAttempt(
                attempt_id=f"{run_id}/verification-repair/{ordinal}",
                task_ref="verification-repair",
                context_ref=artifact.ref,
                grant_ref="verification-repair-write-grant",
                context=json.dumps(
                    {
                        "objective": objective,
                        "acceptance_criteria": list(acceptance_criteria),
                        "allowed_paths": list(allowed_paths),
                        "failed_verification": recorded,
                        "repair_attempt": ordinal,
                        "repair_limit": repair_limit,
                        "gate": gate.name,
                    },
                    sort_keys=True,
                ),
            )
            assert isinstance(failure, Mapping)
            classification = failure.get("classification")
            assert isinstance(classification, str)
            repair_invocation_id = f"{run_id}:verification-repair:{stage}:{ordinal}"
            previous_failing_ids[gate.name] = current_failing_ids
            awaiting_repair_delta[gate.name] = True
            repair_attempts += 1
            repair_budget_consumed += 1
            repair_invocation_ids.append(repair_invocation_id)
            repair_invocations.append({
                "invocation_id": repair_invocation_id,
                "classification": classification,
                "failure_keys": sorted(current_failing_ids) if current_failing_ids else [],
                "gate": gate.name,
            })
            try:
                repair = runner.run(attempt, repair_executor_factory(attempt))
            except InterruptedError as exc:
                audit.append(
                    "deterministic_verification_repair_completed",
                    status="interrupted",
                    payload={
                        "repair_attempt": ordinal,
                        "gate": gate.name,
                        "error": str(exc),
                        "failed_command_evidence_ref": artifact.ref,
                    },
                    actor=actor,
                    attempt_id=attempt.attempt_id,
                )
                return DeterministicVerificationResult(
                    "interrupted",
                    str(exc) or "verification repair interrupted",
                    tuple(command_attempts),
                    repair_attempts,
                    tuple(repair_invocation_ids),
                    tuple(repair_invocations),
                )
            repaired_workspace = workspace_snapshot(worktree_path)
            prior_workspace = command["workspace"]
            assert isinstance(prior_workspace, Mapping)
            outside_scope = paths_outside_scope(
                repaired_workspace["changed_paths"],
                allowed_paths,
            )
            identity_changed = any(
                repaired_workspace[key] != prior_workspace[key]
                for key in ("head", "branch")
            )
            repair_status = (
                "failed"
                if outside_scope or identity_changed
                else repair.status
            )
            audit.append(
                "deterministic_verification_repair_completed",
                status=repair_status,
                payload={
                    "repair_attempt": ordinal,
                    "gate": gate.name,
                    "result": dict(repair.payload),
                    "evidence_refs": list(repair.evidence),
                    "failed_command_evidence_ref": artifact.ref,
                    "workspace": repaired_workspace,
                    "outside_allowed_paths": list(outside_scope),
                    "repository_identity_changed": identity_changed,
                },
                actor=actor,
                attempt_id=attempt.attempt_id,
            )
            if repair_status != "succeeded":
                if (
                    not outside_scope
                    and not identity_changed
                    and repair.status == "failed"
                    and repair.payload.get("error")
                    == "writable worker completed without changing the repository"
                ):
                    recovery_attempt = TaskAttempt(
                        attempt_id=(
                            f"{run_id}/verification-repair/"
                            f"{ordinal}-recovery-1"
                        ),
                        task_ref="verification-repair",
                        context_ref=artifact.ref,
                        grant_ref="verification-repair-write-grant",
                        context=json.dumps(
                            {
                                "objective": objective,
                                "acceptance_criteria": list(acceptance_criteria),
                                "allowed_paths": list(allowed_paths),
                                "failed_verification": recorded,
                                "repair_attempt": ordinal,
                                "repair_limit": repair_limit,
                                "gate": gate.name,
                                "recovery": {
                                    "attempt": 1,
                                    "reason": repair.payload["error"],
                                    "instruction": (
                                        "Use a changed implementation method for "
                                        "the same failed verification; preserve "
                                        "scope and candidate identity."
                                    ),
                                },
                            },
                            sort_keys=True,
                        ),
                    )
                    audit.append(
                        "deterministic_verification_recovery_triggered",
                        status="recovering",
                        payload={
                            "repair_attempt": ordinal,
                            "gate": gate.name,
                            "recovery_attempt": 1,
                            "reason": repair.payload["error"],
                            "failed_command_evidence_ref": artifact.ref,
                        },
                        actor=actor,
                        attempt_id=recovery_attempt.attempt_id,
                    )
                    recovery_invocation_id = (
                        f"{run_id}:verification-repair:{stage}:{ordinal}:recovery-1"
                    )
                    repair_attempts += 1
                    repair_budget_consumed += 1
                    repair_invocation_ids.append(recovery_invocation_id)
                    repair_invocations.append({
                        "invocation_id": recovery_invocation_id,
                        "classification": classification,
                        "failure_keys": sorted(current_failing_ids) if current_failing_ids else [],
                        "gate": gate.name,
                    })
                    recovery = runner.run(
                        recovery_attempt,
                        repair_executor_factory(recovery_attempt),
                    )
                    recovered_workspace = workspace_snapshot(worktree_path)
                    recovery_outside = paths_outside_scope(
                        recovered_workspace["changed_paths"],
                        allowed_paths,
                    )
                    recovery_identity_changed = any(
                        recovered_workspace[key] != prior_workspace[key]
                        for key in ("head", "branch")
                    )
                    recovery_status = (
                        "failed"
                        if recovery_outside or recovery_identity_changed
                        else recovery.status
                    )
                    audit.append(
                        "deterministic_verification_repair_completed",
                        status=recovery_status,
                        payload={
                            "repair_attempt": ordinal,
                            "gate": gate.name,
                            "recovery_attempt": 1,
                            "result": dict(recovery.payload),
                            "evidence_refs": list(recovery.evidence),
                            "failed_command_evidence_ref": artifact.ref,
                            "workspace": recovered_workspace,
                            "outside_allowed_paths": list(recovery_outside),
                            "repository_identity_changed": recovery_identity_changed,
                        },
                        actor=actor,
                        attempt_id=recovery_attempt.attempt_id,
                    )
                    if recovery_status == "succeeded":
                        # A successful recovery is itself a tree mutation, so
                        # the full gate tuple must restart at the first gate.
                        gate_index = 0
                        break
                return DeterministicVerificationResult(
                    "blocked",
                    (
                        "verification repair escaped its grant"
                        if outside_scope or identity_changed
                        else f"verification repair {repair.status}"
                    ),
                    tuple(command_attempts),
                    repair_attempts,
                    tuple(repair_invocation_ids),
                    tuple(repair_invocations),
                )
            # A successful repair mutated the tree: AC-CB206-3 requires the
            # next re-verification to reflect one consistent tree state, so
            # restart at the first gate rather than resuming mid-tuple.
            gate_index = 0
            break
        else:
            # The while loop exhausted every gate without a break: each one
            # passed in this full attempt.
            return DeterministicVerificationResult(
                "succeeded",
                "declared verification gate tuple passed",
                tuple(command_attempts),
                repair_attempts,
                tuple(repair_invocation_ids),
                tuple(repair_invocations),
            )

    return DeterministicVerificationResult(
        "blocked",
        "verification did not converge within the loop's bounded iteration limit",
        tuple(command_attempts),
        repair_attempts,
        tuple(repair_invocation_ids),
        tuple(repair_invocations),
    )


def _run_verification_command(
    worktree_path: Path,
    argv: tuple[str, ...],
    timeout_seconds: float | None,
    ordinal: int,
    stage: str,
    basetemp: Path | None = None,
) -> dict[str, object]:
    # ``executed_argv`` may carry a controller-owned ``--basetemp`` so a pytest
    # run's images survive the process; ``argv`` below stays the declared
    # command verbatim, because that is what a worker is told to re-run.
    executed_argv = pytest_basetemp_argv(argv, basetemp)
    started = monotonic_ns()
    try:
        completed = subprocess.run(
            executed_argv,
            cwd=worktree_path,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        timed_out = True
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
        timed_out = False
    command: dict[str, object] = {
        "stage": stage,
        "attempt": ordinal,
        "argv": list(argv),
        "cwd": str(worktree_path),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "duration_ms": (monotonic_ns() - started) // 1_000_000,
        "workspace": workspace_snapshot(worktree_path),
    }
    if executed_argv != tuple(argv):
        command["executed_argv"] = list(executed_argv)
    return command


def _verification_basetemp(audit: AuditJournal, stage: str) -> Path | None:
    """Return the controller-owned temporary root for one verification stage.

    Deliberately a sibling of the audit run's ``artifacts`` directory, never
    inside it: the artifact inventory requires that directory to hold files
    only. pytest clears this path at the start of each run, so it holds one
    round's images at a time rather than accumulating.
    """

    run_dir = getattr(audit, "run_dir", None)
    if run_dir is None:
        return None
    safe_stage = re.sub(r"[^A-Za-z0-9._-]", "-", stage).strip("-") or "stage"
    basetemp = Path(run_dir) / "verification-tmp" / safe_stage
    try:
        basetemp.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return basetemp


def _attach_failure_images(
    recorded: dict[str, object],
    *,
    command: Mapping[str, object],
    basetemp: Path | None,
    evidence: EvidenceCatalog,
    audit: AuditJournal,
    stage: str,
    ordinal: int,
) -> None:
    """Persist a failing run's images onto the recorded command, if any exist.

    ``recorded`` is what every repair context embeds as
    ``failed_verification``, so writing the descriptors here is the single
    point that reaches all four repair-dispatch sites.

    Capture is on by default and costs worker context on every repair round it
    fires, so each attachment is audited with what it spent and how it chose
    the files. ``scope`` is the load-bearing field: anything other than
    ``failing-tests`` means the selection fell through to the whole temporary
    tree and the worker may be looking at a passing test's pixels.
    """

    captured = capture_failure_images(
        command=command,
        basetemp=basetemp,
        evidence=evidence,
        producer_task_id="verification-owner",
    )
    if not captured:
        return
    recorded[FAILURE_IMAGE_CONTEXT_KEY] = [
        dict(item) for item in captured.descriptors
    ]
    audit.append(
        "verification_failure_images_attached",
        status=(
            "succeeded"
            if captured.scope == SCOPE_FAILING_TESTS
            else "degraded"
        ),
        payload={
            "stage": stage,
            "attempt": ordinal,
            "scope": captured.scope,
            "image_count": len(captured.descriptors),
            "total_bytes": captured.total_bytes,
            "considered": captured.considered,
            "budget": {
                "image_limit": captured.limit,
                "total_bytes_limit": captured.total_bytes_limit,
            },
            "evidence_refs": [
                item["evidence_ref"] for item in captured.descriptors
            ],
            "relative_paths": [
                item["relative_path"] for item in captured.descriptors
            ],
        },
        actor=AuditActor("verification-owner", "verification_owner"),
    )


def _combine_verification_results(
    first: DeterministicVerificationResult | None,
    second: DeterministicVerificationResult,
) -> DeterministicVerificationResult:
    if first is None:
        return second
    return DeterministicVerificationResult(
        status=second.status,
        reason=second.reason,
        command_attempts=first.command_attempts + second.command_attempts,
        repair_attempts=first.repair_attempts + second.repair_attempts,
        repair_invocation_ids=first.repair_invocation_ids + second.repair_invocation_ids,
        repair_invocations=first.repair_invocations + second.repair_invocations,
    )


def _promote_gate_criteria(
    kernel: ControllerKernel,
    verification_result: DeterministicVerificationResult,
) -> None:
    """Satisfy any pending gate-backed criteria from the controller-owned
    verification command's own passing evidence.

    Called only after the declared verification command has actually
    succeeded, so a criterion the coordinator left pending at
    run.complete_request (because it could not truthfully claim it) is
    satisfied from real command evidence rather than from any claim.
    """

    criteria = kernel.snapshot()["criteria"]
    pending_gate_ids = tuple(
        criterion_id
        for criterion_id, criterion in criteria.items()
        if criterion.get("adjudication") == "deterministic_verification"
        and criterion["status"] != "satisfied"
    )
    if not pending_gate_ids:
        return
    passing_ref = None
    for attempt in reversed(verification_result.command_attempts):
        if attempt.get("exit_code") == 0:
            passing_ref = attempt.get("evidence_ref")
            break
    if not isinstance(passing_ref, str):
        raise ValueError(
            "successful verification result has no passing command evidence"
        )
    kernel.record_gate_verification(
        criterion_ids=pending_gate_ids,
        evidence_ref=passing_ref,
    )


def _record_gate_verification_failure(
    kernel: ControllerKernel,
    verification_result: DeterministicVerificationResult,
) -> None:
    """Walk a run a coordinator completion request marked "succeeded" back
    off that status once the declared verification command has actually
    failed, so a gate-backed criterion left pending never persists inside a
    kernel snapshot that still claims the run "succeeded".
    """

    criteria = kernel.snapshot()["criteria"]
    pending_gate_ids = tuple(
        criterion_id
        for criterion_id, criterion in criteria.items()
        if criterion.get("adjudication") == "deterministic_verification"
        and criterion["status"] != "satisfied"
    )
    if not pending_gate_ids:
        return
    failing_ref = None
    for attempt in reversed(verification_result.command_attempts):
        if attempt.get("exit_code") != 0:
            failing_ref = attempt.get("evidence_ref")
            break
    if not isinstance(failing_ref, str):
        return
    kernel.record_gate_verification_failure(
        criterion_ids=pending_gate_ids,
        evidence_ref=failing_ref,
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else value
    )


def _validate_repository_binding(
    contract: RunContract,
    creation: Mapping[str, object],
) -> None:
    expected = {
        "path": creation["worktree_path"],
        "branch": creation["feature_branch"],
        "base_branch": creation["base_branch"],
        "base_commit": creation["base_commit"],
    }
    mismatches = [
        name
        for name, value in expected.items()
        if contract.repository.get(name) != value
    ]
    if mismatches:
        raise ValueError(
            "FeatureRun contract does not bind its Git transaction: "
            + ", ".join(mismatches)
        )


def _record_git_receipt(
    audit: AuditJournal,
    evidence: EvidenceCatalog,
    receipt: Mapping[str, object],
) -> None:
    artifact = evidence.add(
        kind=f"git-{receipt['operation']}-receipt",
        content=receipt,
        media_type="application/json",
        producer_task_id="integration-owner",
    )
    audit.append(
        f"git_{receipt['operation']}_completed",
        status="succeeded",
        payload={**receipt, "evidence_ref": artifact.ref},
        actor=AuditActor("integration-owner", "integration_owner"),
    )


def _is_full_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_plan_graph_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and value[0].isalnum()
        and all(character.isalnum() or character in "._-" for character in value)
    )


def _is_evidence_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("artifact:sha256:")
        and _is_sha256(value.removeprefix("artifact:sha256:"))
    )


def _validate_dependency_candidates(
    candidates: tuple[Mapping[str, object], ...],
) -> None:
    node_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("PlanGraph child dependency candidate must be a mapping")
        if set(candidate) != {"node_id", "candidate_commit", "seal_receipt_ref"}:
            raise ValueError("PlanGraph child dependency candidate has unexpected fields")
        node_id = candidate["node_id"]
        if not _is_plan_graph_id(node_id) or node_id in node_ids:
            raise ValueError("PlanGraph child dependency candidate node_id is invalid or duplicated")
        if not _is_full_commit(candidate["candidate_commit"]):
            raise ValueError("PlanGraph child dependency candidate commit is invalid")
        if not _is_evidence_ref(candidate["seal_receipt_ref"]):
            raise ValueError("PlanGraph child dependency seal receipt ref is invalid")
        node_ids.add(node_id)


def _content_ref(content: Mapping[str, object]) -> str:
    raw = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8") + b"\n"
    return f"artifact:sha256:{hashlib.sha256(raw).hexdigest()}"


def _last_verification_evidence_ref(
    verification: DeterministicVerificationResult,
) -> str | None:
    for attempt in reversed(verification.command_attempts):
        value = attempt.get("evidence_ref")
        content = dict(attempt)
        content.pop("evidence_ref", None)
        if _is_evidence_ref(value) and _content_ref(content) == value:
            return value.removeprefix("artifact:sha256:")
    return None


def _candidate_receipt_ref(
    receipts: tuple[Mapping[str, object], ...],
    *,
    parent_candidate_commit: str,
    candidate_commit: str,
    writable_paths: tuple[str, ...] | None,
) -> str | None:
    for receipt in reversed(receipts):
        if receipt.get("operation") == "commit":
            if (
                receipt.get("base_commit") != parent_candidate_commit
                or receipt.get("candidate_commit") != candidate_commit
            ):
                return None
            if writable_paths is None:
                return None
            try:
                if normalize_allowed_paths(receipt.get("allowed_paths", ())) != (
                    normalize_allowed_paths(writable_paths)
                ):
                    return None
            except GitTransactionError:
                return None
            return _content_ref(dict(receipt)).removeprefix("artifact:sha256:")
    return None


__all__ = [
    "DeterministicVerificationResult",
    "FeatureContractFactory",
    "FeatureProfileBuilder",
    "FeatureRunResult",
    "FeatureSessionFactory",
    "ReviewFixPolicy",
    "VerificationGate",
    "VerificationRepairExecutorFactory",
    "run_feature_worktree",
    "run_plan_graph_feature_worktree",
]
