#!/usr/bin/env python3
"""Durable finding-level closure control for REVIEWING repairs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from repair_preflight import (
    bind_test_command,
    certification_runtime_identity,
    effect_contract_sha256,
    repository_identity,
    solve_effect_constraints,
    validate_assertion_effects,
    validate_capability_manifest,
    validate_resolution_dataflow,
    validate_test_command,
)
from response_schema import canonical_schema_hashes, compile_transport_schema
from state_io import (
    StateError,
    atomic_write_json,
    canonical_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
)


PROTOCOL = "implement-v13-codex/review-closure-ledger/3"
V2_PROTOCOL = "implement-v13-codex/review-closure-ledger/2"
LEGACY_PROTOCOL = "implement-v13-codex/review-closure-ledger/1"
COMPLEXITIES = {"implementation", "architectural"}
REPAIR_ACTIONS = {"author_test", "design", "design_review", "fix", "targeted_review"}
ESCALATION_ACTIONS = {"reassign", "decompose", "operator"}
ATTEMPTS_BEFORE_ESCALATION = 3
EFFECT_CONTRACT_PROTOCOL = "implement-v13-codex/repair-effect-contract/1"
REPAIR_EFFECTS = {
    "failure_checkpoint",
    "blocked_queue",
    "failure_summary",
    "failure_event",
    "success_result",
    "success_receipt",
    "integration_artifact",
    "dispatcher_acknowledgement",
    "base_git_state",
}
EFFECT_DISPOSITIONS = (
    "must_persist",
    "must_remain_absent",
    "must_remain_unchanged",
)


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"{label} must be a nonempty string")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise StateError(f"{label} must be a nonempty string array")
    if len(value) != len(set(value)):
        raise StateError(f"{label} contains duplicates")
    return list(value)


def _optional_strings(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise StateError(f"{label} must be a string array")
    if len(value) != len(set(value)):
        raise StateError(f"{label} contains duplicates")
    return list(value)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _effect_contract(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("protocol") != EFFECT_CONTRACT_PROTOCOL:
        raise StateError(f"{label} must use {EFFECT_CONTRACT_PROTOCOL}")
    if set(value) != {"protocol", *EFFECT_DISPOSITIONS}:
        raise StateError(f"{label} contains unknown or missing fields")
    normalized = {"protocol": EFFECT_CONTRACT_PROTOCOL}
    seen: set[str] = set()
    for disposition in EFFECT_DISPOSITIONS:
        effects = _optional_strings(value.get(disposition), f"{label}.{disposition}")
        unknown = set(effects) - REPAIR_EFFECTS
        if unknown:
            raise StateError(f"{label}.{disposition} contains unknown effects: {sorted(unknown)}")
        overlap = seen.intersection(effects)
        if overlap:
            raise StateError(f"{label} assigns effects to multiple dispositions: {sorted(overlap)}")
        normalized[disposition] = effects
        seen.update(effects)
    missing = REPAIR_EFFECTS - seen
    if missing:
        raise StateError(f"{label} does not disposition every governed effect: {sorted(missing)}")
    return normalized


def _effect_disposition_map(contract: dict[str, Any]) -> dict[str, str]:
    return {
        effect: disposition
        for disposition in EFFECT_DISPOSITIONS
        for effect in contract[disposition]
    }


def _effect_conflicts(authoritative: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    required = _effect_disposition_map(authoritative)
    proposed = _effect_disposition_map(candidate)
    return [
        f"{effect}: authoritative={required[effect]}, candidate={proposed[effect]}"
        for effect in sorted(REPAIR_EFFECTS)
        if required[effect] != proposed[effect]
    ]


def _authoritative_effect_contract(closure: dict[str, Any]) -> dict[str, Any]:
    resolutions = closure.get("contract_resolution_history", [])
    if resolutions:
        return _effect_contract(resolutions[-1].get("effect_contract"), "operator resolution effect_contract")
    closure_test = closure.get("closure_test")
    if not isinstance(closure_test, dict):
        raise StateError("architectural closure has no recorded adversarial test")
    return _effect_contract(closure_test.get("effect_contract"), "closure test effect_contract")


def _source_path(root: Path, raw: Any, label: str) -> Path:
    value = _nonempty(raw, label)
    candidate = Path(value)
    target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise StateError(f"{label} escapes the repository root") from exc
    if not target.is_file():
        raise StateError(f"{label} is not a regular file")
    return target


def _normalize_source_bound_group(
    raw: dict[str, Any],
    *,
    repository_root: Path,
    closure_id: str,
    fingerprints: list[str],
    origin_reviewer: str,
    certification_runtime: dict[str, Any],
) -> dict[str, Any]:
    write_surfaces = _strings(raw.get("write_surfaces"), f"{closure_id}.write_surfaces")
    read_surfaces = _optional_strings(raw.get("read_surfaces"), f"{closure_id}.read_surfaces")
    declared_surfaces = set(write_surfaces + read_surfaces)
    raw_bindings = raw.get("source_bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise StateError(f"{closure_id}.source_bindings must be a nonempty array")
    bindings: list[dict[str, str]] = []
    bound_surfaces: set[str] = set()
    for index, binding in enumerate(raw_bindings):
        if not isinstance(binding, dict):
            raise StateError(f"{closure_id}.source_bindings[{index}] must be an object")
        surface = _nonempty(binding.get("surface"), "source binding surface")
        if surface not in declared_surfaces:
            raise StateError(f"{closure_id} source binding names an undeclared surface")
        source = _source_path(repository_root, binding.get("path"), "source binding path")
        digest = _nonempty(binding.get("sha256"), "source binding sha256")
        if sha256_file(source) != digest:
            raise StateError(f"{closure_id} source binding hash mismatch")
        try:
            relative = str(source.relative_to(repository_root))
        except ValueError as exc:  # pragma: no cover - guarded by _source_path
            raise StateError("source binding escapes repository root") from exc
        bindings.append({"surface": surface, "path": relative, "sha256": digest})
        bound_surfaces.add(surface)
    if bound_surfaces != declared_surfaces:
        raise StateError(f"{closure_id} does not source-bind every declared surface")

    raw_tests = raw.get("immutable_test_nodes")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise StateError(f"{closure_id}.immutable_test_nodes must be a nonempty array")
    tests: list[dict[str, Any]] = []
    test_ids: set[str] = set()
    covered: set[str] = set()
    for index, test in enumerate(raw_tests):
        if not isinstance(test, dict):
            raise StateError(f"{closure_id}.immutable_test_nodes[{index}] must be an object")
        node_id = _nonempty(test.get("node_id"), "immutable test node_id")
        if node_id in test_ids:
            raise StateError(f"{closure_id} has a duplicate immutable test node")
        source = _source_path(repository_root, test.get("source_path"), "immutable test source_path")
        digest = _nonempty(test.get("source_sha256"), "immutable test source_sha256")
        if sha256_file(source) != digest:
            raise StateError(f"{closure_id} immutable test source hash mismatch")
        covers = _strings(test.get("covers_surfaces"), "immutable test covers_surfaces")
        if not set(covers).issubset(declared_surfaces):
            raise StateError(f"{closure_id} immutable test covers an undeclared surface")
        command = _strings(test.get("command"), "immutable test command")
        if (
            len(command) >= 3
            and command[0] in {"python", "python3"}
            and command[1:3] == ["-m", "pytest"]
        ):
            command = [
                str(certification_runtime["interpreter_path"]),
                *command[1:],
            ]
        bound_command = bind_test_command(
            command,
            {"certification_runtime": certification_runtime},
        )
        tests.append({
            "node_id": node_id,
            "source_path": str(source.relative_to(repository_root)),
            "source_sha256": digest,
            "command": bound_command,
            "covers_surfaces": covers,
        })
        test_ids.add(node_id)
        covered.update(covers)
    if not set(write_surfaces).issubset(covered):
        raise StateError(f"{closure_id} has a write surface without an immutable test")

    edge_reasons = raw.get("dependency_edge_reasons", [])
    if not isinstance(edge_reasons, list) or any(not isinstance(item, dict) for item in edge_reasons):
        raise StateError(f"{closure_id}.dependency_edge_reasons must be an object array")
    normalized_reasons: list[dict[str, Any]] = []
    for edge in edge_reasons:
        dependency_id = _nonempty(edge.get("dependency_id"), "dependency edge dependency_id")
        normalized_reasons.append({
            "dependency_id": dependency_id,
            "reason": _nonempty(edge.get("reason"), "dependency edge reason"),
            "code_surfaces": _strings(edge.get("code_surfaces"), "dependency edge code_surfaces"),
            "test_nodes": _strings(edge.get("test_nodes"), "dependency edge test_nodes"),
        })
    if {item["dependency_id"] for item in normalized_reasons} != set(raw.get("depends_on", [])):
        raise StateError(f"{closure_id} must give exactly one source-bound reason per dependency")
    return {
        "closure_id": closure_id,
        "fingerprints": fingerprints,
        "origin_reviewer": origin_reviewer,
        "write_surfaces": write_surfaces,
        "read_surfaces": read_surfaces,
        "source_bindings": bindings,
        "immutable_test_nodes": tests,
        "dependency_edge_reasons": normalized_reasons,
    }


def _write_dependency_graph(
    path: Path,
    *,
    feature_run_id: str,
    repository_root: Path,
    closures: list[dict[str, Any]],
) -> tuple[Path, str]:
    nodes = [
        {
            key: closure[key]
            for key in (
                "closure_id",
                "fingerprints",
                "origin_reviewer",
                "write_surfaces",
                "read_surfaces",
                "source_bindings",
                "immutable_test_nodes",
            )
        }
        for closure in closures
    ]
    tests_by_closure = {
        closure["closure_id"]: {
            item["node_id"] for item in closure["immutable_test_nodes"]
        }
        for closure in closures
    }
    surfaces_by_closure = {
        closure["closure_id"]: set(
            closure["write_surfaces"] + closure["read_surfaces"]
        )
        for closure in closures
    }
    edges: list[dict[str, Any]] = []
    for closure in closures:
        for reason in closure["dependency_edge_reasons"]:
            dependency_id = reason["dependency_id"]
            allowed_surfaces = (
                surfaces_by_closure[closure["closure_id"]]
                | surfaces_by_closure[dependency_id]
            )
            if not set(reason["code_surfaces"]).issubset(allowed_surfaces):
                raise StateError("dependency edge reason names an unbound code surface")
            allowed_tests = (
                tests_by_closure[closure["closure_id"]]
                | tests_by_closure[dependency_id]
            )
            if not set(reason["test_nodes"]).issubset(allowed_tests):
                raise StateError("dependency edge reason names an unbound test node")
            edges.append({
                "from_closure_id": dependency_id,
                "to_closure_id": closure["closure_id"],
                "reason": reason["reason"],
                "code_surfaces": reason["code_surfaces"],
                "test_nodes": reason["test_nodes"],
            })
    graph = {
        "protocol": "implement-v13-codex/repair-dependency-graph/2",
        "feature_run_id": feature_run_id,
        "repository_root": str(repository_root),
        "repository_identity": repository_identity(repository_root),
        "closures": nodes,
        "edges": edges,
    }
    graph_path = path.parent / "repair-dependency-graph.v2.json"
    atomic_write_json(graph_path, graph)
    return graph_path, sha256_file(graph_path)


def _scheduler_policy(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"max_ready_age", "retry_penalty"}:
        raise StateError("source-bound closure scheduling requires max_ready_age and retry_penalty")
    normalized: dict[str, int] = {}
    for field in ("max_ready_age", "retry_penalty"):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise StateError(f"scheduler {field} must be a positive configured integer")
        normalized[field] = item
    return normalized


def create_ledger(
    path: Path,
    *,
    feature_run_id: str,
    groups: list[dict[str, Any]],
    repository_root: Path | None = None,
    scheduler_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if path.exists():
        raise StateError("closure ledger already exists")
    _nonempty(feature_run_id, "feature_run_id")
    if not isinstance(groups, list) or not groups:
        raise StateError("closure groups must be a nonempty array")
    closures: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    source_bound_requested = any(
        any(
            field in raw
            for field in (
                "write_surfaces",
                "read_surfaces",
                "source_bindings",
                "immutable_test_nodes",
                "dependency_edge_reasons",
            )
        )
        for raw in groups
    )
    if source_bound_requested and (
        repository_root is None or not repository_root.resolve().is_dir()
    ):
        raise StateError("source-bound closure groups require repository_root")
    configured_scheduler = (
        _scheduler_policy(scheduler_policy) if source_bound_requested else None
    )
    graph_runtime = (
        certification_runtime_identity() if source_bound_requested else None
    )
    for raw in groups:
        closure_id = _nonempty(raw.get("closure_id"), "closure_id")
        if closure_id in seen_ids:
            raise StateError("duplicate closure_id")
        fingerprints = _strings(raw.get("fingerprints"), f"{closure_id}.fingerprints")
        if seen_fingerprints.intersection(fingerprints):
            raise StateError("a finding fingerprint belongs to multiple closure groups")
        complexity = raw.get("complexity")
        if complexity not in COMPLEXITIES:
            raise StateError(f"{closure_id}.complexity must be implementation or architectural")
        origin_reviewer = _nonempty(raw.get("origin_reviewer"), f"{closure_id}.origin_reviewer")
        acceptance = _strings(raw.get("acceptance"), f"{closure_id}.acceptance")
        graph_fields = (
            _normalize_source_bound_group(
                raw,
                repository_root=repository_root.resolve(),  # type: ignore[union-attr]
                closure_id=closure_id,
                fingerprints=fingerprints,
                origin_reviewer=origin_reviewer,
                certification_runtime=graph_runtime,  # type: ignore[arg-type]
            )
            if source_bound_requested
            else {
                "write_surfaces": [],
                "read_surfaces": [],
                "source_bindings": [],
                "immutable_test_nodes": [],
                "dependency_edge_reasons": [],
            }
        )
        closures.append({
            "closure_id": closure_id,
            "fingerprints": fingerprints,
            "origin_reviewer": origin_reviewer,
            "complexity": complexity,
            "acceptance": acceptance,
            "depends_on": _optional_strings(raw.get("depends_on"), f"{closure_id}.depends_on"),
            "related_closures": _optional_strings(raw.get("related_closures"), f"{closure_id}.related_closures"),
            "excluded_fingerprints": _optional_strings(raw.get("excluded_fingerprints"), f"{closure_id}.excluded_fingerprints"),
            "status": "test_required",
            "closure_test": None,
            "design": None,
            "design_rejections": [],
            "attempts": [],
            "escalation_history": [],
            "contract_resolution_history": [],
            "design_rejection_baseline": None,
            "attempt_rejection_baseline": None,
            "active_resolution_sha256": None,
            "budget_activation_history": [],
            "assertion_map_path": None,
            "assertion_map_sha256": None,
            "capability_manifest_path": None,
            "capability_manifest_sha256": None,
            **graph_fields,
            "ready_age": 0,
            "scheduler_rejections": 0,
            "batch_history": [],
            "regression_evidence": [],
        })
        seen_ids.add(closure_id)
        seen_fingerprints.update(fingerprints)
    closure_ids = {item["closure_id"] for item in closures}
    for closure in closures:
        references = set(closure["depends_on"] + closure["related_closures"])
        if closure["closure_id"] in references or not references.issubset(closure_ids):
            raise StateError(f"{closure['closure_id']} has an invalid closure dependency/reference")
        if set(closure["fingerprints"]).intersection(closure["excluded_fingerprints"]):
            raise StateError(f"{closure['closure_id']} excludes one of its assigned fingerprints")
    visiting: set[str] = set()
    visited: set[str] = set()
    dependency_map = {item["closure_id"]: item["depends_on"] for item in closures}

    def visit(closure_id: str) -> None:
        if closure_id in visiting:
            raise StateError("closure dependency graph contains a cycle")
        if closure_id in visited:
            return
        visiting.add(closure_id)
        for dependency in dependency_map[closure_id]:
            visit(dependency)
        visiting.remove(closure_id)
        visited.add(closure_id)

    for closure_id in dependency_map:
        visit(closure_id)
    graph_path: Path | None = None
    graph_sha256: str | None = None
    if source_bound_requested:
        graph_path, graph_sha256 = _write_dependency_graph(
            path,
            feature_run_id=feature_run_id,
            repository_root=repository_root.resolve(),  # type: ignore[union-attr]
            closures=closures,
        )
    ledger = {
        "protocol": PROTOCOL,
        "feature_run_id": feature_run_id,
        "state_revision": 0,
        "attempts_before_escalation": ATTEMPTS_BEFORE_ESCALATION,
        "active_closure_id": next(item["closure_id"] for item in closures if not item["depends_on"]),
        "closures": closures,
        "dependency_graph_status": "validated" if source_bound_requested else "legacy_unbound",
        "dependency_graph_path": str(graph_path) if graph_path is not None else None,
        "dependency_graph_sha256": graph_sha256,
        "scheduler_policy": configured_scheduler,
        "scheduling_history": [],
    }
    atomic_write_json(path, ledger)
    return ledger


def _load(path: Path) -> dict[str, Any]:
    ledger = read_json(path)
    if ledger.get("protocol") not in {PROTOCOL, V2_PROTOCOL, LEGACY_PROTOCOL}:
        raise StateError("unsupported closure ledger protocol")
    return ledger


def _closure(ledger: dict[str, Any], closure_id: str) -> dict[str, Any]:
    matches = [item for item in ledger.get("closures", []) if item.get("closure_id") == closure_id]
    if len(matches) != 1:
        raise StateError("closure ledger does not contain exactly one requested closure")
    return matches[0]


def _closure_rejections(closure: dict[str, Any]) -> int:
    attempts = closure.get("attempts", [])
    design_rejections = closure.get("design_rejections", [])
    return (
        sum(item.get("status") == "rejected" for item in attempts if isinstance(item, dict))
        + len(design_rejections if isinstance(design_rejections, list) else [])
    )


def _save(path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    ledger["state_revision"] = int(ledger.get("state_revision", 0)) + 1
    closed = {item["closure_id"] for item in ledger["closures"] if item["status"] == "closed"}
    ready_items = [
        item for item in ledger["closures"]
        if item["status"] not in {"closed", "blocked"}
        and set(item.get("depends_on", [])).issubset(closed)
    ]
    policy = ledger.get("scheduler_policy")
    selected = ready_items[0] if ready_items else None
    if ledger.get("dependency_graph_status") == "validated":
        policy = _scheduler_policy(policy)
        prior_active = ledger.get("active_closure_id")
        for item in ready_items:
            item["ready_age"] = min(
                int(item.get("ready_age", 0)) + (item["closure_id"] != prior_active),
                policy["max_ready_age"],
            )
            item["scheduler_rejections"] = _closure_rejections(item)
        indexed = {item["closure_id"]: index for index, item in enumerate(ledger["closures"])}
        ready_items.sort(
            key=lambda item: (
                -int(item.get("ready_age", 0)),
                min(int(item.get("scheduler_rejections", 0)), policy["retry_penalty"]),
                indexed[item["closure_id"]],
            )
        )
        prior_item = next(
            (item for item in ready_items if item["closure_id"] == prior_active),
            None,
        )
        pin_intermediate = bool(
            prior_item
            and (
                prior_item["status"]
                in {"design_review_required", "fix_running", "rereview_required"}
                or (
                    prior_item["status"] == "design_required"
                    and _closure_rejections(prior_item) == 0
                )
                or (
                    prior_item["status"] == "test_required"
                    and not prior_item.get("escalation_history")
                )
            )
        )
        selected = prior_item if pin_intermediate else (
            ready_items[0] if ready_items else None
        )
        if selected is not None:
            selected["ready_age"] = 0
        reordered = bool(selected and prior_active and selected["closure_id"] != prior_active)
        ledger.setdefault("scheduling_history", []).append({
            "event": "repair_starvation_reordered" if reordered else "repair_batch_selected",
            "selected_closure_id": selected["closure_id"] if selected else "",
            "prior_active_closure_id": prior_active or "",
            "ready": [
                {
                    "closure_id": item["closure_id"],
                    "ready_age": item["ready_age"],
                    "retry_penalty": min(item["scheduler_rejections"], policy["retry_penalty"]),
                }
                for item in ready_items
            ],
        })
    ledger["active_closure_id"] = selected["closure_id"] if selected else ""
    atomic_write_json(path, ledger)
    return ledger


def cas_save_ledger(
    path: Path,
    expected_revision: int,
    updates: dict[str, Any],
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Migration-only ledger CAS; caller must already hold the ledger authority."""
    ledger = _load(path)
    if ledger.get("state_revision") != expected_revision:
        raise StateError(
            f"stale closure-ledger revision: expected {expected_revision}, "
            f"found {ledger.get('state_revision')}"
        )
    if expected_sha256 is not None and sha256_file(path) != expected_sha256:
        raise StateError("stale closure-ledger hash")
    reserved = {"protocol", "feature_run_id", "state_revision", "closures"}
    if reserved.intersection(updates):
        raise StateError("closure-ledger migration cannot replace identity or history")
    migrated = dict(ledger)
    migrated.update(updates)
    migrated["state_revision"] = expected_revision + 1
    atomic_write_json(path, migrated)
    return migrated


def attempt_history_sha256(closure: dict[str, Any]) -> str:
    return _canonical_sha256(closure.get("attempts", []))


def _design_rejections(closure: dict[str, Any]) -> list[dict[str, Any]]:
    value = closure.setdefault("design_rejections", [])
    if not isinstance(value, list):
        raise StateError("closure design_rejections must be an array")
    return value


def _commands(value: Any, label: str) -> list[Any]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, (str, dict)) for item in value)
    ):
        raise StateError(f"{label} must be a nonempty command array")
    return value


def _post_resolution_design_rejections(closure: dict[str, Any]) -> int:
    """Count only designs rejected under the currently authoritative operator contract."""
    history = _design_rejections(closure)
    baseline = closure.get("design_rejection_baseline")
    if baseline is None:
        baseline = 0
        if closure.get("active_resolution_sha256") is not None:
            raise StateError("active operator resolution lacks a design rejection baseline")
    if not isinstance(baseline, int) or baseline < 0 or baseline > len(history):
        raise StateError("closure design_rejection_baseline is invalid")
    return len(history) - baseline


def _post_resolution_attempt_rejections(closure: dict[str, Any]) -> int:
    """Count only fixer attempts rejected under the current operator contract."""
    attempts = closure.get("attempts", [])
    if not isinstance(attempts, list):
        raise StateError("closure attempts must be an array")
    rejected = sum(item.get("status") == "rejected" for item in attempts)
    baseline = closure.get("attempt_rejection_baseline")
    if baseline is None:
        baseline = 0
        if closure.get("active_resolution_sha256") is not None:
            raise StateError("active operator resolution lacks an attempt rejection baseline")
    if not isinstance(baseline, int) or baseline < 0 or baseline > rejected:
        raise StateError("closure attempt_rejection_baseline is invalid")
    return rejected - baseline


def _resolution_sha256(resolution: dict[str, Any]) -> str:
    value = resolution.get("profile_sha256")
    if isinstance(value, str) and len(value) == 64:
        return value
    return _canonical_sha256(resolution)


def _budget_history(closure: dict[str, Any]) -> list[dict[str, Any]]:
    history = closure.setdefault("budget_activation_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise StateError("closure budget_activation_history must be an object array")
    hashes = [item.get("resolution_sha256") for item in history]
    if len(hashes) != len(set(hashes)):
        raise StateError("post-resolution budget was activated more than once")
    return history


def _activate_resolution_budgets(
    closure: dict[str, Any],
    resolution_sha256: str,
    *,
    design: bool,
    attempt: bool,
    migration_recovery: bool,
) -> None:
    history = _budget_history(closure)
    entry = next(
        (item for item in history if item.get("resolution_sha256") == resolution_sha256),
        None,
    )
    new_entry = entry is None
    if entry is None:
        entry = {
            "resolution_sha256": resolution_sha256,
            "design_activated": False,
            "attempt_activated": False,
            "design_rejection_baseline": None,
            "attempt_rejection_baseline": None,
            "migration_recovery": migration_recovery,
        }
        history.append(entry)
    if design:
        if entry.get("design_activated") is True or (
            not new_entry and closure.get("design_rejection_baseline") is not None
        ):
            raise StateError("post-resolution design budget is already active")
        baseline = len(_design_rejections(closure))
        prior = closure.get("design_rejection_baseline")
        if isinstance(prior, int) and baseline < prior:
            raise StateError("post-resolution design baseline cannot decrease")
        closure["design_rejection_baseline"] = baseline
        entry["design_rejection_baseline"] = baseline
        entry["design_activated"] = True
    if attempt:
        if entry.get("attempt_activated") is True or (
            not new_entry and closure.get("attempt_rejection_baseline") is not None
        ):
            raise StateError("post-resolution attempt budget is already active")
        attempts = closure.get("attempts", [])
        if not isinstance(attempts, list):
            raise StateError("closure attempts must be an array")
        baseline = sum(item.get("status") == "rejected" for item in attempts)
        prior = closure.get("attempt_rejection_baseline")
        if isinstance(prior, int) and baseline < prior:
            raise StateError("post-resolution attempt baseline cannot decrease")
        closure["attempt_rejection_baseline"] = baseline
        entry["attempt_rejection_baseline"] = baseline
        entry["attempt_activated"] = True
    closure["active_resolution_sha256"] = resolution_sha256


def _archive_rejected_design(closure: dict[str, Any], *, stage: str) -> None:
    design = closure.get("design")
    if not isinstance(design, dict):
        return
    review = design.get("review")
    rejected = bool(design.get("compatibility_conflicts")) or (
        isinstance(review, dict) and review.get("approved") is not True
    )
    if not rejected:
        return
    designer_receipt_id = _nonempty(design.get("designer_receipt_id"), "designer_receipt_id")
    review_receipt_id = review.get("reviewer_receipt_id") if isinstance(review, dict) else ""
    history = _design_rejections(closure)
    if any(item.get("designer_receipt_id") == designer_receipt_id for item in history):
        return
    history.append({
        "rejection": len(history) + 1,
        "stage": stage,
        "designer_receipt_id": designer_receipt_id,
        "strategy_family": _nonempty(design.get("strategy_family"), "strategy_family"),
        "result_path": _nonempty(design.get("result_path"), "result_path"),
        "result_sha256": _nonempty(design.get("result_sha256"), "result_sha256"),
        "reviewer_receipt_id": review_receipt_id if isinstance(review_receipt_id, str) else "",
        "compatibility_conflicts": list(design.get("compatibility_conflicts", [])),
        "evidence": list(review.get("evidence", [])) if isinstance(review, dict) else list(design.get("compatibility_conflicts", [])),
    })


def backfill_design_rejections(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Migrate verified legacy rejected-design provenance before bounded continuation."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["complexity"] != "architectural" or closure["status"] not in {"design_required", "design_review_required"}:
        raise StateError("design rejection backfill requires an active architectural design closure")
    entries = result.get("entries")
    if not isinstance(entries, list) or not entries:
        raise StateError("design rejection backfill requires entries")
    history = _design_rejections(closure)
    for raw in entries:
        if not isinstance(raw, dict):
            raise StateError("design rejection backfill entry must be an object")
        designer_receipt_id = _nonempty(raw.get("designer_receipt_id"), "designer_receipt_id")
        if any(item.get("designer_receipt_id") == designer_receipt_id for item in history):
            continue
        history.append({
            "rejection": len(history) + 1,
            "stage": "legacy_backfill",
            "designer_receipt_id": designer_receipt_id,
            "strategy_family": _nonempty(raw.get("strategy_family"), "strategy_family"),
            "result_path": _nonempty(raw.get("result_path"), "result_path"),
            "result_sha256": _nonempty(raw.get("result_sha256"), "result_sha256"),
            "reviewer_receipt_id": _nonempty(raw.get("reviewer_receipt_id"), "reviewer_receipt_id"),
            "compatibility_conflicts": _optional_strings(raw.get("compatibility_conflicts"), "compatibility_conflicts"),
            "evidence": _strings(raw.get("evidence"), "evidence"),
        })
    return _save(path, ledger)


def _normalized_test_commands(
    raw_commands: Any,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw_commands, list) or not raw_commands:
        raise StateError("commands must be a nonempty array")
    normalized: list[dict[str, Any]] = []
    runtime = manifest["certification_runtime"]
    interpreter = str(runtime["interpreter_path"])
    for raw in raw_commands:
        if isinstance(raw, dict):
            normalized.append(validate_test_command(raw, manifest))
            continue
        if not isinstance(raw, str) or not raw.strip():
            raise StateError("commands contains an invalid entry")
        if any(token in raw for token in ("|", ">", "<", "$(", "`", "&&", "||", ";")):
            raise StateError("legacy_unpinned test command contains shell operators")
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            raise StateError("legacy_unpinned test command is not parseable") from exc
        while parts and (
            parts[0].startswith("TMPDIR=")
            or parts[0].startswith("PYTHONDONTWRITEBYTECODE=")
        ):
            parts.pop(0)
        if len(parts) >= 3 and parts[0] in {"python3", interpreter} and parts[1:3] == ["-m", "pytest"]:
            argv = [interpreter, "-m", "pytest", *parts[3:]]
        elif parts and parts[0] == "pytest":
            argv = [interpreter, "-m", "pytest", *parts[1:]]
        else:
            raise StateError("legacy_unpinned test command requires explicit binding")
        normalized.append(bind_test_command(argv, manifest))
    return normalized


def record_test(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "test_required":
        raise StateError("closure test may be recorded only when test_required")
    author_role = _nonempty(result.get("author_role"), "author_role")
    if author_role != closure["origin_reviewer"]:
        raise StateError("closure test author must be the originating reviewer role")
    receipt_id = _nonempty(result.get("author_receipt_id"), "author_receipt_id")
    effect_contract = _effect_contract(
        result.get("effect_contract"), "closure test effect_contract"
    )
    test_paths = _strings(result.get("test_paths"), "test_paths")
    capability_path_raw = _nonempty(
        result.get("capability_manifest_path"), "capability_manifest_path"
    )
    capability_path = Path(capability_path_raw)
    if not capability_path.is_absolute():
        raise StateError("capability_manifest_path must be absolute")
    capability_path = capability_path.resolve()
    try:
        capability_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("capability manifest must be run-owned") from exc
    capability_sha256 = _nonempty(
        result.get("capability_manifest_sha256"), "capability_manifest_sha256"
    )
    manifest = validate_capability_manifest(
        capability_path,
        capability_sha256,
        repository_root=Path(
            _nonempty(result.get("repository_root"), "repository_root")
        ),
        feature_run_id=ledger["feature_run_id"],
    )
    commands = _normalized_test_commands(result.get("commands"), manifest)
    assertion_map = {
        "protocol": "implement-v13-codex/repair-assertion-map/2",
        "feature_run_id": ledger["feature_run_id"],
        "closure_id": closure_id,
        "repository_root": _nonempty(
            result.get("repository_root"), "repository_root"
        ),
        "repository_identity": _nonempty(
            result.get("repository_identity"), "repository_identity"
        ),
        "test": {
            "source_path": _nonempty(
                result.get("test_source_path"), "test_source_path"
            ),
            "source_sha256": _nonempty(
                result.get("test_source_sha256"), "test_source_sha256"
            ),
            "node_id": _nonempty(result.get("test_node_id"), "test_node_id"),
            "command": commands[0],
        },
        "effect_contract_sha256": effect_contract_sha256(effect_contract),
        "assertions": result.get("assertions"),
    }
    validated_assertions = validate_assertion_effects(
        assertion_map,
        feature_run_id=ledger["feature_run_id"],
        closure_id=closure_id,
        effect_contract=effect_contract,
        test_paths=test_paths,
        commands=commands,
    )
    assertion_dir = path.parent / "repair-assertion-maps"
    assertion_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    assertion_path = assertion_dir / (
        hashlib.sha256(closure_id.encode("utf-8")).hexdigest()
        + ".repair-assertion-map.v1.json"
    )
    if assertion_path.exists() and sha256_file(assertion_path) != validated_assertions[
        "assertion_map_sha256"
    ]:
        raise StateError("repair assertion map artifact already exists with different bytes")
    if not assertion_path.exists():
        atomic_write_json(assertion_path, assertion_map)
    assertion_sha256 = sha256_file(assertion_path)
    if assertion_sha256 != validated_assertions["assertion_map_sha256"]:
        raise StateError("persisted repair assertion map hash mismatch")
    supplemental_resolution = closure.get("supplemental_test_resolution")
    closure["closure_test"] = {
        "author_role": author_role,
        "author_receipt_id": receipt_id,
        "test_paths": test_paths,
        "commands": commands,
        "observed_failure": result.get("observed_failure") is True,
        "evidence": _strings(result.get("evidence"), "evidence"),
        "effect_contract": effect_contract,
        "assertion_map_path": str(assertion_path.resolve()),
        "assertion_map_sha256": assertion_sha256,
        "capability_manifest_path": str(capability_path),
        "capability_manifest_sha256": capability_sha256,
    }
    closure["assertion_map_path"] = str(assertion_path.resolve())
    closure["assertion_map_sha256"] = assertion_sha256
    closure["capability_manifest_path"] = str(capability_path)
    closure["capability_manifest_sha256"] = capability_sha256
    if (
        not closure["closure_test"]["observed_failure"]
        and not isinstance(supplemental_resolution, dict)
    ):
        raise StateError("adversarial closure test must fail before the repair")
    if isinstance(supplemental_resolution, dict):
        closure["closure_test"]["supplemental"] = True
        closure["closure_test"]["supplements_verification_sha256"] = (
            supplemental_resolution["verification_result_sha256"]
        )
    closure["status"] = "design_required" if closure["complexity"] == "architectural" else "ready_for_fix"
    return _save(path, ledger)


def resolve_legacy_assertion_conflict(
    path: Path, closure_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Reopen only the test/design cycle after a legacy map fails verification.

    This operator path is deliberately narrower than a general contract rewrite:
    it preserves the original test, design, attempts, and verifier evidence, and
    authorizes one source-bound supplemental immutable test for the unchanged
    canonical effect contract.
    """
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if result.get("authority") != "operator":
        raise StateError("legacy assertion conflict resolution requires operator authority")
    if result.get("decision") != "supplemental_immutable_test":
        raise StateError("legacy assertion conflict resolution has an unsupported decision")
    closure_test = closure.get("closure_test")
    if not isinstance(closure_test, dict):
        raise StateError("legacy assertion conflict resolution requires closure-test evidence")
    if closure_test.get("assertion_map_sha256") is not None:
        raise StateError("legacy assertion conflict resolution requires an unbound legacy test")
    if isinstance(closure.get("supplemental_test_resolution"), dict):
        raise StateError("legacy assertion conflict was already resolved")

    verification_path_raw = _nonempty(
        result.get("verification_result_path"), "verification_result_path"
    )
    verification_path = Path(verification_path_raw)
    if not verification_path.is_absolute():
        raise StateError("verification result path must be absolute")
    verification_path = verification_path.resolve()
    try:
        verification_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("verification result must be run-owned") from exc
    verification_sha256 = _nonempty(
        result.get("verification_result_sha256"), "verification_result_sha256"
    )
    if (
        not verification_path.is_file()
        or sha256_file(verification_path) != verification_sha256
    ):
        raise StateError("verification result hash mismatch")
    verification = read_json(verification_path)
    if (
        verification.get("protocol")
        != "implement-v13-codex/assertion-map-verification/1"
        or verification.get("feature_run_id") != ledger["feature_run_id"]
        or verification.get("role") != "assertion_map_verifier"
        or verification.get("status") != "blocked"
    ):
        raise StateError("verification result is not a blocked independent map review")
    verification_effect_contract = _effect_contract(
        verification.get("effect_contract"), "verification effect_contract"
    )
    legacy_effect_contract_raw = closure_test.get("effect_contract")
    if legacy_effect_contract_raw is None:
        # A pre-contract legacy test cannot supply the contract that the
        # independent verifier was introduced to establish. Bind the recovery
        # to the verifier's hashed result; every later supplemental test must
        # then preserve this adopted contract through contract_resolution_history.
        authoritative_effect_contract = verification_effect_contract
    else:
        authoritative_effect_contract = _effect_contract(
            legacy_effect_contract_raw, "closure test effect_contract"
        )
        if verification_effect_contract != authoritative_effect_contract:
            raise StateError("verification result changes the canonical effect contract")
    evidence = _strings(result.get("evidence"), "evidence")
    authorization_sha256 = _nonempty(
        result.get("operator_authorization_sha256"),
        "operator_authorization_sha256",
    )
    if len(authorization_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in authorization_sha256
    ):
        raise StateError("operator_authorization_sha256 must be lowercase sha256")

    preserved = json.loads(json.dumps(closure_test))
    closure.setdefault("superseded_closure_tests", []).append({
        "closure_test": preserved,
        "reason": "legacy_assertion_map_evidence_conflict",
        "verification_result_path": str(verification_path),
        "verification_result_sha256": verification_sha256,
        "evidence": list(verification.get("evidence", [])),
    })
    resolution = {
        "authority": "operator",
        "decision": "supplemental_immutable_test",
        "operator_authorization_sha256": authorization_sha256,
        "verification_result_path": str(verification_path),
        "verification_result_sha256": verification_sha256,
        "effect_contract": authoritative_effect_contract,
        "evidence": evidence,
    }
    closure["supplemental_test_resolution"] = resolution
    closure.setdefault("contract_resolution_history", []).append(resolution)
    closure["closure_test"] = None
    closure["design"] = None
    for field in (
        "assertion_map_path",
        "assertion_map_sha256",
        "capability_manifest_path",
        "capability_manifest_sha256",
    ):
        closure.pop(field, None)
    _activate_resolution_budgets(
        closure,
        authorization_sha256,
        design=True,
        attempt=True,
        migration_recovery=False,
    )
    closure["status"] = "test_required"
    return _save(path, ledger)


def backfill_assertion_map(
    path: Path, closure_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Bind independently verified assertion evidence to one legacy closure."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    closure_test = closure.get("closure_test")
    if not isinstance(closure_test, dict):
        raise StateError("assertion-map backfill requires legacy closure-test evidence")
    if closure_test.get("assertion_map_sha256") is not None:
        raise StateError("closure assertion map is already bound")
    verifier_role = _nonempty(
        result.get("independent_verifier_role"), "independent_verifier_role"
    )
    if verifier_role in {
        closure.get("origin_reviewer"),
        closure_test.get("author_role"),
    }:
        raise StateError("assertion-map backfill verifier must be independent")
    verifier_receipt_id = _nonempty(
        result.get("verification_receipt_id"), "verification_receipt_id"
    )
    assertion_path_raw = _nonempty(
        result.get("assertion_map_path"), "assertion_map_path"
    )
    assertion_path = Path(assertion_path_raw)
    if not assertion_path.is_absolute():
        raise StateError("assertion-map backfill path must be absolute")
    assertion_path = assertion_path.resolve()
    try:
        assertion_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("assertion-map backfill artifact must be run-owned") from exc
    assertion_sha256 = _nonempty(
        result.get("assertion_map_sha256"), "assertion_map_sha256"
    )
    if not assertion_path.is_file() or sha256_file(assertion_path) != assertion_sha256:
        raise StateError("assertion-map backfill artifact hash mismatch")
    effect_contract = _effect_contract(
        result.get("effect_contract"), "assertion-map backfill effect_contract"
    )
    assertion_map = read_json(assertion_path)
    validate_assertion_effects(
        assertion_map,
        feature_run_id=ledger["feature_run_id"],
        closure_id=closure_id,
        effect_contract=effect_contract,
        test_paths=_strings(closure_test.get("test_paths"), "legacy test_paths"),
        commands=_commands(closure_test.get("commands"), "legacy commands"),
    )
    capability_path_raw = _nonempty(
        result.get("capability_manifest_path"), "capability_manifest_path"
    )
    capability_path = Path(capability_path_raw)
    if not capability_path.is_absolute():
        raise StateError("capability_manifest_path must be absolute")
    capability_path = capability_path.resolve()
    try:
        capability_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("capability manifest must be run-owned") from exc
    capability_sha256 = _nonempty(
        result.get("capability_manifest_sha256"), "capability_manifest_sha256"
    )
    validate_capability_manifest(
        capability_path,
        capability_sha256,
        repository_root=Path(assertion_map["repository_root"]),
        feature_run_id=ledger["feature_run_id"],
    )
    closure_test.update(
        effect_contract=effect_contract,
        assertion_map_path=str(assertion_path),
        assertion_map_sha256=assertion_sha256,
        capability_manifest_path=str(capability_path),
        capability_manifest_sha256=capability_sha256,
    )
    closure.update(
        assertion_map_path=str(assertion_path),
        assertion_map_sha256=assertion_sha256,
        capability_manifest_path=str(capability_path),
        capability_manifest_sha256=capability_sha256,
    )
    closure.setdefault("assertion_backfill_history", []).append(
        {
            "assertion_map_path": str(assertion_path),
            "assertion_map_sha256": assertion_sha256,
            "independent_verifier_role": verifier_role,
            "verification_receipt_id": verifier_receipt_id,
        }
    )
    return _save(path, ledger)


def record_design(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "design_required":
        raise StateError("repair design may be recorded only when design_required")
    _archive_rejected_design(closure, stage="design_review")
    if _post_resolution_design_rejections(closure) >= int(ledger["attempts_before_escalation"]):
        closure["status"] = "escalation_required"
        return _save(path, ledger)
    effect_contract = _effect_contract(result.get("effect_contract"), "repair design effect_contract")
    conflicts = _effect_conflicts(_authoritative_effect_contract(closure), effect_contract)
    closure["design"] = {
        "designer_receipt_id": _nonempty(result.get("designer_receipt_id"), "designer_receipt_id"),
        "strategy_family": _nonempty(result.get("strategy_family"), "strategy_family"),
        "result_path": _nonempty(result.get("result_path"), "result_path"),
        "result_sha256": _nonempty(result.get("result_sha256"), "result_sha256"),
        "effect_contract": effect_contract,
        "compatibility_conflicts": conflicts,
        "review": None,
    }
    closure["status"] = "design_required" if conflicts else "design_review_required"
    if conflicts:
        _archive_rejected_design(closure, stage="effect_contract_gate")
        if _post_resolution_design_rejections(closure) >= int(ledger["attempts_before_escalation"]):
            closure["status"] = "escalation_required"
    return _save(path, ledger)


def record_design_review(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "design_review_required" or not isinstance(closure.get("design"), dict):
        raise StateError("design review may be recorded only when design_review_required")
    reviewer_role = _nonempty(result.get("reviewer_role"), "reviewer_role")
    reviewer_receipt_id = _nonempty(result.get("reviewer_receipt_id"), "reviewer_receipt_id")
    if reviewer_role != closure["origin_reviewer"]:
        raise StateError("architectural design must be reviewed by the originating reviewer role")
    if reviewer_receipt_id == closure["design"]["designer_receipt_id"]:
        raise StateError("architectural designer cannot approve its own design")
    approved = result.get("approved") is True
    effect_contract = _effect_contract(
        result.get("effect_contract"), "repair design review effect_contract"
    )
    conflicts = _effect_conflicts(_authoritative_effect_contract(closure), effect_contract)
    conflicts.extend(_effect_conflicts(closure["design"]["effect_contract"], effect_contract))
    conflicts = list(dict.fromkeys(conflicts))
    compatible = not conflicts
    closure["design"]["review"] = {
        "reviewer_role": reviewer_role,
        "reviewer_receipt_id": reviewer_receipt_id,
        "approved": approved and compatible,
        "requested_approval": approved,
        "effect_contract": effect_contract,
        "compatibility_conflicts": conflicts,
        "evidence": _strings(result.get("evidence"), "evidence"),
    }
    closure["status"] = "ready_for_fix" if approved and compatible else "design_required"
    if closure["status"] == "design_required":
        _archive_rejected_design(closure, stage="design_review")
        if _post_resolution_design_rejections(closure) >= int(ledger["attempts_before_escalation"]):
            closure["status"] = "escalation_required"
    return _save(path, ledger)


def resolve_design_contradiction(
    path: Path, closure_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Activate one generic, run-owned operator profile after executable proof."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "blocked" or not closure.get("escalation_history"):
        raise StateError("design contradiction resolution requires an operator-blocked closure")
    if closure["escalation_history"][-1].get("action") != "operator":
        raise StateError("design contradiction resolution requires the latest operator escalation")
    if result.get("authority") != "operator":
        raise StateError("design contradiction resolution requires operator authority")
    profile_path_raw = _nonempty(
        result.get("operator_resolution_profile_path"),
        "operator_resolution_profile_path",
    )
    profile_path = Path(profile_path_raw)
    if not profile_path.is_absolute():
        raise StateError("operator resolution profile path must be absolute")
    profile_path = profile_path.resolve()
    try:
        profile_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("operator resolution profile must be run-owned") from exc
    profile_sha256 = _nonempty(
        result.get("operator_resolution_profile_sha256"),
        "operator_resolution_profile_sha256",
    )
    if not profile_path.is_file() or sha256_file(profile_path) != profile_sha256:
        raise StateError("operator resolution profile hash mismatch")
    profile = read_json(profile_path)
    closure_test = closure.get("closure_test")
    if not isinstance(closure_test, dict):
        raise StateError("operator resolution requires immutable closure-test evidence")
    assertion_path = Path(str(closure_test.get("assertion_map_path", "")))
    if (
        not assertion_path.is_absolute()
        or not assertion_path.is_file()
        or sha256_file(assertion_path) != closure_test.get("assertion_map_sha256")
    ):
        raise StateError("operator resolution assertion-map evidence mismatch")
    assertion_map = read_json(assertion_path)
    dataflow = validate_resolution_dataflow(
        profile,
        repository_identity_sha256=assertion_map["repository_identity"],
        feature_run_id=ledger["feature_run_id"],
        closure_id=closure_id,
        test_node_id=assertion_map["test"]["node_id"],
        test_source_path=assertion_map["test"]["source_path"],
        test_source_sha256=assertion_map["test"]["source_sha256"],
        assertion_map_sha256=closure_test["assertion_map_sha256"],
    )
    effect_contract = _effect_contract(
        profile.get("effect_contract"), "operator resolution effect_contract"
    )
    requested_contract = result.get("effect_contract")
    if requested_contract is not None and _effect_contract(
        requested_contract, "requested operator resolution effect_contract"
    ) != effect_contract:
        raise StateError("operator resolution effect contract mismatches profile")
    if any(
        item.get("profile_sha256") == profile_sha256
        for item in closure.get("contract_resolution_history", [])
    ):
        raise StateError("operator resolution profile was already activated")
    decision = profile["resolution_kind"]
    if result.get("decision") not in {None, decision}:
        raise StateError("operator resolution decision mismatches profile")
    resolution = {
        "authority": "operator",
        "decision": decision,
        "effect_contract": effect_contract,
        "evidence": list(profile["evidence"]),
        "profile_path": str(profile_path),
        "profile_sha256": profile_sha256,
        "dataflow_proof_sha256": dataflow["dataflow_proof_sha256"],
        "live_dataflow_proof_sha256": dataflow[
            "live_dataflow_proof_sha256"
        ],
        "active_subject_sha256": dataflow["active_subject_sha256"],
    }
    closure.setdefault("contract_resolution_history", []).append(resolution)
    _activate_resolution_budgets(
        closure,
        profile_sha256,
        design=True,
        attempt=True,
        migration_recovery=False,
    )
    resolution["design_rejection_baseline"] = closure[
        "design_rejection_baseline"
    ]
    resolution["attempt_rejection_baseline"] = closure[
        "attempt_rejection_baseline"
    ]
    closure["design"] = None
    closure["status"] = "design_required"
    return _save(path, ledger)


def activate_post_resolution_design_budget(path: Path, closure_id: str) -> dict[str, Any]:
    """Recover a pre-fix resolution that omitted its preserved-history baseline."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    resolutions = closure.get("contract_resolution_history", [])
    if closure.get("status") != "blocked" or not isinstance(resolutions, list) or not resolutions:
        raise StateError("post-resolution budget activation requires a resolved blocked closure")
    escalations = closure.get("escalation_history", [])
    if not isinstance(escalations, list) or not escalations or escalations[-1].get("action") != "operator":
        raise StateError("post-resolution budget activation requires operator escalation evidence")
    resolution_sha256 = _resolution_sha256(resolutions[-1])
    _activate_resolution_budgets(
        closure,
        resolution_sha256,
        design=True,
        attempt=False,
        migration_recovery=True,
    )
    baseline = closure["design_rejection_baseline"]
    resolutions[-1]["design_rejection_baseline"] = baseline
    closure.setdefault("budget_recovery_history", []).append({
        "reason": "activate_one_fresh_design_budget_after_operator_contract_resolution",
        "preserved_rejections": baseline,
        "resolution_sha256": resolution_sha256,
    })
    closure["design"] = None
    closure["status"] = "design_required"
    return _save(path, ledger)


def activate_post_resolution_attempt_budget(path: Path, closure_id: str) -> dict[str, Any]:
    """Recover a resolved closure whose historical fixer rejections still consume its new budget."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    resolutions = closure.get("contract_resolution_history", [])
    if closure.get("status") not in {"design_required", "blocked"} or not isinstance(resolutions, list) or not resolutions:
        raise StateError("post-resolution attempt budget activation requires a resolved closure")
    resolution_sha256 = _resolution_sha256(resolutions[-1])
    _activate_resolution_budgets(
        closure,
        resolution_sha256,
        design=False,
        attempt=True,
        migration_recovery=True,
    )
    baseline = closure["attempt_rejection_baseline"]
    resolutions[-1]["attempt_rejection_baseline"] = baseline
    closure.setdefault("budget_recovery_history", []).append({
        "reason": "activate_one_fresh_fixer_budget_after_operator_contract_resolution",
        "preserved_rejected_attempts": baseline,
        "resolution_sha256": resolution_sha256,
    })
    return _save(path, ledger)


def start_attempt(path: Path, closure_id: str, attempt: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "ready_for_fix":
        raise StateError("repair attempt may start only when ready_for_fix")
    acknowledged = _nonempty(attempt.get("attempt_history_sha256"), "attempt_history_sha256")
    current_hash = attempt_history_sha256(closure)
    if acknowledged != current_hash:
        raise StateError("repair attempt did not acknowledge the complete prior-attempt history")
    family = _nonempty(attempt.get("strategy_family"), "strategy_family")
    rejected_families = {
        item["strategy_family"] for item in closure["attempts"] if item.get("status") == "rejected"
    }
    if family in rejected_families:
        raise StateError("repair attempt repeats a previously rejected strategy family")
    invocation_id = _nonempty(attempt.get("invocation_id"), "invocation_id")
    test_receipt = closure["closure_test"]["author_receipt_id"]
    if invocation_id == test_receipt:
        raise StateError("closure-test author cannot be the repair fixer")
    closure["attempts"].append({
        "attempt": len(closure["attempts"]) + 1,
        "invocation_id": invocation_id,
        "fixer_identity": _nonempty(attempt.get("fixer_identity"), "fixer_identity"),
        "strategy_family": family,
        "strategy_summary": _nonempty(attempt.get("strategy_summary"), "strategy_summary"),
        "prior_attempt_history_sha256": current_hash,
        "status": "running",
        "result_path": "",
        "result_sha256": "",
        "rejection_evidence": [],
    })
    closure["status"] = "fix_running"
    return _save(path, ledger)


def finish_attempt(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "fix_running" or not closure["attempts"]:
        raise StateError("repair result may be recorded only for a running attempt")
    current = closure["attempts"][-1]
    if current["status"] != "running":
        raise StateError("latest repair attempt is not running")
    current["result_path"] = _nonempty(result.get("result_path"), "result_path")
    current["result_sha256"] = _nonempty(result.get("result_sha256"), "result_sha256")
    current["status"] = "awaiting_review"
    closure["status"] = "rereview_required"
    return _save(path, ledger)


def reconcile_interrupted_attempts(
    path: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Atomically reconcile every running member of one interrupted fixer call."""
    ledger = _load(path)
    raw_receipt_path = _nonempty(
        result.get("receipt_path"), "interrupted receipt_path"
    )
    receipt_path = Path(raw_receipt_path)
    if not receipt_path.is_absolute():
        raise StateError("interrupted receipt path must be absolute")
    receipt_path = receipt_path.resolve()
    try:
        receipt_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("interrupted receipt must be run-owned") from exc
    expected_sha256 = _nonempty(
        result.get("receipt_sha256"), "interrupted receipt_sha256"
    )
    if not receipt_path.is_file() or sha256_file(receipt_path) != expected_sha256:
        raise StateError("interrupted receipt hash mismatch")
    receipt = read_json(receipt_path)
    interruption = receipt.get("interruption")
    if (
        receipt.get("status") != "failed"
        or not isinstance(interruption, dict)
        or interruption.get("marker") is not True
        or interruption.get("termination_status") != "verified"
        or interruption.get("supervisor_reaped") is not True
    ):
        raise StateError("interrupted receipt lacks verified termination proof")
    invocation_id = _nonempty(receipt.get("receipt_id"), "receipt_id")
    requested_ids = result.get("closure_ids")
    if (
        not isinstance(requested_ids, list)
        or not requested_ids
        or any(not isinstance(item, str) or not item for item in requested_ids)
        or len(set(requested_ids)) != len(requested_ids)
    ):
        raise StateError("interrupted reconciliation requires unique closure_ids")
    running: list[dict[str, Any]] = []
    for closure in ledger["closures"]:
        attempts = closure.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            continue
        current = attempts[-1]
        if (
            closure.get("status") == "fix_running"
            and current.get("status") == "running"
            and current.get("invocation_id") == invocation_id
        ):
            running.append(closure)
    if [item["closure_id"] for item in running] != requested_ids:
        raise StateError(
            "interrupted receipt does not match the complete ordered repair batch"
        )
    if not running:
        raise StateError("interrupted receipt matches no running repair batch")
    for closure in running:
        current = closure["attempts"][-1]
        current.update(
            status="interrupted",
            interruption_receipt_path=str(receipt_path),
            interruption_receipt_sha256=expected_sha256,
        )
        closure["status"] = "ready_for_fix"
    return _save(path, ledger)


def _load_dependency_graph(ledger_path: Path, ledger: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    if ledger.get("dependency_graph_status") != "validated":
        raise StateError("active legacy closure requires a reviewed dependency graph")
    raw_path = ledger.get("dependency_graph_path")
    digest = ledger.get("dependency_graph_sha256")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or not isinstance(digest, str):
        raise StateError("closure ledger dependency graph binding is incomplete")
    graph_path = Path(raw_path).resolve()
    try:
        graph_path.relative_to(ledger_path.parent.resolve())
    except ValueError as exc:
        raise StateError("repair dependency graph must be run-owned") from exc
    if not graph_path.is_file() or sha256_file(graph_path) != digest:
        raise StateError("repair dependency graph hash mismatch")
    graph = read_json(graph_path)
    if (
        graph.get("protocol")
        not in {
            "implement-v13-codex/repair-dependency-graph/1",
            "implement-v13-codex/repair-dependency-graph/2",
        }
        or graph.get("feature_run_id") != ledger.get("feature_run_id")
    ):
        raise StateError("repair dependency graph subject mismatch")
    root = Path(str(graph.get("repository_root", ""))).resolve()
    if graph.get("repository_identity") != repository_identity(root):
        raise StateError("repair dependency graph repository identity mismatch")
    for node in graph.get("closures", []):
        for binding in node.get("source_bindings", []):
            source = _source_path(root, binding.get("path"), "dependency graph source binding")
            if sha256_file(source) != binding.get("sha256"):
                raise StateError("repair dependency graph source binding changed")
        for test in node.get("immutable_test_nodes", []):
            source = _source_path(root, test.get("source_path"), "dependency graph test source")
            if sha256_file(source) != test.get("source_sha256"):
                raise StateError("repair dependency graph immutable test changed")
    return graph_path, graph


def _affected_closure_ids(
    graph: dict[str, Any], changed_surfaces: set[str]
) -> list[str]:
    nodes = graph.get("closures", [])
    selected = {
        node["closure_id"]
        for node in nodes
        if changed_surfaces.intersection(
            set(node.get("write_surfaces", []) + node.get("read_surfaces", []))
        )
    }
    if not selected:
        raise StateError("changed surface is not bound to the repair dependency graph")
    edges = graph.get("edges", [])
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge["from_closure_id"] in selected and edge["to_closure_id"] not in selected:
                selected.add(edge["to_closure_id"])
                changed = True
    order = [node["closure_id"] for node in nodes]
    return [closure_id for closure_id in order if closure_id in selected]


def _selected_tests(
    graph: dict[str, Any], affected_closure_ids: list[str], changed_surfaces: set[str]
) -> tuple[list[str], list[Any]]:
    affected = set(affected_closure_ids)
    nodes: list[str] = []
    commands: list[Any] = []
    covered: set[str] = set()
    for closure in graph.get("closures", []):
        if closure.get("closure_id") not in affected:
            continue
        required_here = changed_surfaces.intersection(
            set(closure.get("write_surfaces", []) + closure.get("read_surfaces", []))
        )
        covered_here: set[str] = set()
        for test in closure.get("immutable_test_nodes", []):
            if changed_surfaces.intersection(test.get("covers_surfaces", [])) or (
                closure.get("closure_id") in affected
            ):
                if test["node_id"] not in nodes:
                    nodes.append(test["node_id"])
                    command = test["command"]
                    commands.append(
                        dict(command) if isinstance(command, dict) else list(command)
                    )
                covered.update(set(test.get("covers_surfaces", [])) & changed_surfaces)
                covered_here.update(
                    set(test.get("covers_surfaces", [])) & required_here
                )
        if required_here and covered_here != required_here:
            raise StateError(
                "repair dependency graph completeness guard found an affected closure "
                "without its dependent test"
            )
    if covered != changed_surfaces:
        raise StateError(
            "repair dependency graph completeness guard found a changed surface without a mapped test"
        )
    return nodes, commands


def select_repair_batch(
    path: Path,
    closure_ids: list[str],
    union_write_set: list[str],
    *,
    changed_surfaces: list[str] | None = None,
) -> dict[str, Any]:
    """Validate and persist one explicit connected repair batch (maximum three closures)."""
    ledger = _load(path)
    graph_path, graph = _load_dependency_graph(path, ledger)
    selected_ids = _strings(closure_ids, "repair batch closure_ids")
    if len(selected_ids) > 3:
        raise StateError("repair batch may contain at most three closures")
    selected = [_closure(ledger, closure_id) for closure_id in selected_ids]
    if any(item.get("status") != "ready_for_fix" for item in selected):
        raise StateError("repair batch closures must all be ready_for_fix")
    reviewers = [item["origin_reviewer"] for item in selected]
    if len(reviewers) != len(set(reviewers)):
        raise StateError("repair batch requires independent originating reviewers")
    fingerprints = set().union(*(set(item["fingerprints"]) for item in selected))
    excluded = set().union(*(set(item.get("excluded_fingerprints", [])) for item in selected))
    if fingerprints.intersection(excluded):
        raise StateError("repair batch overlaps an excluded fingerprint")
    declared_union = sorted(
        set().union(*(set(item.get("write_surfaces", [])) for item in selected))
    )
    requested_union = sorted(_strings(union_write_set, "repair batch union_write_set"))
    if declared_union != requested_union:
        raise StateError("repair batch path overlap falls outside its declared union write set")
    edges = {
        frozenset((edge["from_closure_id"], edge["to_closure_id"]))
        for edge in graph.get("edges", [])
    }
    if len(selected_ids) > 1:
        reached = {selected_ids[0]}
        while True:
            expanded = reached | {
                candidate
                for candidate in selected_ids
                if any(frozenset((candidate, prior)) in edges for prior in reached)
            }
            if expanded == reached:
                break
            reached = expanded
        if reached != set(selected_ids):
            raise StateError("multi-closure repair batch is disconnected")
    changed = set(
        _strings(changed_surfaces, "repair batch changed_surfaces")
        if changed_surfaces is not None
        else requested_union
    )
    if not changed.issubset(set(requested_union)):
        raise StateError("repair batch changed surfaces escape the union write set")
    component = _affected_closure_ids(graph, changed)
    tests, raw_commands = _selected_tests(graph, component, changed)
    manifest_paths = {item.get("capability_manifest_path") for item in selected}
    manifest_hashes = {item.get("capability_manifest_sha256") for item in selected}
    if (
        len(manifest_paths) != 1
        or len(manifest_hashes) != 1
        or not isinstance(next(iter(manifest_paths)), str)
        or not isinstance(next(iter(manifest_hashes)), str)
    ):
        raise StateError("repair batch closures do not share one capability manifest")
    manifest_path = Path(str(next(iter(manifest_paths)))).resolve()
    manifest = validate_capability_manifest(
        manifest_path,
        str(next(iter(manifest_hashes))),
        repository_root=Path(str(graph["repository_root"])),
        feature_run_id=ledger["feature_run_id"],
    )
    commands = [
        (
            validate_test_command(command, manifest)
            if isinstance(command, dict)
            else bind_test_command(command, manifest)
        )
        for command in raw_commands
    ]
    batch_id = hashlib.sha256(
        canonical_bytes({
            "graph": ledger["dependency_graph_sha256"],
            "closures": selected_ids,
            "union_write_set": requested_union,
            "changed_surfaces": sorted(changed),
        })
    ).hexdigest()[:24]
    batch = {
        "protocol": "implement-v13-codex/repair-batch/2",
        "feature_run_id": ledger["feature_run_id"],
        "batch_id": batch_id,
        "dependency_graph_path": str(graph_path),
        "dependency_graph_sha256": ledger["dependency_graph_sha256"],
        "closure_ids": selected_ids,
        "component_closure_ids": component,
        "union_write_set": requested_union,
        "changed_surfaces": sorted(changed),
        "independent_reviewers": reviewers,
        "selected_test_nodes": tests,
        "selected_commands": commands,
    }
    batch_path = path.parent / f"repair-batch-{batch_id}.v2.json"
    atomic_write_json(batch_path, batch)
    batch_sha256 = sha256_file(batch_path)
    for closure in selected:
        closure.setdefault("batch_history", []).append({
            "batch_id": batch_id,
            "batch_path": str(batch_path),
            "batch_sha256": batch_sha256,
        })
    ledger.setdefault("scheduling_history", []).append({
        "event": "repair_batch_selected",
        "batch_id": batch_id,
        "closure_ids": selected_ids,
        "affected_component_size": len(component),
        "regression_tests_selected": len(tests),
    })
    _save(path, ledger)
    return {
        **batch,
        "batch_path": str(batch_path),
        "batch_sha256": batch_sha256,
    }


def _validated_gate_receipt(
    path: Path,
    ledger: dict[str, Any],
    closure_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    raw_path = result.get("gate_receipt_path")
    digest = result.get("gate_receipt_sha256")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or not isinstance(digest, str):
        raise StateError("graph-aware targeted review requires a deterministic gate receipt")
    receipt_path = Path(raw_path).resolve()
    try:
        receipt_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("repair gate receipt must be run-owned") from exc
    if not receipt_path.is_file() or sha256_file(receipt_path) != digest:
        raise StateError("repair gate receipt hash mismatch")
    receipt = read_json(receipt_path)
    if (
        receipt.get("protocol") != "implement-v13-codex/repair-gates/1"
        or receipt.get("status") != "passed"
        or receipt.get("feature_run_id") != ledger.get("feature_run_id")
        or closure_id not in receipt.get("batch_closure_ids", [])
        or receipt.get("dependency_graph_sha256") != ledger.get("dependency_graph_sha256")
    ):
        raise StateError("repair gate receipt does not authorize targeted review")
    expected_order = [
        "forbidden_access",
        "pre_communication_output_bound",
        "process_evidence",
        "capability_manifest",
        "production_certification",
    ]
    if [item.get("gate_class") for item in receipt.get("gates", [])] != expected_order:
        raise StateError("repair gate receipt is incomplete or out of order")
    if any(item.get("status") != "passed" for item in receipt["gates"]):
        raise StateError("targeted review cannot run after a failed deterministic gate")
    return receipt


def record_gate_failure(
    path: Path,
    closure_id: str,
    *,
    gate_receipt_path: Path,
) -> dict[str, Any]:
    """Reject one completed fix from deterministic evidence without model review."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure.get("status") != "rereview_required" or not closure.get("attempts"):
        raise StateError("repair gate failure requires a completed fix awaiting review")
    receipt_path = gate_receipt_path.resolve()
    try:
        receipt_path.relative_to(path.parent.resolve())
    except ValueError as exc:
        raise StateError("repair gate failure receipt must be run-owned") from exc
    receipt = read_json(receipt_path)
    if (
        receipt.get("protocol") != "implement-v13-codex/repair-gates/1"
        or receipt.get("status") != "failed"
        or closure_id not in receipt.get("batch_closure_ids", [])
        or receipt.get("feature_run_id") != ledger.get("feature_run_id")
    ):
        raise StateError("repair gate failure receipt subject mismatch")
    current = closure["attempts"][-1]
    if current.get("status") != "awaiting_review":
        raise StateError("repair gate failure does not match the active fixer attempt")
    current["status"] = "rejected"
    current["gate_receipt_path"] = str(receipt_path)
    current["gate_receipt_sha256"] = sha256_file(receipt_path)
    current["rejection_evidence"] = [
        f"{receipt.get('failure_class', 'repair_gate_failed')}: "
        f"{next((gate.get('error') for gate in receipt.get('gates', []) if gate.get('status') == 'failed'), '')}"
    ]
    closure.setdefault("regression_evidence", []).append({
        "gate_receipt_path": str(receipt_path),
        "gate_receipt_sha256": sha256_file(receipt_path),
        "failure_class": receipt.get("failure_class"),
        "affected_closure_ids": receipt.get("affected_closure_ids", []),
        "selected_test_nodes": receipt.get("selected_test_nodes", []),
    })
    closure["status"] = (
        "escalation_required"
        if _post_resolution_attempt_rejections(closure)
        >= int(ledger["attempts_before_escalation"])
        else "ready_for_fix"
    )
    return _save(path, ledger)


def record_review(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "rereview_required" or not closure["attempts"]:
        raise StateError("targeted review may be recorded only when rereview_required")
    reviewer_role = _nonempty(result.get("reviewer_role"), "reviewer_role")
    reviewer_receipt = _nonempty(result.get("reviewer_receipt_id"), "reviewer_receipt_id")
    current = closure["attempts"][-1]
    if reviewer_role != closure["origin_reviewer"]:
        raise StateError("targeted closure review must use the originating reviewer role")
    if reviewer_receipt in {current["invocation_id"], closure["closure_test"]["author_receipt_id"]}:
        raise StateError("targeted reviewer must be independent of test author and fixer")
    statuses = result.get("finding_statuses")
    if not isinstance(statuses, dict) or set(statuses) != set(closure["fingerprints"]):
        raise StateError("targeted review must disposition every assigned fingerprint exactly once")
    if any(value not in {"fixed", "not_fixed", "regression"} for value in statuses.values()):
        raise StateError("invalid targeted finding status")
    graph_aware = ledger.get("dependency_graph_status") == "validated"
    gate_receipt = (
        _validated_gate_receipt(path, ledger, closure_id, result)
        if graph_aware
        else None
    )
    closed_others = [
        item for item in ledger["closures"]
        if item["closure_id"] != closure_id and item["status"] == "closed"
    ]
    checks = result.get("regression_checks", {})
    if not isinstance(checks, dict):
        raise StateError("targeted review regression_checks must be an object")
    if not graph_aware and set(checks) != {item["closure_id"] for item in closed_others}:
        raise StateError("targeted review must rerun every previously closed closure test")
    if graph_aware and checks:
        affected = set(gate_receipt.get("affected_closure_ids", []))
        if not set(checks).issubset(affected):
            raise StateError("targeted reviewer supplied an unrelated closed-peer Boolean")
    evidence = _strings(result.get("evidence"), "evidence")
    current["reviewer_receipt_id"] = reviewer_receipt
    current["finding_statuses"] = statuses
    current["regression_checks"] = checks
    current["gate_receipt_path"] = result.get("gate_receipt_path", "")
    current["gate_receipt_sha256"] = result.get("gate_receipt_sha256", "")
    closure.setdefault("regression_evidence", []).append({
        "gate_receipt_path": result.get("gate_receipt_path", ""),
        "gate_receipt_sha256": result.get("gate_receipt_sha256", ""),
        "affected_closure_ids": gate_receipt.get("affected_closure_ids", []) if gate_receipt else [],
        "selected_test_nodes": gate_receipt.get("selected_test_nodes", []) if gate_receipt else [],
    })
    regressed = [
        item for item in closed_others
        if item["closure_id"] in checks and checks[item["closure_id"]] is not True
    ]
    for item in regressed:
        item["status"] = "ready_for_fix"
    if all(value == "fixed" for value in statuses.values()):
        current["status"] = "accepted"
        closure["status"] = "closed"
    else:
        current["status"] = "rejected"
        current["rejection_evidence"] = evidence
        closure["status"] = (
            "escalation_required"
            if _post_resolution_attempt_rejections(closure) >= int(ledger["attempts_before_escalation"])
            else "ready_for_fix"
        )
    return _save(path, ledger)


def record_escalation(path: Path, closure_id: str, result: dict[str, Any]) -> dict[str, Any]:
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    if closure["status"] != "escalation_required":
        raise StateError("escalation may be recorded only when escalation_required")
    action = result.get("action")
    if action not in ESCALATION_ACTIONS:
        raise StateError("escalation action must be reassign, decompose, or operator")
    entry = {
        "action": action,
        "reason": _nonempty(result.get("reason"), "reason"),
        "evidence": _strings(result.get("evidence"), "evidence"),
    }
    if action == "reassign":
        entry["new_fixer_identity"] = _nonempty(result.get("new_fixer_identity"), "new_fixer_identity")
        if entry["new_fixer_identity"] == closure["attempts"][-1]["fixer_identity"]:
            raise StateError("reassignment must select a different fixer identity")
        closure["status"] = "ready_for_fix"
    elif action == "decompose":
        entry["decomposition"] = _strings(result.get("decomposition"), "decomposition")
        closure["status"] = "test_required"
    else:
        closure["status"] = "blocked"
    closure["escalation_history"].append(entry)
    return _save(path, ledger)


def next_action(path: Path) -> dict[str, Any]:
    ledger = _load(path)
    active = ledger.get("active_closure_id")
    if not active:
        return {"status": "complete", "closure_id": "", "attempt_history_sha256": ""}
    closure = _closure(ledger, active)
    return {
        "status": closure["status"],
        "closure_id": active,
        "complexity": closure["complexity"],
        "origin_reviewer": closure["origin_reviewer"],
        "depends_on": closure.get("depends_on", []),
        "related_closures": closure.get("related_closures", []),
        "excluded_fingerprints": closure.get("excluded_fingerprints", []),
        "write_surfaces": closure.get("write_surfaces", []),
        "read_surfaces": closure.get("read_surfaces", []),
        "immutable_test_nodes": closure.get("immutable_test_nodes", []),
        "dependency_graph_status": ledger.get("dependency_graph_status", "legacy_unbound"),
        "dependency_graph_path": ledger.get("dependency_graph_path"),
        "dependency_graph_sha256": ledger.get("dependency_graph_sha256"),
        "ready_age": closure.get("ready_age", 0),
        "attempt_history_sha256": attempt_history_sha256(closure),
    }


def validate_pre_model_closure(
    path: Path,
    closure_id: str,
    *,
    candidate_effect_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate immutable evidence before every designer or fixer call."""
    ledger = _load(path)
    closure = _closure(ledger, closure_id)
    closure_test = closure.get("closure_test")
    if not isinstance(closure_test, dict):
        raise StateError("repair model gate requires recorded closure-test evidence")
    assertion_path = Path(str(closure_test.get("assertion_map_path", "")))
    if (
        not assertion_path.is_absolute()
        or not assertion_path.is_file()
        or sha256_file(assertion_path) != closure_test.get("assertion_map_sha256")
    ):
        raise StateError("repair assertion map changed after closure-test recording")
    assertion_map = read_json(assertion_path)
    authoritative = _authoritative_effect_contract(closure)
    validate_assertion_effects(
        assertion_map,
        feature_run_id=ledger["feature_run_id"],
        closure_id=closure_id,
        effect_contract=authoritative,
        test_paths=closure_test["test_paths"],
        commands=closure_test["commands"],
    )
    satisfiability = solve_effect_constraints(
        assertion_map,
        effect_contract=candidate_effect_contract or authoritative,
    )
    capability_path = Path(str(closure_test.get("capability_manifest_path", "")))
    capability_sha256 = closure_test.get("capability_manifest_sha256")
    if not isinstance(capability_sha256, str):
        raise StateError("repair closure lacks capability-manifest evidence")
    validate_capability_manifest(
        capability_path,
        capability_sha256,
        repository_root=Path(assertion_map["repository_root"]),
        feature_run_id=ledger["feature_run_id"],
    )
    return {
        "protocol": "implement-v13-codex/repair-pre-model-gates/1",
        "status": (
            "ready"
            if satisfiability["model_calls_permitted"]
            else "deterministic_blocked"
        ),
        "model_calls_permitted": satisfiability["model_calls_permitted"],
        "assertion_map_sha256": closure_test["assertion_map_sha256"],
        "capability_manifest_sha256": capability_sha256,
        "satisfiability": satisfiability,
        "model_policy": {
            "designer": {"model": "gpt-5.6-terra", "reasoning": "medium"},
            "design_reviewer": {"model": "gpt-5.6-sol", "reasoning": "medium"},
            "quality_advantage": "not_established",
            "benchmark_is_release_gate": False,
        },
    }


def validate_invocation_spec(spec: dict[str, Any], artifact_dir: Path) -> None:
    action = spec.get("closure_action")
    repair_role = spec.get("role") in {"code_fixer", "repair_designer", "targeted_reviewer"}
    if action is None:
        if repair_role:
            raise StateError("repair invocation is missing closure_action")
        return
    if action not in REPAIR_ACTIONS:
        raise StateError("unknown closure_action")
    raw_path = spec.get("closure_ledger_path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise StateError("repair invocation requires an absolute closure_ledger_path")
    ledger_path = Path(raw_path).resolve()
    try:
        ledger_path.relative_to(artifact_dir)
    except ValueError:
        raise StateError("closure ledger must be beneath the run artifact directory") from None
    ledger = _load(ledger_path)
    closure = _closure(ledger, _nonempty(spec.get("closure_id"), "closure_id"))
    if ledger.get("active_closure_id") != closure["closure_id"]:
        raise StateError("repair invocation does not target the active closure group")
    expected_status = {
        "author_test": "test_required",
        "design": "design_required",
        "design_review": "design_review_required",
        "fix": "fix_running",
        "targeted_review": "rereview_required",
    }[action]
    if closure["status"] != expected_status:
        raise StateError(f"closure action {action} is not permitted while {closure['status']}")
    capability_path_raw = spec.get("capability_manifest_path")
    capability_sha256 = spec.get("capability_manifest_sha256")
    cwd_raw = spec.get("cwd")
    if (
        not isinstance(capability_path_raw, str)
        or not Path(capability_path_raw).is_absolute()
        or not isinstance(capability_sha256, str)
        or not isinstance(cwd_raw, str)
        or not Path(cwd_raw).is_absolute()
    ):
        raise StateError("repair invocation requires capability manifest and absolute cwd")
    capability_path = Path(capability_path_raw).resolve()
    try:
        capability_path.relative_to(artifact_dir)
    except ValueError as exc:
        raise StateError("repair invocation capability manifest must be run-owned") from exc
    validate_capability_manifest(
        capability_path,
        capability_sha256,
        repository_root=Path(cwd_raw),
        feature_run_id=ledger["feature_run_id"],
        controller_package_digest=spec.get("controller_package_digest"),
    )
    if action in {"author_test", "design_review", "targeted_review"}:
        # Invocation specs request scratch; they never select its path.  run_exec
        # derives the private path from the receipt identity, verifies that it
        # cannot overlap repository write authority, creates it immediately
        # before launch, and hashes/removes it before the receipt is terminal.
        # Keeping this field boolean is a single contract shared by closure
        # validation and the execution boundary.
        if spec.get("ephemeral_scratch") is not True:
            raise StateError(
                "reviewer invocation requires ephemeral_scratch=true; "
                "caller-selected scratch paths are forbidden"
            )
        if not isinstance(spec.get("receipt_id"), str) or not spec["receipt_id"]:
            raise StateError("reviewer ephemeral_scratch requires receipt_id")
    if action == "author_test":
        raw_write_paths = spec.get("allowed_write_paths")
        if not isinstance(raw_write_paths, list) or not 1 <= len(raw_write_paths) <= 4:
            raise StateError("author_test requires one to four allowed_write_paths")
        normalized: set[str] = set()
        cwd = Path(cwd_raw).resolve()
        for raw_write_path in raw_write_paths:
            if (
                not isinstance(raw_write_path, str)
                or not raw_write_path
                or Path(raw_write_path).is_absolute()
            ):
                raise StateError("author_test allowed_write_paths must be nonempty relative paths")
            target = (cwd / raw_write_path).resolve()
            try:
                relative = target.relative_to(cwd).as_posix()
            except ValueError:
                raise StateError("author_test allowed_write_paths escape the feature worktree") from None
            if relative != Path(raw_write_path).as_posix() or relative in {"", "."}:
                raise StateError("author_test allowed_write_paths must be normalized file paths")
            normalized.add(relative)
        if len(normalized) != len(raw_write_paths):
            raise StateError("author_test allowed_write_paths contain duplicates")
    if action in {"design", "fix"}:
        gate = validate_pre_model_closure(ledger_path, closure["closure_id"])
        if gate["model_calls_permitted"] is not True:
            raise StateError("repair effect constraints are contradictory before model invocation")
    model, reasoning = spec.get("model"), spec.get("reasoning")
    if action == "design":
        if (spec.get("role"), model, reasoning) != ("repair_designer", "gpt-5.6-terra", "medium"):
            raise StateError("repair design requires the Terra-medium design identity")
    elif action == "fix":
        if (spec.get("role"), model, reasoning) != ("code_fixer", "gpt-5.6-terra", "medium"):
            raise StateError("repair fixes require the Terra-medium implementation identity")
    else:
        if spec.get("role") != closure["origin_reviewer"] or model != "gpt-5.6-sol" or reasoning != "medium":
            raise StateError("adversarial closure roles require the originating Sol-medium reviewer")
    if action == "design":
        canonical = Path(__file__).resolve().parents[1] / "schemas" / "repair-design-result.schema.json"
        raw_schema_path = spec.get("schema_path")
        if not isinstance(raw_schema_path, str):
            raise StateError("repair designer must use canonical repair-design-result.schema.json")
        try:
            supplied = Path(raw_schema_path).resolve()
            if supplied != canonical.resolve():
                raise StateError("repair designer must use canonical repair-design-result.schema.json")
            source = json.loads(canonical.read_text(encoding="utf-8"))
            hashes = canonical_schema_hashes(source, compile_transport_schema(source))
            for field in ("schema_source_sha256", "schema_transport_sha256"):
                if field in spec and spec[field] != hashes[field]:
                    raise StateError(f"repair designer {field} mismatches canonical compilation")
        except OSError as exc:
            raise StateError(f"repair design schema is unreadable: {type(exc).__name__}") from exc
        if spec.get("sandbox", "read-only") != "read-only" or spec.get("writable_roots", []):
            raise StateError("repair designer must be read-only; its schema-bound result is the design artifact")
        expected = spec.get("expected")
        required_expected = {
            "protocol": "implement-v13-codex/repair-design/1",
            "queue_run_id": spec.get("queue_run_id"),
            "feature_run_id": spec.get("feature_run_id"),
            "phase": "REVIEWING",
            "phase_detail": "repair_design",
            "role": "repair_designer",
        }
        if expected != required_expected or not all(required_expected.values()):
            raise StateError("repair designer expected identity must match the canonical repair-design contract")
    if action == "fix":
        current = closure["attempts"][-1]
        if spec.get("receipt_id") != current["invocation_id"]:
            raise StateError("fix invocation does not match the ledger's running attempt")
        if spec.get("closure_strategy_family") != current["strategy_family"]:
            raise StateError("fix invocation strategy does not match the closure ledger")
        if spec.get("closure_attempt_history_sha256") != current["prior_attempt_history_sha256"]:
            raise StateError("fix invocation omits or mismatches prior-attempt history")


def _result(path: str) -> dict[str, Any]:
    value = read_json(Path(path).resolve())
    if not isinstance(value, dict):
        raise StateError("result must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("ledger"); init.add_argument("feature_run_id"); init.add_argument("groups")
    for name in ("record-test", "backfill-assertion-map", "resolve-legacy-assertion-conflict", "record-design", "record-design-review", "backfill-design-rejections", "resolve-design-contradiction", "activate-post-resolution-design-budget", "activate-post-resolution-attempt-budget", "start-attempt", "finish-attempt", "record-review", "record-escalation"):
        command = sub.add_parser(name); command.add_argument("ledger"); command.add_argument("closure_id"); command.add_argument("result")
    status = sub.add_parser("next"); status.add_argument("ledger")
    args = parser.parse_args(argv)
    ledger_path = Path(args.ledger).resolve()
    if args.command == "init":
        groups = read_json(Path(args.groups).resolve())
        output = create_ledger(ledger_path, feature_run_id=args.feature_run_id, groups=groups)
    elif args.command == "next":
        output = next_action(ledger_path)
    else:
        function = {
            "record-test": record_test, "record-design": record_design,
            "backfill-assertion-map": backfill_assertion_map,
            "resolve-legacy-assertion-conflict": resolve_legacy_assertion_conflict,
            "record-design-review": record_design_review, "start-attempt": start_attempt,
            "backfill-design-rejections": backfill_design_rejections,
            "resolve-design-contradiction": resolve_design_contradiction,
            "activate-post-resolution-design-budget": lambda path, closure_id, _result: activate_post_resolution_design_budget(path, closure_id),
            "activate-post-resolution-attempt-budget": lambda path, closure_id, _result: activate_post_resolution_attempt_budget(path, closure_id),
            "finish-attempt": finish_attempt, "record-review": record_review,
            "record-escalation": record_escalation,
        }[args.command]
        output = function(ledger_path, args.closure_id, _result(args.result))
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
