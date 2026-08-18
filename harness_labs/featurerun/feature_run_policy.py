"""Neutral policy and dispatch schema for the standard FeatureRun lifecycle."""

from __future__ import annotations

from harness_labs.core.development_policy import DevelopmentPolicy
from harness_labs.featurerun.feature_run import (
    RecoveryAgent,
    RecoveryContext,
    RecoveryDecision,
    deterministic_recovery_agent,
)


def standard_feature_run_policy() -> DevelopmentPolicy:
    """Return the provider- and skill-neutral standard FeatureRun policy."""

    return DevelopmentPolicy(
        policy_id="feature-run-sourcebound-riskreview/1",
        planning={
            "required_source_binding_fields": [
                "claim",
                "repository_path",
                "symbol_or_line",
                "verification_command",
                "evidence_ref",
            ],
            "required_plan_sections": [
                "scope",
                "verified_current_state",
                "dependency_ordered_steps",
                "runtime_contracts",
                "acceptance_and_tests",
                "risks",
                "rejected_alternatives",
            ],
            "refutation_lenses": [
                {
                    "id": "FRAME",
                    "attacks": [
                        "scope",
                        "ownership",
                        "architecture",
                        "contracts",
                        "ADR alignment",
                    ],
                },
                {
                    "id": "NECESSITY",
                    "attacks": [
                        "unnecessary substrate",
                        "duplicate mechanism",
                        "avoidable surface area",
                    ],
                },
                {
                    "id": "MECHANISM",
                    "attacks": [
                        "transactions",
                        "crash replay",
                        "idempotency",
                        "destructive operations",
                        "integrity",
                        "sensitive-data containment",
                        "refusal behavior",
                    ],
                },
            ],
            "independent_review_rounds": 2,
            "required_handoff_sections": [
                "will_touch",
                "must_not_touch",
                "rejected_alternatives",
                "tacit_knowledge",
                "gate_hazards",
            ],
        },
        review={
            "finding_fields": [
                "file",
                "subject",
                "score",
                "fix_cost",
                "protects",
                "scope_expanding",
                "contract_violation",
                "new_evidence",
                "required_paths",
            ],
            "base_panel": [
                {
                    "role": "correctness-reviewer",
                    "focus": [
                        "acceptance criteria",
                        "runtime behavior",
                        "unhappy paths",
                    ],
                },
                {
                    "role": "adversarial-reviewer",
                    "focus": [
                        "false success",
                        "scope growth",
                        "regressions",
                    ],
                },
            ],
            "risk_rules": [
                {
                    "id": "security-sensitive",
                    "path_markers": ["auth", "security", "permission", "secret"],
                    "add_assignments": [
                        {
                            "role": "security-reviewer",
                            "focus": [
                                "authorization",
                                "disclosure attack",
                                "fail-closed behavior",
                            ],
                        }
                    ],
                },
                {
                    "id": "data-contract",
                    "path_markers": ["schema", "migration", ".sql", "store"],
                    "add_assignments": [
                        {
                            "role": "data-contract-reviewer",
                            "focus": [
                                "compatibility",
                                "migration rollback",
                                "data integrity",
                            ],
                        }
                    ],
                },
                {
                    "id": "ui-runtime",
                    "path_markers": [".html", ".css", ".js", ".tsx", "template"],
                    "add_assignments": [
                        {
                            "role": "ui-runtime-reviewer",
                            "focus": [
                                "browser walk",
                                "responsive behavior",
                                "accessibility",
                                "visual regression",
                            ],
                        }
                    ],
                },
            ],
            "mechanical_cycle_limit": 3,
            "sensitive_cycle_limit": 5,
            "required_final_checks": [
                "targeted tests",
                "regression review",
                "unresolved-finding gate",
            ],
        },
    )


def standard_feature_run_dispatch_schema():
    """Compile the neutral standard seven-phase FeatureRun lifecycle."""

    from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment

    policy = standard_feature_run_policy()
    return CoordinatorDispatchSchema(
        schema_id="feature-run-standard/1",
        segments=(
            CoordinatorSegment(
                id="plan-refute",
                phases=("orient", "plan"),
                coordinator_profile="planning-coordinator",
                instructions=(
                    "Orient, construct a source-bound plan, run every declared "
                    "refutation lens and independent review round, revise once in "
                    "a batch, and produce the declared build handoff."
                ),
                exit_artifact_kinds=(
                    "engineering-plan",
                    "source-binding-report",
                    "refutation-frame",
                    "refutation-necessity",
                    "refutation-mechanism",
                    "build-briefing",
                ),
                development_policy=policy,
            ),
            CoordinatorSegment(
                id="build",
                phases=("implement",),
                coordinator_profile="build-coordinator",
                instructions=(
                    "Implement from the accepted plan and build briefing, gather "
                    "additional context as needed, and leave a tested candidate."
                ),
                context_artifact_kinds=(
                    "engineering-plan",
                    "source-binding-report",
                    "build-briefing",
                ),
                required_artifact_kinds=("engineering-plan", "build-briefing"),
                exit_artifact_kinds=("implementation-summary",),
                development_policy=policy,
            ),
            CoordinatorSegment(
                id="verify",
                phases=("verify",),
                coordinator_profile="verification-coordinator",
                instructions=(
                    "Run the declared acceptance and regression checks against "
                    "the actual candidate and report their observed results. Do "
                    "not perform adversarial review or remediate the candidate."
                ),
                context_artifact_kinds=(
                    "engineering-plan",
                    "implementation-summary",
                ),
                required_artifact_kinds=(
                    "engineering-plan",
                    "implementation-summary",
                ),
                exit_artifact_kinds=("verification-report",),
                development_policy=policy,
            ),
            CoordinatorSegment(
                id="review",
                phases=("review",),
                coordinator_profile="review-coordinator",
                instructions=(
                    "Run one adversarial discovery review of the verified "
                    "candidate. Freeze that first finding set; later closure "
                    "passes may only resolve those findings and cannot authorize "
                    "new work."
                ),
                context_artifact_kinds=(
                    "engineering-plan",
                    "implementation-summary",
                    "verification-report",
                ),
                required_artifact_kinds=(
                    "engineering-plan",
                    "implementation-summary",
                    "verification-report",
                ),
                exit_artifact_kinds=("review-ledger",),
                development_policy=policy,
            ),
            CoordinatorSegment(
                id="integrate-report",
                phases=("integrate", "report"),
                coordinator_profile="integration-coordinator",
                instructions=(
                    "Read the gate evidence, request integration only when every "
                    "required finding is closed, and produce the final run report."
                ),
                context_artifact_kinds=(
                    "engineering-plan",
                    "implementation-summary",
                    "verification-report",
                    "review-ledger",
                ),
                required_artifact_kinds=(
                    "verification-report",
                    "review-ledger",
                ),
                exit_artifact_kinds=("run-report",),
                development_policy=policy,
            ),
        ),
    )


# The review loop reports why it stopped.  Only budget exhaustion is worth
# continuing: "no_progress" and "marginal_yield" are the loop's own futility
# detectors, and buying more cycles after either one repeats a strategy the
# loop already measured as not working.
_CONTINUABLE_STOP_REASONS = frozenset({"cycle_limit"})


def standard_review_continuation_recovery_agent() -> RecoveryAgent:
    """Return the conservative default FeatureRun recovery policy.

    It grants continuations for exactly one condition: a review loop that
    exhausted its cycles while still discharging findings.  Continuing there
    reuses the implementation and the review ledger already paid for, whereas
    letting the node block re-runs implementation, verification, and review
    from scratch on the next graph attempt.

    Every other abnormal stage stops, deliberately.  Dispatch, verification,
    and Git-integration failures are not cheaper to retry blind, and silently
    retrying them would hide real defects behind spend.  ``recovery_limit``
    bounds how many continuations a single FeatureRun can be granted.
    """

    def agent(context: RecoveryContext) -> RecoveryDecision:
        if context.stage != "review":
            return RecoveryDecision(
                "stop",
                f"no automatic recovery policy for stage {context.stage!r}",
            )
        if context.condition != "blocked":
            return RecoveryDecision(
                "stop",
                f"review ended {context.condition!r}; only an exhausted-cycle "
                "block is safe to continue",
            )
        detail = context.stage_detail
        stop_reason = str(detail.get("stop_reason", ""))
        if stop_reason not in _CONTINUABLE_STOP_REASONS:
            return RecoveryDecision(
                "stop",
                f"review stopped as {stop_reason or 'unclassified'!s}, which is "
                "the loop's own verdict that more cycles will not help",
            )
        open_keys = detail.get("open_finding_keys") or ()
        if not open_keys:
            return RecoveryDecision(
                "stop",
                "review blocked with no open findings to continue against",
            )
        return RecoveryDecision(
            "retry",
            "review exhausted its cycle budget with "
            f"{len(open_keys)} finding(s) still open; continuing the ledger "
            "instead of re-running implementation",
        )

    return agent


def standard_composed_recovery_agent() -> RecoveryAgent:
    """Return the platform default: continuation first, transient retry second.

    The two policies are complementary, not alternatives, and binding either
    one alone silently gives up what the other covers:

    * ``standard_review_continuation_recovery_agent`` handles exactly one
      content condition -- a review loop that ran out of cycle budget while
      still discharging findings -- and stops on everything else, including
      every infrastructure failure.
    * ``deterministic_recovery_agent`` retries infrastructure transients (a
      dropped stream, a DNS blip, a dead backend process) and classifies a
      review block as a non-transient stop.

    Order matters.  The deterministic agent would stop on a review block
    before the continuation policy was ever consulted, so the continuation is
    tried first and only for its own trigger; anything it declines falls
    through to the transient policy unchanged.  ``recovery_limit`` bounds the
    combined budget, which the two policies share.
    """

    continuation = standard_review_continuation_recovery_agent()

    def agent(context: RecoveryContext) -> RecoveryDecision:
        if (
            context.stage == "review"
            and context.condition == "blocked"
            and str(context.stage_detail.get("stop_reason", "")) == "cycle_limit"
        ):
            decision = continuation(context)
            if decision.action != "stop":
                return decision
        return deterministic_recovery_agent(context)

    return agent


__all__ = [
    "standard_composed_recovery_agent",
    "standard_feature_run_dispatch_schema",
    "standard_feature_run_policy",
    "standard_review_continuation_recovery_agent",
]
