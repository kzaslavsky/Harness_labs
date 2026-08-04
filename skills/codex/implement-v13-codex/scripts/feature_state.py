#!/usr/bin/env python3
"""Deterministic feature state, input, reconciliation, and terminal transactions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from state_io import StateError, atomic_write_json, cas_update, read_json, sha256_file


PHASE_DETAILS: dict[str, tuple[str, ...]] = {
    "PLANNING": ("planner_prepare", "planner_run", "plan_validate", "plan_render"),
    "PLAN_REVIEW": ("review_dispatch", "review_collect", "revise", "revised_plan_validate"),
    "IMPLEMENTING": ("strategy_validate", "workers_dispatch", "workers_collect", "integration_validate"),
    "RUNTIME_SMOKE": ("smoke_a_run", "smoke_a_fix", "smoke_a_rerun"),
    "REVIEWING": ("review_dispatch", "ui_walk_plan", "score", "fix", "rereview", "review_finalize"),
    "COMMITTING": (
        "smoke_b_run",
        "smoke_b_fix",
        "ui_walk_run",
        "full_venv_run",
        "full_venv_fix",
        "final_gates",
        "feature_commit",
        "manifest_commit",
        "merge_prepare",
        "merge",
        "cleanup",
    ),
}
PHASE_ORDER = tuple(PHASE_DETAILS)
TRANSACTION_STATES = (
    "prepared",
    "feature_committed",
    "manifest_committed",
    "merge_prepared",
    "merged",
    "cleanup_complete",
    "feature_result_written",
    "dispatcher_ack",
)
REQUIRED_COMPLETION_UPDATES = {"agents", "next_steps", "development_index"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def initialize_checkpoint(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Create the one authoritative feature checkpoint."""
    required = {"task", "base_branch", "worktree_name", "branch", "feature_index", "queue_run_id", "feature_run_id"}
    missing = sorted(required - payload.keys())
    if missing:
        raise StateError(f"checkpoint missing fields: {', '.join(missing)}")
    if path.exists():
        current = read_json(path)
        for guard in ("task", "base_branch", "queue_run_id", "feature_run_id"):
            if current.get(guard) != payload.get(guard):
                raise StateError(f"active checkpoint mismatch: {guard}")
        return current
    checkpoint = dict(payload)
    checkpoint.update(
        protocol_version=(
            "1.1" if payload.get("controller_package_digest") else "1.0"
        ),
        runner="implement-v13-codex",
        engine="v13-codex",
        phase="PLANNING",
        phase_detail="planner_prepare",
        phase_state="ready",
        state_revision=0,
        started=_now(),
        updated_at=_now(),
        completed_phase_details=[],
        detail_receipts={},
        certification_cycle=0,
        invalidation_history=[],
        blocked_history=[],
        artifacts={},
    )
    atomic_write_json(path, checkpoint)
    return checkpoint


def _catalog_position(phase: str, detail: str) -> int:
    flattened = [(coarse, item) for coarse, items in PHASE_DETAILS.items() for item in items]
    try:
        return flattened.index((phase, detail))
    except ValueError as exc:
        raise StateError(f"unknown phase detail: {phase}/{detail}") from exc


def transition(
    path: Path,
    expected_revision: int,
    phase: str,
    detail: str,
    state: str,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Advance exactly one phase-detail edge with compare-and-swap."""
    if state not in {"ready", "running", "validating", "blocked", "complete"}:
        raise StateError(f"invalid phase state: {state}")
    current = read_json(path)
    current_position = _catalog_position(str(current["phase"]), str(current["phase_detail"]))
    target_position = _catalog_position(phase, detail)
    if target_position not in {current_position, current_position + 1}:
        raise StateError("phase transition must remain in place or advance one detail")
    current_state = str(current.get("phase_state"))
    if target_position == current_position + 1:
        if current_state != "complete" or state != "ready":
            raise StateError("advancing requires a completed detail and a ready successor")
    else:
        allowed = {
            "ready": {"running", "blocked"},
            "running": {"validating", "blocked"},
            "validating": {"complete", "blocked"},
            "blocked": {"blocked"},
            "complete": {"complete"},
        }
        if state not in allowed.get(current_state, set()):
            raise StateError(f"invalid phase-state edge: {current_state} -> {state}")
    if phase == "PLAN_REVIEW" and detail == "review_dispatch" and state == "blocked":
        if receipt_path is None or not receipt_path.is_file():
            raise StateError(
                "review findings must advance through review_collect, revise, and "
                "revised_plan_validate before blocking"
            )
        failed_receipt = read_json(receipt_path)
        if failed_receipt.get("status") not in {"failed", "orphaned"}:
            raise StateError(
                "review_dispatch may block only on a terminal failed reviewer receipt"
            )
        if failed_receipt.get("phase") not in {None, phase} or failed_receipt.get(
            "phase_detail"
        ) not in {None, detail}:
            raise StateError("failed reviewer receipt phase identity mismatch")
    completed = list(current.get("completed_phase_details", []))
    detail_receipts = dict(current.get("detail_receipts", {}))
    if state == "complete":
        if receipt_path is None or not receipt_path.is_file():
            raise StateError("completing a detail requires a terminal receipt")
        receipt = read_json(receipt_path)
        if receipt.get("status") not in {"succeeded", "passed"}:
            raise StateError("detail receipt is not terminal-success")
        if receipt.get("phase") not in {None, phase} or receipt.get("phase_detail") not in {None, detail}:
            raise StateError("detail receipt phase identity mismatch")
        detail_receipts[f"{phase}/{detail}"] = {
            "path": str(receipt_path.resolve()),
            "sha256": sha256_file(receipt_path),
        }
    if target_position == current_position + 1:
        token = f'{current["phase"]}/{current["phase_detail"]}'
        if token not in completed:
            completed.append(token)
    return cas_update(
        path,
        expected_revision,
        {
            "phase": phase,
            "phase_detail": detail,
            "phase_state": state,
            "completed_phase_details": completed,
            "detail_receipts": detail_receipts,
            "updated_at": _now(),
        },
    )


def resume_blocked_checkpoint(
    path: Path,
    expected_revision: int,
    resume_authorization: dict[str, Any],
) -> dict[str, Any]:
    """Reopen the same detail only after the serial dispatcher authorized resume."""
    current = read_json(path)
    if current.get("phase_state") != "blocked":
        raise StateError("only a blocked checkpoint can be resumed")
    if not isinstance(resume_authorization, dict) or not resume_authorization:
        raise StateError("blocked checkpoint resume requires dispatcher authorization")
    authorization_sha256 = resume_authorization.get("authorization_sha256")
    resolution_evidence = resume_authorization.get("resolution_evidence")
    authorized_at = resume_authorization.get("authorized_at")
    if not isinstance(authorization_sha256, str) or len(authorization_sha256) != 64:
        raise StateError("blocked checkpoint resume authorization digest is invalid")
    if not isinstance(resolution_evidence, dict) or not resolution_evidence:
        raise StateError("blocked checkpoint resume resolution evidence is missing")
    if not isinstance(authorized_at, str) or not authorized_at:
        raise StateError("blocked checkpoint resume authorization timestamp is missing")
    history = list(current.get("blocked_history", []))
    history.append({
        "phase": current.get("phase"),
        "phase_detail": current.get("phase_detail"),
        "blocked_revision": current.get("state_revision"),
        "active_blocker": current.get("active_blocker"),
        "authorization_sha256": authorization_sha256,
        "authorized_at": authorized_at,
    })
    evidence = list(current.get("resolution_evidence", []))
    evidence.append({
        "authorization_sha256": authorization_sha256,
        "authorized_at": authorized_at,
        "evidence": resolution_evidence,
    })
    return cas_update(
        path,
        expected_revision,
        {
            "phase_state": "ready",
            "active_blocker": None,
            "blocked_history": history,
            "resolution_evidence": evidence,
            "updated_at": _now(),
        },
    )


def block_checkpoint(
    path: Path,
    expected_revision: int,
    blocker: dict[str, Any],
) -> dict[str, Any]:
    """Atomically persist a coordinator-declared blocker at the current detail."""
    current = read_json(path)
    if current.get("phase_state") not in {"ready", "running", "validating"}:
        raise StateError("coordinator may block only an active checkpoint detail")
    required = ("blocker_class", "reason", "resume_condition")
    for field in required:
        if not isinstance(blocker.get(field), str) or not blocker[field]:
            raise StateError(f"coordinator blocker {field} is missing")
    recorded = {
        "phase": current.get("phase"),
        "phase_detail": current.get("phase_detail"),
        "blocker_class": blocker["blocker_class"],
        "reason": blocker["reason"],
        "resume_condition": blocker["resume_condition"],
        "resolution_evidence": [],
        "at": _now(),
    }
    history = list(current.get("blocked_history", []))
    history.append(recorded)
    return cas_update(
        path,
        expected_revision,
        {
            "phase_state": "blocked",
            "active_blocker": recorded,
            "blocked_history": history,
            "updated_at": recorded["at"],
        },
    )


def invalidate_certification(
    path: Path,
    expected_revision: int,
    evidence_path: Path,
) -> dict[str, Any]:
    """Rewind post-review certification after a fixer changed code."""
    current = read_json(path)
    origin = (str(current.get("phase")), str(current.get("phase_detail")))
    allowed_origins = {
        ("COMMITTING", "smoke_b_fix"),
        ("COMMITTING", "full_venv_fix"),
    }
    if origin not in allowed_origins or current.get("phase_state") != "complete":
        raise StateError("certification invalidation is not allowed from this checkpoint")
    if not evidence_path.is_file():
        raise StateError("certification invalidation requires fixer evidence")
    evidence = read_json(evidence_path)
    if evidence.get("status") not in {"succeeded", "passed"}:
        raise StateError("fixer evidence is not terminal-success")
    history = list(current.get("invalidation_history", []))
    cycle = int(current.get("certification_cycle", 0)) + 1
    history.append(
        {
            "cycle": cycle,
            "from_phase": origin[0],
            "from_phase_detail": origin[1],
            "reason": "post_review_code_edit",
            "evidence_path": str(evidence_path.resolve()),
            "evidence_sha256": sha256_file(evidence_path),
            "at": _now(),
        }
    )
    return cas_update(
        path,
        expected_revision,
        {
            "phase": "REVIEWING",
            "phase_detail": "review_dispatch",
            "phase_state": "ready",
            "certification_cycle": cycle,
            "invalidation_history": history,
            "updated_at": _now(),
        },
    )


def _baseline_inputs(worktree: Path) -> list[dict[str, Any]]:
    candidates = (
        ("agents", "AGENTS.md", "governing"),
        ("next_steps", "docs/development/NEXT_STEPS.md", "seed_plan"),
        ("development_index", "docs/development/INDEX.md", "background"),
    )
    result: list[dict[str, Any]] = []
    for input_id, relative, role in candidates:
        result.append(
            {
                "id": input_id,
                "path": relative,
                "role": role,
                "required": True,
                "revision": "latest_on_base",
                "update_policy": "reconcile_if_affected",
            }
        )
    return result


def _relevant_module_inputs(worktree: Path, paths_path: Path | None) -> list[dict[str, Any]]:
    if paths_path is None:
        return []
    value = json.loads(paths_path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("paths") or value.get("changed_paths")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateError("relevant paths must be a string array")
    modules: set[str] = set()
    for raw in value:
        parts = Path(raw).parts
        if len(parts) >= 2 and parts[0] == "retinology":
            modules.add(parts[1])
    result: list[dict[str, Any]] = []
    for module in sorted(modules):
        relative = f"retinology/{module}/context.md"
        if (worktree / relative).is_file():
            result.append(
                {
                    "id": f"module_context_{module}",
                    "path": relative,
                    "role": "governing",
                    "required": True,
                    "revision": "latest_on_base",
                    "update_policy": "reconcile_if_affected",
                }
            )
    return result


def build_inputs(
    worktree: Path,
    artifact_dir: Path,
    declared_path: Path | None,
    output_path: Path,
    relevant_paths_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve and hash the exact allowlisted planner input bundle."""
    worktree = worktree.resolve()
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    declared: list[dict[str, Any]] = []
    if declared_path:
        raw = json.loads(declared_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise StateError("declared planning inputs must be a JSON array of objects")
        declared = raw
    baseline = _baseline_inputs(worktree) + _relevant_module_inputs(worktree, relevant_paths_path)
    merged: dict[str, dict[str, Any]] = {item["id"]: item for item in baseline}
    for item in declared:
        input_id = item.get("id")
        if (
            not isinstance(input_id, str)
            or not input_id
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", input_id) is None
        ):
            raise StateError("each planning input requires a path-safe stable id")
        merged[input_id] = dict(item)
    resolved: list[dict[str, Any]] = []
    snapshot_dir = artifact_dir / "planning-input-snapshots"
    for item in merged.values():
        role = item.get("role")
        if role not in {"governing", "background", "acceptance", "seed_plan"}:
            raise StateError(f"invalid planning input role: {role}")
        revision = item.get("revision", "latest_on_base")
        source = Path(str(item.get("path", "")))
        if not source.is_absolute():
            source = (worktree / source).resolve()
        if revision == "snapshot":
            if not item.get("allow_external_snapshot", False):
                raise StateError(f"external snapshot requires explicit authorization: {item['id']}")
            if not source.is_file():
                raise StateError(f"snapshot input missing: {source}")
            snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = snapshot_dir / f"{item['id']}{source.suffix}"
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            resolved_path = destination
        else:
            if not _inside(source, worktree):
                raise StateError(f"non-snapshot planning input escapes worktree: {source}")
            resolved_path = source
        if not resolved_path.is_file():
            if item.get("required", True):
                raise StateError(f"required planning input missing: {resolved_path}")
            continue
        digest = sha256_file(resolved_path)
        expected = item.get("sha256")
        if revision == "exact_sha256" and expected != digest:
            raise StateError(f"planning input hash mismatch: {item['id']}")
        resolved.append(
            {
                **item,
                "resolved_path": str(resolved_path),
                "sha256": digest,
            }
        )
    manifest = {
        "protocol": "implement-v13-codex/planning-inputs/1",
        "worktree": str(worktree),
        "created_at": _now(),
        "inputs": resolved,
    }
    atomic_write_json(output_path, manifest)
    return manifest


def validate_reconciliation(
    input_manifest: Path,
    reconciliation_path: Path,
    worktree: Path | None = None,
    changed_paths_path: Path | None = None,
) -> dict[str, Any]:
    """Require an evidence-backed disposition for every mutable planning input."""
    manifest = read_json(input_manifest)
    reconciliation = read_json(reconciliation_path)
    if reconciliation.get("protocol") != "implement-v13-codex/context-reconciliation/1":
        raise StateError("invalid context reconciliation protocol")
    rows = reconciliation.get("entries")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise StateError("context reconciliation entries must be an object array")
    by_id = {row.get("input_id"): row for row in rows}
    manifest_by_id = {item.get("id"): item for item in manifest.get("inputs", [])}
    valid = {"updated", "verified_current", "superseded", "not_affected"}
    for item in manifest.get("inputs", []):
        policy = item.get("update_policy", "verify_only")
        needs_row = policy == "reconcile_if_affected" or item.get("id") == "next_steps"
        if not needs_row:
            continue
        row = by_id.get(item.get("id"))
        if not row:
            raise StateError(f"missing context reconciliation: {item.get('id')}")
        if row.get("disposition") not in valid:
            raise StateError(f"invalid context disposition: {item.get('id')}")
        if row.get("path") != item.get("path"):
            raise StateError(f"context disposition path mismatch: {item.get('id')}")
        if row.get("input_sha256") != item.get("sha256"):
            raise StateError(f"context disposition input hash mismatch: {item.get('id')}")
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise StateError(f"context disposition lacks evidence: {item.get('id')}")
        if item.get("id") in REQUIRED_COMPLETION_UPDATES:
            if row.get("disposition") != "updated":
                raise StateError(f"baseline context must be updated: {item.get('id')}")
            if worktree is None:
                raise StateError("baseline context update validation requires a worktree")
            output_sha256 = row.get("output_sha256")
            current_path = (worktree.resolve() / str(item.get("path"))).resolve()
            if (
                not _inside(current_path, worktree.resolve())
                or not current_path.is_file()
                or output_sha256 == item.get("sha256")
                or output_sha256 != sha256_file(current_path)
            ):
                raise StateError(f"baseline context update proof mismatch: {item.get('id')}")
    if changed_paths_path is not None:
        if worktree is None:
            raise StateError("changed-path reconciliation requires a worktree")
        for context in _relevant_module_inputs(worktree.resolve(), changed_paths_path):
            input_id = context["id"]
            if input_id not in manifest_by_id:
                raise StateError(f"touched module context was not a planning input: {input_id}")
            if input_id not in by_id:
                raise StateError(f"touched module context lacks reconciliation: {input_id}")
    return reconciliation


def initialize_transaction(path: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Create the base-local transaction that survives worktree cleanup."""
    if path.exists():
        current = read_json(path)
        if current.get("feature_run_id") != checkpoint.get("feature_run_id"):
            raise StateError("feature transaction belongs to another run")
        return current
    transaction = {
        "protocol": (
            "implement-v13-codex/feature-transaction/2"
            if checkpoint.get("controller_package_digest")
            else "implement-v13-codex/feature-transaction/1"
        ),
        "queue_run_id": checkpoint["queue_run_id"],
        "feature_run_id": checkpoint["feature_run_id"],
        "feature_index": checkpoint["feature_index"],
        "base_branch": checkpoint["base_branch"],
        "state": "prepared",
        "state_revision": 0,
        "created_at": _now(),
        "history": [{"state": "prepared", "at": _now()}],
    }
    for field in (
        "controller_package_protocol",
        "controller_package_version",
        "controller_package_digest",
        "controller_package_path",
    ):
        if field in checkpoint:
            transaction[field] = checkpoint[field]
    atomic_write_json(path, transaction)
    return transaction


def advance_transaction(path: Path, expected_revision: int, target: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Advance exactly one terminal transaction edge."""
    if target not in TRANSACTION_STATES:
        raise StateError(f"invalid transaction state: {target}")
    current = read_json(path)
    position = TRANSACTION_STATES.index(str(current["state"]))
    target_position = TRANSACTION_STATES.index(target)
    if target_position != position + 1:
        raise StateError("transaction must advance exactly one state")
    reserved = {"state", "state_revision", "history", "protocol", "queue_run_id", "feature_run_id", "feature_index", "base_branch", "created_at", "updated_at"}
    collision = sorted(reserved & evidence.keys())
    if collision:
        raise StateError(f"transaction evidence contains reserved fields: {', '.join(collision)}")
    history = list(current.get("history", []))
    history.append({"state": target, "at": _now(), "evidence": evidence})
    return cas_update(
        path,
        expected_revision,
        {"state": target, "updated_at": _now(), "history": history, "last_evidence": evidence},
    )


def write_feature_result(transaction_path: Path, result_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Write the immutable success token only after cleanup and base proofs."""
    transaction = read_json(transaction_path)
    if transaction.get("state") != "cleanup_complete":
        raise StateError("feature result requires cleanup_complete transaction")
    required = {"manifest", "merge_receipt", "clearance_report", "base_head", "cleanup_proof"}
    missing = sorted(required - result.keys())
    if missing:
        raise StateError(f"feature result missing fields: {', '.join(missing)}")
    reserved = {"protocol", "status", "queue_run_id", "feature_run_id", "feature_index", "completed_at"}
    collision = sorted(reserved & result.keys())
    if collision:
        raise StateError(f"feature result contains reserved fields: {', '.join(collision)}")
    document = {
        "protocol": "implement-v13-codex/feature-result/1",
        "status": "done",
        "queue_run_id": transaction["queue_run_id"],
        "feature_run_id": transaction["feature_run_id"],
        "feature_index": transaction["feature_index"],
        "completed_at": _now(),
        **result,
    }
    if result_path.exists():
        existing = read_json(result_path)
        comparable_existing = {key: value for key, value in existing.items() if key != "completed_at"}
        comparable_document = {key: value for key, value in document.items() if key != "completed_at"}
        if comparable_existing != comparable_document:
            raise StateError("immutable feature result already exists with different content")
        return existing
    atomic_write_json(result_path, document)
    return document


def _json_arg(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise StateError("JSON argument must be an object")
    return parsed


def main() -> int:
    """CLI entry point for deterministic feature state operations."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("checkpoint", type=Path)
    init.add_argument("payload")
    move = sub.add_parser("transition")
    move.add_argument("checkpoint", type=Path)
    move.add_argument("revision", type=int)
    move.add_argument("phase")
    move.add_argument("detail")
    move.add_argument("state")
    move.add_argument("--receipt", type=Path)
    block = sub.add_parser("block")
    block.add_argument("checkpoint", type=Path)
    block.add_argument("revision", type=int)
    block.add_argument("blocker")
    invalidate = sub.add_parser("invalidate-certification")
    invalidate.add_argument("checkpoint", type=Path)
    invalidate.add_argument("revision", type=int)
    invalidate.add_argument("evidence", type=Path)
    inputs = sub.add_parser("build-inputs")
    inputs.add_argument("worktree", type=Path)
    inputs.add_argument("artifact_dir", type=Path)
    inputs.add_argument("output", type=Path)
    inputs.add_argument("--declared", type=Path)
    inputs.add_argument("--relevant-paths", type=Path)
    reconcile = sub.add_parser("validate-reconciliation")
    reconcile.add_argument("input_manifest", type=Path)
    reconcile.add_argument("reconciliation", type=Path)
    reconcile.add_argument("--worktree", type=Path)
    reconcile.add_argument("--changed-paths", type=Path)
    tx_init = sub.add_parser("transaction-init")
    tx_init.add_argument("transaction", type=Path)
    tx_init.add_argument("checkpoint", type=Path)
    tx_move = sub.add_parser("transaction-advance")
    tx_move.add_argument("transaction", type=Path)
    tx_move.add_argument("revision", type=int)
    tx_move.add_argument("target")
    tx_move.add_argument("evidence")
    finish = sub.add_parser("feature-result")
    finish.add_argument("transaction", type=Path)
    finish.add_argument("output", type=Path)
    finish.add_argument("result")
    args = parser.parse_args()
    try:
        if args.command == "init":
            value = initialize_checkpoint(args.checkpoint, _json_arg(args.payload))
        elif args.command == "transition":
            value = transition(args.checkpoint, args.revision, args.phase, args.detail, args.state, args.receipt)
        elif args.command == "block":
            value = block_checkpoint(args.checkpoint, args.revision, _json_arg(args.blocker))
        elif args.command == "invalidate-certification":
            value = invalidate_certification(args.checkpoint, args.revision, args.evidence)
        elif args.command == "build-inputs":
            value = build_inputs(args.worktree, args.artifact_dir, args.declared, args.output, args.relevant_paths)
        elif args.command == "validate-reconciliation":
            value = validate_reconciliation(args.input_manifest, args.reconciliation, args.worktree, args.changed_paths)
        elif args.command == "transaction-init":
            value = initialize_transaction(args.transaction, read_json(args.checkpoint))
        elif args.command == "transaction-advance":
            value = advance_transaction(args.transaction, args.revision, args.target, _json_arg(args.evidence))
        else:
            value = write_feature_result(args.transaction, args.output, _json_arg(args.result))
    except (StateError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
