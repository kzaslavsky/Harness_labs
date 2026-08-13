"""Scenario-neutral semantic result contracts for controller-owned tasks."""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Any, Mapping

from .attempts import TaskResult


SEMANTIC_RESULT_PROTOCOL = "semantic-task-result/1"
CLAIM_KINDS = frozenset({"observed", "inferred"})
FINDING_SEVERITIES = frozenset({"critical", "major", "minor", "info"})
COVERAGE_STATUSES = frozenset({"satisfied", "not_satisfied", "not_applicable"})

# Deterministic deliverable-content floor. Closed rule set, no model judgment:
# a field is refused if it is shorter than this, exactly matches a known
# placeholder token (case-insensitive, whitespace-normalized), or consists of
# a single token repeated across the whole field. Chosen low enough that real
# short sentences ("Built.", "Verified.") clear it on length alone.
MIN_DELIVERABLE_LENGTH = 4
_PLACEHOLDER_TOKENS = frozenset(
    {
        "test",
        "todo",
        "tbd",
        "n/a",
        "na",
        "placeholder",
        "wip",
        "fixme",
        "xxx",
        "lorem ipsum",
    }
)


class SemanticResultError(ValueError):
    """Raised when a TaskResult does not satisfy its declared semantic contract."""


class DeliverableFloorViolation(SemanticResultError):
    """Raised when a summary or deliverable field fails the content floor.

    Carries a machine-classified ``reason`` (``not_a_string``,
    ``sub_minimal_length``, ``placeholder_token``, or ``repeated_token``) so
    the refusal is auditable as more than a generic validation failure.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(
            f"semantic result {field} failed the deliverable-content floor "
            f"({reason})"
        )


def deliverable_floor_reason(text: str) -> str | None:
    """Classify placeholder content, or return ``None`` if it clears the floor."""

    stripped = text.strip()
    if len(stripped) < MIN_DELIVERABLE_LENGTH:
        return "sub_minimal_length"
    normalized = " ".join(stripped.lower().split()).strip(string.punctuation + " ")
    if normalized in _PLACEHOLDER_TOKENS:
        return "placeholder_token"
    tokens = [tok.strip(string.punctuation) for tok in stripped.split()]
    tokens = [tok.lower() for tok in tokens if tok]
    if len(tokens) > 1 and len(set(tokens)) == 1:
        return "repeated_token"
    return None


def enforce_deliverable_floor(text: Any, field: str) -> str:
    """Refuse placeholder content at the shared semantic result boundary.

    A closed, deterministic check — the same function both semantic executors
    and :func:`validate_semantic_result` call, so a placeholder result is
    refused mechanically rather than relying on coordinator judgment.
    """

    if not isinstance(text, str):
        raise DeliverableFloorViolation(field, "not_a_string")
    reason = deliverable_floor_reason(text)
    if reason is not None:
        raise DeliverableFloorViolation(field, reason)
    return text


@dataclass(frozen=True)
class SemanticTaskResult:
    """Validated, domain-neutral interpretation of a TaskResult payload."""

    summary: str
    claims: tuple[Mapping[str, Any], ...]
    findings: tuple[Mapping[str, Any], ...]
    artifacts: tuple[Mapping[str, Any], ...]
    criterion_coverage: tuple[Mapping[str, Any], ...]
    recommendations: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    delegation_requests: tuple[Mapping[str, Any], ...]
    details_schema: str
    details: Mapping[str, Any]


def validate_semantic_result(
    result: TaskResult,
    *,
    expected_details_schema: str,
) -> SemanticTaskResult:
    """Validate the common envelope and bind it to the task's detail schema."""

    payload = result.payload
    if not isinstance(payload, Mapping):
        raise SemanticResultError("semantic result payload must be an object")
    if payload.get("protocol") != SEMANTIC_RESULT_PROTOCOL:
        raise SemanticResultError("semantic result protocol is invalid")
    summary = _text(payload, "summary")
    enforce_deliverable_floor(summary, "summary")
    details_schema = _text(payload, "details_schema")
    if details_schema != expected_details_schema:
        raise SemanticResultError(
            "semantic result details_schema does not match the task contract"
        )
    details = payload.get("details")
    if not isinstance(details, Mapping):
        raise SemanticResultError("semantic result details must be an object")

    claims = _object_list(payload, "claims")
    seen_claims: set[str] = set()
    for claim in claims:
        claim_id = _mapping_text(claim, "id", "claim")
        if claim_id in seen_claims:
            raise SemanticResultError(f"duplicate claim id: {claim_id}")
        seen_claims.add(claim_id)
        _mapping_text(claim, "statement", "claim")
        if claim.get("kind") not in CLAIM_KINDS:
            raise SemanticResultError(f"claim {claim_id} has an invalid kind")
        _reference_list(claim, "evidence_refs", f"claim {claim_id}")

    findings = _object_list(payload, "findings")
    seen_findings: set[str] = set()
    for finding in findings:
        finding_id = _mapping_text(finding, "id", "finding")
        if finding_id in seen_findings:
            raise SemanticResultError(f"duplicate finding id: {finding_id}")
        seen_findings.add(finding_id)
        _mapping_text(finding, "statement", "finding")
        _mapping_text(finding, "category", "finding")
        if finding.get("severity") not in FINDING_SEVERITIES:
            raise SemanticResultError(
                f"finding {finding_id} has an invalid severity"
            )
        if not isinstance(finding.get("requires_disposition", False), bool):
            raise SemanticResultError(
                f"finding {finding_id} requires_disposition must be boolean"
            )
        _reference_list(finding, "evidence_refs", f"finding {finding_id}")
        source_ids = finding.get("source_finding_ids", [])
        if (
            not isinstance(source_ids, list)
            or not all(isinstance(item, str) and item for item in source_ids)
        ):
            raise SemanticResultError(
                f"finding {finding_id} source_finding_ids must be strings"
            )

    artifacts = _object_list(payload, "artifacts")
    for artifact in artifacts:
        _mapping_text(artifact, "ref", "artifact")
        _mapping_text(artifact, "kind", "artifact")
        digest = _mapping_text(artifact, "sha256", "artifact")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SemanticResultError("artifact sha256 is invalid")
        _mapping_text(artifact, "media_type", "artifact")

    coverage = _object_list(payload, "criterion_coverage")
    seen_criteria: set[str] = set()
    for item in coverage:
        criterion_id = _mapping_text(item, "criterion_id", "criterion coverage")
        if criterion_id in seen_criteria:
            raise SemanticResultError(
                f"duplicate criterion coverage: {criterion_id}"
            )
        seen_criteria.add(criterion_id)
        if item.get("status") not in COVERAGE_STATUSES:
            raise SemanticResultError(
                f"criterion {criterion_id} has an invalid coverage status"
            )
        refs = _reference_list(
            item,
            "evidence_refs",
            f"criterion {criterion_id}",
        )
        if item.get("status") == "satisfied" and not refs:
            raise SemanticResultError(
                f"satisfied criterion {criterion_id} requires evidence"
            )

    recommendations = _text_list(payload, "recommendations")
    unresolved = _text_list(payload, "unresolved_questions")
    delegations = _object_list(payload, "delegation_requests")
    for delegation in delegations:
        tasks = delegation.get("tasks")
        if not isinstance(tasks, list) or not tasks or not all(
            isinstance(item, Mapping) for item in tasks
        ):
            raise SemanticResultError(
                "delegation request tasks must be a non-empty object list"
            )
        max_parallelism = delegation.get("max_parallelism", 1)
        if not isinstance(max_parallelism, int) or max_parallelism < 1:
            raise SemanticResultError(
                "delegation request max_parallelism must be positive"
            )
    return SemanticTaskResult(
        summary=summary,
        claims=claims,
        findings=findings,
        artifacts=artifacts,
        criterion_coverage=coverage,
        recommendations=recommendations,
        unresolved_questions=unresolved,
        delegation_requests=delegations,
        details_schema=details_schema,
        details=dict(details),
    )


def semantic_payload(
    *,
    summary: str,
    details_schema: str,
    details: Mapping[str, Any],
    claims: tuple[Mapping[str, Any], ...] = (),
    findings: tuple[Mapping[str, Any], ...] = (),
    artifacts: tuple[Mapping[str, Any], ...] = (),
    criterion_coverage: tuple[Mapping[str, Any], ...] = (),
    recommendations: tuple[str, ...] = (),
    unresolved_questions: tuple[str, ...] = (),
    delegation_requests: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """Construct the common envelope without performing task-specific judgment."""

    return {
        "protocol": SEMANTIC_RESULT_PROTOCOL,
        "summary": summary,
        "claims": [dict(item) for item in claims],
        "findings": [dict(item) for item in findings],
        "artifacts": [dict(item) for item in artifacts],
        "criterion_coverage": [dict(item) for item in criterion_coverage],
        "recommendations": list(recommendations),
        "unresolved_questions": list(unresolved_questions),
        "delegation_requests": [dict(item) for item in delegation_requests],
        "details_schema": details_schema,
        "details": dict(details),
    }


def _text(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise SemanticResultError(f"semantic result {name} must be non-empty")
    return item


def _mapping_text(value: Mapping[str, Any], name: str, owner: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise SemanticResultError(f"{owner} {name} must be non-empty")
    return item


def _object_list(
    value: Mapping[str, Any],
    name: str,
) -> tuple[Mapping[str, Any], ...]:
    items = value.get(name, [])
    if not isinstance(items, list) or not all(
        isinstance(item, Mapping) for item in items
    ):
        raise SemanticResultError(f"semantic result {name} must be an object list")
    return tuple(dict(item) for item in items)


def _text_list(value: Mapping[str, Any], name: str) -> tuple[str, ...]:
    items = value.get(name, [])
    if (
        not isinstance(items, list)
        or not all(isinstance(item, str) and item.strip() for item in items)
    ):
        raise SemanticResultError(f"semantic result {name} must be a string list")
    return tuple(items)


def _reference_list(
    value: Mapping[str, Any],
    name: str,
    owner: str,
) -> tuple[str, ...]:
    refs = value.get(name, [])
    if (
        not isinstance(refs, list)
        or not all(isinstance(item, str) and item.strip() for item in refs)
    ):
        raise SemanticResultError(f"{owner} {name} must be a reference list")
    return tuple(refs)
