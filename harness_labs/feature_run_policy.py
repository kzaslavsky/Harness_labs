"""Neutral policy and dispatch schema for the standard FeatureRun lifecycle."""

from __future__ import annotations

from .development_policy import DevelopmentPolicy


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

    from .coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment

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


__all__ = [
    "standard_feature_run_dispatch_schema",
    "standard_feature_run_policy",
]
