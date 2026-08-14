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
]
