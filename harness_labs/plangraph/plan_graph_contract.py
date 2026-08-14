"""Canonical, repository-owned identity for PlanGraph inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


PLAN_GRAPH_PROTOCOL = "plan-graph-plan/1"
REPOSITORY_IDENTITY_PROTOCOL = "harness-repository-identity/1"


class PlanGraphContractError(ValueError):
    """Raised when a canonical PlanGraph artifact violates its contract."""


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def normalize_repository_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanGraphContractError(f"{field} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("./"):
        raise PlanGraphContractError(
            f"{field} must be a normalized repository-relative path"
        )
    normalized = str(path)
    if normalized in {"", "."} or normalized != value:
        raise PlanGraphContractError(
            f"{field} must be a normalized repository-relative path"
        )
    return normalized


def canonical_plan_graph_payload(payload: Mapping[str, object]) -> dict[str, Any]:
    """Validate and normalize one committed ``plan-graph-plan/1`` payload."""

    expected = {
        "protocol",
        "plan",
        "plan_sections",
        "acceptance_criteria",
        "runs",
        "functionality_tests",
        "referenced_artifacts",
    }
    _require_exact_keys(payload, expected, "plan")
    if payload.get("protocol") != PLAN_GRAPH_PROTOCOL:
        raise PlanGraphContractError(
            f"plan protocol must be {PLAN_GRAPH_PROTOCOL!r}"
        )
    plan_path = normalize_repository_path(payload.get("plan"), field="plan.plan")
    sections = _string_mapping(payload.get("plan_sections"), "plan.plan_sections")
    criteria = _string_mapping(
        payload.get("acceptance_criteria"), "plan.acceptance_criteria"
    )
    runs_value = payload.get("runs")
    if not isinstance(runs_value, list) or not runs_value:
        raise PlanGraphContractError("plan.runs must be a non-empty array")
    runs = [_canonical_run(value, index) for index, value in enumerate(runs_value)]
    tests_value = payload.get("functionality_tests")
    if not isinstance(tests_value, list):
        raise PlanGraphContractError("plan.functionality_tests must be an array")
    functionality_tests = [
        _canonical_command(value, f"plan.functionality_tests[{index}]")
        for index, value in enumerate(tests_value)
    ]
    artifacts_value = payload.get("referenced_artifacts")
    if not isinstance(artifacts_value, list):
        raise PlanGraphContractError("plan.referenced_artifacts must be an array")
    referenced_artifacts = [
        normalize_repository_path(
            value, field=f"plan.referenced_artifacts[{index}]"
        )
        for index, value in enumerate(artifacts_value)
    ]
    if len(referenced_artifacts) != len(set(referenced_artifacts)):
        raise PlanGraphContractError("plan.referenced_artifacts contains duplicates")
    return {
        "protocol": PLAN_GRAPH_PROTOCOL,
        "plan": plan_path,
        "plan_sections": sections,
        "acceptance_criteria": criteria,
        "runs": runs,
        "functionality_tests": functionality_tests,
        "referenced_artifacts": referenced_artifacts,
    }


def plan_graph_identity(
    *,
    repository_id: str,
    base_commit: str,
    plan_sha256: str,
    decomposition: Mapping[str, object],
) -> str:
    """Return the one identity shared by approval and PlanGraph audit."""

    if not repository_id:
        raise PlanGraphContractError("repository_id must not be empty")
    _require_hex(base_commit, 40, "base_commit")
    _require_hex(plan_sha256, 64, "plan_sha256")
    canonical = canonical_plan_graph_payload(decomposition)
    return sha256_json(
        {
            "protocol": "plan-graph-identity/1",
            "repository_id": repository_id,
            "base_commit": base_commit,
            "plan_sha256": plan_sha256,
            "decomposition": canonical,
        }
    )


def load_repository_id(payload: Mapping[str, object]) -> str:
    _require_exact_keys(payload, {"protocol", "repository_id"}, "repository identity")
    if payload.get("protocol") != REPOSITORY_IDENTITY_PROTOCOL:
        raise PlanGraphContractError("unsupported repository identity protocol")
    repository_id = payload.get("repository_id")
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise PlanGraphContractError("repository_id must be a non-empty string")
    return repository_id


def path_is_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        candidate == PurePosixPath(allowed)
        or PurePosixPath(allowed) in candidate.parents
        for allowed in allowed_paths
    )


def _canonical_run(value: object, index: int) -> dict[str, Any]:
    field = f"plan.runs[{index}]"
    if not isinstance(value, Mapping):
        raise PlanGraphContractError(f"{field} must be an object")
    required = {
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
    }
    # ``verification_gates`` is deliberately optional: a run that omits it
    # canonicalizes with the exact same key set (and therefore byte-for-byte
    # the same JSON and digest) every existing decomposition already
    # produces. Only a run that declares the key opts into gate-tuple shape.
    _require_keys(value, required, {"verification_gates"}, field)
    has_gate_tuple = "verification_gates" in value
    run_id = _nonempty_string(value.get("id"), f"{field}.id")
    objective = _nonempty_string(value.get("objective"), f"{field}.objective")
    plan_sections = _string_array(value.get("plan_sections"), f"{field}.plan_sections")
    criteria = _string_array(value.get("criteria"), f"{field}.criteria")
    depends_on = _string_array(
        value.get("depends_on"), f"{field}.depends_on", allow_empty=True
    )
    allowed_values = value.get("allowed_paths")
    if not isinstance(allowed_values, list) or not allowed_values:
        raise PlanGraphContractError(f"{field}.allowed_paths must be a non-empty array")
    allowed_paths = [
        normalize_repository_path(item, field=f"{field}.allowed_paths[{offset}]")
        for offset, item in enumerate(allowed_values)
    ]
    if len(allowed_paths) != len(set(allowed_paths)):
        raise PlanGraphContractError(f"{field}.allowed_paths contains duplicates")
    intents_value = value.get("path_intents")
    if not isinstance(intents_value, list):
        raise PlanGraphContractError(f"{field}.path_intents must be an array")
    path_intents = [
        _canonical_path_intent(item, f"{field}.path_intents[{offset}]")
        for offset, item in enumerate(intents_value)
    ]
    for intent in path_intents:
        if not path_is_allowed(intent["path"], allowed_paths):
            raise PlanGraphContractError(
                f"{field} path intent {intent['path']!r} is outside allowed_paths"
            )
    # A run declaring a gate tuple carries its verification shape entirely
    # in ``verification_gates``; its flat ``verification_argv`` is then
    # allowed to be empty rather than the ordinarily-required command.
    argv = _string_array(
        value.get("verification_argv"),
        f"{field}.verification_argv",
        allow_empty=has_gate_tuple,
    )
    timeout = _positive_number(
        value.get("verification_timeout_seconds"),
        f"{field}.verification_timeout_seconds",
    )
    required_values = value.get("verification_required_paths")
    if not isinstance(required_values, list):
        raise PlanGraphContractError(
            f"{field}.verification_required_paths must be an array"
        )
    required_paths = [
        _canonical_required_path(
            item, f"{field}.verification_required_paths[{offset}]"
        )
        for offset, item in enumerate(required_values)
    ]
    result = {
        "id": run_id,
        "objective": objective,
        "plan_sections": plan_sections,
        "criteria": criteria,
        "depends_on": depends_on,
        "allowed_paths": allowed_paths,
        "path_intents": path_intents,
        "verification_argv": argv,
        "verification_timeout_seconds": timeout,
        "verification_required_paths": required_paths,
    }
    if has_gate_tuple:
        if argv:
            raise PlanGraphContractError(
                f"{field} may declare verification_argv or verification_gates, not both"
            )
        result["verification_gates"] = _canonical_gate_tuple(
            value["verification_gates"], f"{field}.verification_gates"
        )
    return result


def _canonical_gate_tuple(value: object, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise PlanGraphContractError(f"{field} must be a non-empty array")
    gates = [
        _canonical_gate(item, f"{field}[{index}]") for index, item in enumerate(value)
    ]
    names = [gate["name"] for gate in gates]
    if len(names) != len(set(names)):
        raise PlanGraphContractError(f"{field} contains duplicate gate names")
    return gates


def _canonical_gate(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanGraphContractError(f"{field} must be an object")
    _require_exact_keys(value, {"name", "argv", "timeout_seconds"}, field)
    return {
        "name": _nonempty_string(value.get("name"), f"{field}.name"),
        "argv": _string_array(value.get("argv"), f"{field}.argv"),
        "timeout_seconds": _positive_number(
            value.get("timeout_seconds"), f"{field}.timeout_seconds"
        ),
    }


def _canonical_command(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanGraphContractError(f"{field} must be an object")
    _require_exact_keys(value, {"argv", "timeout_seconds", "required_paths"}, field)
    required_values = value.get("required_paths")
    if not isinstance(required_values, list):
        raise PlanGraphContractError(f"{field}.required_paths must be an array")
    return {
        "argv": _string_array(value.get("argv"), f"{field}.argv"),
        "timeout_seconds": _positive_number(
            value.get("timeout_seconds"), f"{field}.timeout_seconds"
        ),
        "required_paths": [
            _canonical_required_path(item, f"{field}.required_paths[{index}]")
            for index, item in enumerate(required_values)
        ],
    }


def _canonical_required_path(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanGraphContractError(f"{field} must be an object")
    availability = value.get("availability")
    if availability == "base":
        _require_exact_keys(value, {"path", "availability"}, field)
        producer = None
    elif availability == "created_by":
        _require_exact_keys(value, {"path", "availability", "producer_run_id"}, field)
        producer = _nonempty_string(value.get("producer_run_id"), f"{field}.producer_run_id")
    else:
        raise PlanGraphContractError(
            f"{field}.availability must be 'base' or 'created_by'"
        )
    result = {
        "path": normalize_repository_path(value.get("path"), field=f"{field}.path"),
        "availability": availability,
    }
    if producer is not None:
        result["producer_run_id"] = producer
    return result


def _canonical_path_intent(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PlanGraphContractError(f"{field} must be an object")
    _require_exact_keys(value, {"path", "action"}, field)
    action = value.get("action")
    if action not in {"create", "modify"}:
        raise PlanGraphContractError(f"{field}.action must be 'create' or 'modify'")
    return {
        "path": normalize_repository_path(value.get("path"), field=f"{field}.path"),
        "action": str(action),
    }


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PlanGraphContractError(f"{field} must be a non-empty object")
    result: dict[str, str] = {}
    for key, item in value.items():
        result[_nonempty_string(key, f"{field} key")] = _nonempty_string(
            item, f"{field}.{key}"
        )
    return result


def _string_array(value: object, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        raise PlanGraphContractError(f"{field} must be {qualifier}")
    result = [_nonempty_string(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise PlanGraphContractError(f"{field} contains duplicates")
    return result


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PlanGraphContractError(f"{field} must be a positive number")
    return float(value)


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanGraphContractError(f"{field} must be a non-empty string")
    return value


def _require_exact_keys(value: Mapping[str, object], expected: set[str], field: str) -> None:
    _require_keys(value, expected, set(), field)


def _require_keys(
    value: Mapping[str, object], required: set[str], optional: set[str], field: str
) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    extra = sorted(actual - required - optional)
    if missing or extra:
        raise PlanGraphContractError(
            f"{field} has invalid keys (missing={missing}, extra={extra})"
        )


def _require_hex(value: str, length: int, field: str) -> None:
    if len(value) != length or any(character not in "0123456789abcdef" for character in value):
        raise PlanGraphContractError(f"{field} must be {length} lowercase hex characters")


__all__ = [
    "PLAN_GRAPH_PROTOCOL",
    "PlanGraphContractError",
    "canonical_json",
    "canonical_plan_graph_payload",
    "load_repository_id",
    "normalize_repository_path",
    "path_is_allowed",
    "plan_graph_identity",
    "sha256_bytes",
    "sha256_json",
]
