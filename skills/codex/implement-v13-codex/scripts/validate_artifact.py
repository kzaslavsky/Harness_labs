#!/usr/bin/env python3
"""Validate a JSON artifact against JSON Schema and optional exact fields."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


MANDATORY_REVIEW_LENSES = (
    "l1_l2_contract_boundary",
    "security_privacy_destructive_behavior",
    "correctness",
)
MANDATORY_REVIEW_CHARGES = (
    "Challenge layer ownership, dependency direction, and contract boundaries.",
    "Challenge trust boundaries, sensitive-data handling, and destructive operations.",
    "Challenge state invariants, edge cases, and failure handling.",
)


def _lens_words(value: str) -> set[str]:
    """Return stable content words for duplicate-lens checks."""
    ignored = {
        "a", "an", "and", "against", "code", "diff", "for", "in", "of",
        "on", "review", "the", "to", "with",
    }
    return {
        word
        for word in re.findall(r"[a-z0-9]+", value.casefold())
        if word not in ignored
    }


def _validate_review_lenses(plan: dict[str, Any]) -> None:
    lenses = plan["review_lenses"]
    lens_ids = [lens["id"] for lens in lenses]
    if tuple(lens_ids[:3]) != MANDATORY_REVIEW_LENSES:
        raise ValueError("plan review lenses must begin with the three mandatory lenses in canonical order")
    if tuple(lens["charge"] for lens in lenses[:3]) != MANDATORY_REVIEW_CHARGES:
        raise ValueError("mandatory plan review lenses must use their canonical charges")
    if len(lens_ids) != len(set(lens_ids)):
        raise ValueError("plan review lens IDs must be unique")

    signatures: list[tuple[str, set[str]]] = []
    for lens in lenses:
        if len(lens["must_read"]) != len(set(lens["must_read"])):
            raise ValueError("plan review lens must_read paths must be unique")
        normalized_charge = " ".join(lens["charge"].casefold().split())
        signature = _lens_words(f'{lens["id"]} {lens["charge"]}')
        for prior_charge, prior_signature in signatures:
            shared = signature & prior_signature
            smaller = min(len(signature), len(prior_signature))
            if normalized_charge == prior_charge or (
                smaller >= 2 and len(shared) / smaller >= 0.8
            ):
                raise ValueError("plan review lenses must have materially distinct charges")
        signatures.append((normalized_charge, signature))


def _validate_plan(plan: dict[str, Any], input_manifest: Path | None) -> None:
    _validate_review_lenses(plan)
    steps = plan["steps"]
    step_ids = [step["id"] for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("plan step IDs must be unique")
    nodes = plan["task_dag"]["nodes"]
    if len(nodes) != len(set(nodes)) or set(nodes) != set(step_ids):
        raise ValueError("task DAG nodes must equal the unique plan step IDs")
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for edge in plan["task_dag"]["edges"]:
        source, target = edge["from"], edge["to"]
        if source not in adjacency or target not in adjacency or source == target:
            raise ValueError("task DAG edge references an invalid node")
        adjacency[source].append(target)
        indegree[target] += 1
    ready = [node for node, count in indegree.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for target in adjacency[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(nodes):
        raise ValueError("task DAG must be acyclic")
    total = float(plan["total_effort"])
    step_total = sum(float(step["effort"]) for step in steps)
    if abs(total - step_total) > 1e-9:
        raise ValueError("total_effort must equal the sum of step effort")
    critical = float(plan["critical_path_effort"])
    share = float(plan["critical_path_share"])
    if critical > total or abs(share - critical / total) > 1e-9:
        raise ValueError("critical path arithmetic is inconsistent")
    parallel = plan["parallelization"]
    groups = parallel["worker_groups"]
    if parallel["recommended"] and share > 0.60:
        raise ValueError("parallelization is forbidden above 0.60 critical-path share")
    claimed_steps: set[str] = set()
    claimed_paths: dict[str, str] = {}
    for group in groups:
        for step_id in group["step_ids"]:
            if step_id not in step_ids or step_id in claimed_steps:
                raise ValueError("parallel worker step ownership is invalid")
            claimed_steps.add(step_id)
        for path in group["write_paths"]:
            prior = claimed_paths.get(path)
            if prior is not None and parallel["shared_file_owner"] not in {prior, group["id"]}:
                raise ValueError("parallel worker write paths overlap without one shared owner")
            claimed_paths[path] = group["id"]
    if parallel["recommended"] and claimed_steps != set(step_ids):
        raise ValueError("parallel worker groups must claim every plan step")
    if input_manifest is not None:
        manifest = json.loads(input_manifest.read_text(encoding="utf-8"))
        all_inputs = {
            item["id"]: (item["sha256"], item["role"])
            for item in manifest.get("inputs", [])
        }
        required_ids = {
            item["id"] for item in manifest.get("inputs", []) if item.get("required", True)
        }
        acknowledgements = {
            item["input_id"]: (item["sha256"], item["role"])
            for item in plan["input_acknowledgements"]
        }
        if len(acknowledgements) != len(plan["input_acknowledgements"]) or not required_ids.issubset(acknowledgements) or any(
            input_id not in all_inputs or all_inputs[input_id] != value
            for input_id, value in acknowledgements.items()
        ):
            raise ValueError("plan input acknowledgements do not match required planning inputs")


def _validate_fix_result(result: dict[str, Any]) -> None:
    assigned = set(result["assigned_findings"])
    addressed = set(result["addressed_findings"])
    if not addressed.issubset(assigned):
        raise ValueError("addressed findings must be a subset of the fixer assignment")


def _validate_targeted_review(review: dict[str, Any]) -> None:
    fingerprints = [finding["fingerprint"] for finding in review["findings"]]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("targeted review finding fingerprints must be unique")


def validate(
    document_path: Path,
    schema_path: Path,
    expected: dict[str, Any],
    input_manifest: Path | None = None,
) -> None:
    """Validate syntax, schema, and caller-declared semantic witnesses."""
    import jsonschema  # type: ignore[import-not-found]

    document = json.loads(document_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(document, schema)
    if not isinstance(document, dict):
        raise ValueError("artifact must be a JSON object")
    for key, value in expected.items():
        if document.get(key) != value:
            raise ValueError(f"semantic mismatch for {key}")
    if schema_path.name == "plan.schema.json":
        _validate_plan(document, input_manifest)
    elif schema_path.name == "fix-result.schema.json":
        _validate_fix_result(document)
    elif schema_path.name == "targeted-review.schema.json":
        _validate_targeted_review(document)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("--expect", default="{}", help="JSON object of exact top-level fields")
    parser.add_argument("--input-manifest", type=Path)
    args = parser.parse_args()
    try:
        expected = json.loads(args.expect)
        if not isinstance(expected, dict):
            raise ValueError("--expect must be a JSON object")
        validate(args.document, args.schema, expected, args.input_manifest)
    except Exception as exc:
        print(json.dumps({"valid": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"valid": True, "document": str(args.document), "schema": str(args.schema)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
