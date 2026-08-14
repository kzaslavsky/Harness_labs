"""Typed, registration-bound recovery authority for PlanGraph."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


class RecoveryAuthorityError(ValueError):
    pass


AUTHORITY_PROTOCOL = "plan-graph-automatic-recovery/1"
DECISION_PROTOCOL = "plan-graph-recovery-decision/1"
TRANSITION_PROTOCOL = "plan-graph-version-transition/1"
ACTION_TYPES = frozenset({
    "resume", "extend_budget", "transfer_ownership", "ratify_gate_change",
    "revise_acceptance", "revise_functionality", "accept_contract_deviation",
})
REVISION_ACTIONS = frozenset({"revise_acceptance", "revise_functionality", "accept_contract_deviation"})
STRUCTURAL_ACTIONS = frozenset({"transfer_ownership", "ratify_gate_change"}) | REVISION_ACTIONS


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class AutomaticRecoveryAuthority:
    allowed_actions: tuple[str, ...]
    max_extra_node_launches: int
    max_structural_decisions: int
    protocol: str = AUTHORITY_PROTOCOL

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "AutomaticRecoveryAuthority":
        if value is None:
            return cls((), 0, 0)
        if not isinstance(value, Mapping) or set(value) != {"protocol", "allowed_actions", "max_extra_node_launches", "max_structural_decisions"}:
            raise RecoveryAuthorityError("automatic_recovery must contain exactly its v1 fields")
        actions, limit, structural_limit = (value.get("allowed_actions"), value.get("max_extra_node_launches"), value.get("max_structural_decisions"))
        if value.get("protocol") != AUTHORITY_PROTOCOL:
            raise RecoveryAuthorityError("unsupported automatic_recovery protocol")
        if (not isinstance(actions, list) or not all(isinstance(item, str) and item in ACTION_TYPES for item in actions)
                or len(set(actions)) != len(actions) or type(limit) is not int or limit < 0
                or type(structural_limit) is not int or structural_limit < 0):
            raise RecoveryAuthorityError("automatic_recovery is invalid")
        return cls(tuple(actions), limit, structural_limit)

    def as_mapping(self) -> dict[str, object]:
        return {"protocol": self.protocol, "allowed_actions": list(self.allowed_actions), "max_extra_node_launches": self.max_extra_node_launches, "max_structural_decisions": self.max_structural_decisions}

    @property
    def sha256(self) -> str:
        return digest(self.as_mapping())


def validate_recovery_decision(
    value: Mapping[str, object], authority: AutomaticRecoveryAuthority, *,
    allow_plan_revision: bool = False,
) -> dict[str, object]:
    required = {"protocol", "action", "target", "expected_prior_digest", "payload"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("protocol") != DECISION_PROTOCOL:
        raise RecoveryAuthorityError("unsupported or malformed recovery decision")
    action, target, prior, payload = value.get("action"), value.get("target"), value.get("expected_prior_digest"), value.get("payload")
    if (not isinstance(action, str) or action not in ACTION_TYPES or action not in authority.allowed_actions
            or not isinstance(target, str) or not target or not _is_sha256(prior)
            or not isinstance(payload, Mapping)):
        raise RecoveryAuthorityError("recovery decision exceeds registration authority")
    if action == "extend_budget" and (
        set(payload) != {"launches"} or type(payload.get("launches")) is not int
        or payload["launches"] < 1
    ):
        raise RecoveryAuthorityError("extend_budget requires positive launches")
    if action in REVISION_ACTIONS and not allow_plan_revision:
        raise RecoveryAuthorityError("plan revision actions require a plan-version transition")
    if action == "resume" and payload:
        raise RecoveryAuthorityError("resume requires an empty payload")
    if action == "transfer_ownership" and (
        set(payload) != {"receiving_node"}
        or not isinstance(payload.get("receiving_node"), str)
        or not payload["receiving_node"]
        or payload["receiving_node"] == target
    ):
        raise RecoveryAuthorityError(
            "transfer_ownership requires a distinct receiving_node"
        )
    if action == "ratify_gate_change" and (
        set(payload) != {"gate", "budget_carryover"}
        or not isinstance(payload.get("gate"), str) or not payload["gate"]
        or payload.get("budget_carryover") not in {"full", "reset"}
    ):
        raise RecoveryAuthorityError("ratify_gate_change requires gate and budget_carryover")
    return dict(value)


def validate_plan_version_transition(value: Mapping[str, object], authority: AutomaticRecoveryAuthority) -> dict[str, object]:
    required = {"protocol", "action", "predecessor_plan_sha256", "successor_plan_sha256", "node_correspondence", "budget_carryover", "authorizing_decision"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("protocol") != TRANSITION_PROTOCOL:
        raise RecoveryAuthorityError("unsupported or malformed plan-version transition")
    action = value.get("action")
    predecessor, successor = value.get("predecessor_plan_sha256"), value.get("successor_plan_sha256")
    correspondence, carryover, decision = value.get("node_correspondence"), value.get("budget_carryover"), value.get("authorizing_decision")
    if (action not in REVISION_ACTIONS or action not in authority.allowed_actions or not all(_is_sha256(item) for item in (predecessor, successor))
            or predecessor == successor or not isinstance(correspondence, Mapping) or not isinstance(carryover, Mapping) or not isinstance(decision, Mapping)):
        raise RecoveryAuthorityError("plan-version transition exceeds registration authority")
    if (not correspondence or set(correspondence) != set(carryover)
            or any(not isinstance(node, str) or node != mapped for node, mapped in correspondence.items())):
        raise RecoveryAuthorityError("plan-version transition only permits identical node_id correspondence")
    if any(value not in {"full", "reset"} for value in carryover.values()):
        raise RecoveryAuthorityError("plan-version transition has invalid budget carryover")
    try:
        decision_value = validate_recovery_decision(
            decision, authority, allow_plan_revision=True
        )
    except RecoveryAuthorityError as exc:
        raise RecoveryAuthorityError("transition authorizing decision is invalid") from exc
    if (decision_value["action"] != action
            or decision_value["target"] != "plan_version"
            or decision_value["expected_prior_digest"] != predecessor):
        raise RecoveryAuthorityError("transition authorizing decision action mismatch")
    return dict(value)
