"""Versioned feature-development policies compiled into coordinator context."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


DEVELOPMENT_POLICY_PROTOCOL = "development-policy/1"


@dataclass(frozen=True)
class ReviewAssignment:
    role: str
    focus: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "focus": list(self.focus)}


@dataclass(frozen=True)
class DevelopmentPolicy:
    """Portable planning and review obligations, not model-specific prompts."""

    policy_id: str
    planning: Mapping[str, Any]
    review: Mapping[str, Any]
    protocol: str = DEVELOPMENT_POLICY_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != DEVELOPMENT_POLICY_PROTOCOL:
            raise ValueError("development policy protocol is invalid")
        if not self.policy_id.strip():
            raise ValueError("development policy_id must be non-empty")
        _validate_planning(self.planning)
        _validate_review(self.review)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DevelopmentPolicy":
        if value.get("protocol") != DEVELOPMENT_POLICY_PROTOCOL:
            raise ValueError("development policy protocol is invalid")
        planning = value.get("planning")
        review = value.get("review")
        if not isinstance(planning, Mapping) or not isinstance(review, Mapping):
            raise ValueError("development policy requires planning and review objects")
        return cls(
            policy_id=_required_text(value, "policy_id"),
            planning=dict(planning),
            review=dict(review),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "policy_id": self.policy_id,
            "planning": dict(self.planning),
            "review": dict(self.review),
        }

    def sha256(self) -> str:
        raw = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    def review_assignments(
        self, changed_paths: Iterable[str]
    ) -> tuple[ReviewAssignment, ...]:
        paths = tuple(path.lower() for path in changed_paths)
        review = self.review
        assignments = [
            ReviewAssignment(
                str(item["role"]),
                tuple(str(value) for value in item["focus"]),
            )
            for item in review["base_panel"]
        ]
        for rule in review["risk_rules"]:
            if any(
                any(marker in path for marker in rule["path_markers"])
                for path in paths
            ):
                assignments.extend(
                    ReviewAssignment(
                        str(item["role"]),
                        tuple(str(value) for value in item["focus"]),
                    )
                    for item in rule["add_assignments"]
                )
        unique: dict[str, ReviewAssignment] = {}
        for assignment in assignments:
            previous = unique.get(assignment.role)
            focus = tuple(
                dict.fromkeys(
                    (*(previous.focus if previous else ()), *assignment.focus)
                )
            )
            unique[assignment.role] = ReviewAssignment(assignment.role, focus)
        return tuple(unique[key] for key in sorted(unique))


def implement_v13_development_policy() -> DevelopmentPolicy:
    """Return the provider-neutral policy distilled from Claude implement-v13."""

    return DevelopmentPolicy(
        policy_id="implement-v13-sourcebound-riskreview/1",
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


def implement_v13_dispatch_schema():
    """Compile the policy into the standard seven-phase feature lifecycle."""

    from .coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment

    policy = implement_v13_development_policy()
    return CoordinatorDispatchSchema(
        schema_id="implement-v13-portable/1",
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


def _validate_planning(value: Mapping[str, Any]) -> None:
    for name in (
        "required_source_binding_fields",
        "required_plan_sections",
        "refutation_lenses",
        "required_handoff_sections",
    ):
        if not isinstance(value.get(name), list) or not value[name]:
            raise ValueError(f"planning policy {name} must be a non-empty list")
    rounds = value.get("independent_review_rounds")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
        raise ValueError("planning independent_review_rounds must be non-negative")


def _validate_review(value: Mapping[str, Any]) -> None:
    for name in ("finding_fields", "base_panel", "risk_rules", "required_final_checks"):
        if not isinstance(value.get(name), list) or not value[name]:
            raise ValueError(f"review policy {name} must be a non-empty list")
    for name in ("mechanical_cycle_limit", "sensitive_cycle_limit"):
        if not isinstance(value.get(name), int) or value[name] < 1:
            raise ValueError(f"review policy {name} must be positive")


def _required_text(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"development policy {name} must be non-empty")
    return item


__all__ = [
    "DEVELOPMENT_POLICY_PROTOCOL",
    "DevelopmentPolicy",
    "ReviewAssignment",
    "implement_v13_development_policy",
    "implement_v13_dispatch_schema",
]
