#!/usr/bin/env python3
"""Durably drive one dispatched feature until its queue item is terminal."""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from typing import Any, Callable
import uuid

from run_exec import preflight_response_schema, run as run_exec
from controller_package import (
    migration_authority_lock,
    validate_committed_migration,
    verify_controller_package,
)
from response_schema import production_response_schema_paths
from implementation_partition import ensure_partition, validate_worker_spec
from feature_state import (
    block_checkpoint,
    resume_blocked_checkpoint,
    resume_checkpoint_delta_scoped,
)
from closure_driver import PROTOCOL as CLOSURE_PROGRAM_PROTOCOL, run_closure_program
from review_closure import (
    validate_delta_scope,
    validate_invocation_spec as validate_closure_invocation_spec,
)
from state_io import (
    StateError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    locked,
    read_json,
    sha256_bytes,
    sha256_file,
)


PACKAGE = Path(__file__).resolve().parent.parent
QUEUE_STATE_SCRIPT = PACKAGE / "scripts" / "feature_queue_state.py"
COORDINATOR_PROTOCOL = "implement-v13-codex/coordinator-turn/2"
ROLLOVER_PROTOCOL = "implement-v13-codex/coordinator-rollover/2"
LEGACY_ROLLOVER_PROTOCOL = "implement-v13-codex/coordinator-rollover/1"
MIGRATION_ROLLOVER_PROTOCOL = "implement-v13-codex/controller-migration-rollover/1"
CONTROLLER_CHILD_ENV = "IMPLEMENT_V13_RUN_FEATURE_CHILD"
COORDINATOR_JUDGMENT_REASONS = {
    "novel_contract_choice",
    "ambiguous_dependency_decomposition",
    "semantic_conflict_resolution",
    "integration_risk_judgment",
}
EXECUTION_ENVIRONMENT_CONTEXT = (
    "EXECUTION_ENVIRONMENT=macOS BSD userland; shell=zsh\n"
    "Use rc for command exit status; never assign zsh's read-only status parameter.\n"
    "Use rg --files for file discovery; GNU find -printf is unavailable.\n"
    "Run optional rg discovery separately and handle exit 1 explicitly; do not hide failures from required assertions.\n"
)


def _emit_controller_phase(checkpoint: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    signature = (
        checkpoint.get("phase"), checkpoint.get("phase_detail"),
        checkpoint.get("phase_state"), checkpoint.get("state_revision"),
    )
    print(json.dumps({
        "type": "controller.phase",
        "controller": "run_feature.py",
        "phase_authority": "durable_checkpoint",
        "process_liveness_only": True,
        "phase": signature[0],
        "phase_detail": signature[1],
        "phase_state": signature[2],
        "state_revision": signature[3],
    }, sort_keys=True), file=sys.stderr, flush=True)
    return signature


def _queue_state_module() -> Any:
    spec = importlib.util.spec_from_file_location("feature_queue_state", QUEUE_STATE_SCRIPT)
    if spec is None or spec.loader is None:
        raise StateError("feature queue controller is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _absolute_under(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise StateError(f"dispatch {label} is missing")
    candidate = Path(value)
    target = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise StateError(f"dispatch {label} escapes base worktree") from None
    return target


def _active_feature(queue: dict[str, Any], run_id: str) -> dict[str, Any]:
    matches = [item for item in queue.get("features", []) if item.get("feature_run_id") == run_id]
    if len(matches) != 1:
        raise StateError("queue does not contain exactly one dispatched feature")
    return matches[0]


def _package_path(base: Path, dispatch: dict[str, Any]) -> Path:
    raw = dispatch.get("controller_package_path")
    if not isinstance(raw, str) or not raw:
        raise StateError("dispatch controller_package_path is missing")
    path = Path(raw)
    root = path.resolve() if path.is_absolute() else (base / path).resolve()
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise StateError("run-owned controller package escapes the base worktree") from exc
    digest = dispatch.get("controller_package_digest")
    if not isinstance(digest, str):
        raise StateError("dispatch controller_package_digest is missing")
    verify_controller_package(root, digest)
    if PACKAGE.parent.resolve() != root:
        raise StateError("run_feature.py is not executing from the dispatched run-owned package")
    return root


def _migration_path(base: Path, dispatch: dict[str, Any]) -> Path:
    raw = dispatch.get("controller_migration_journal_path")
    if not isinstance(raw, str) or not raw:
        raise StateError("resumed dispatch migration journal path is missing")
    path = Path(raw)
    target = path.resolve() if path.is_absolute() else (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise StateError("resumed dispatch migration journal escapes the base worktree") from exc
    return target


def _coordinator_generation(dispatch: dict[str, Any]) -> str:
    migration_id = dispatch.get("controller_migration_id")
    return f"migrated-{str(migration_id)[:16]}" if migration_id else "original"


def _coordinator_receipt_id(dispatch: dict[str, Any], turn: int) -> str:
    if _coordinator_generation(dispatch) == "original":
        return f"{dispatch['feature_run_id']}:COORDINATOR:drive:feature_coordinator:{turn}:1"
    return (
        f"{dispatch['feature_run_id']}:COORDINATOR:drive:"
        f"{_coordinator_generation(dispatch)}:feature_coordinator:{turn}:1"
    )


def _coordinator_limits(dispatch: dict[str, Any]) -> dict[str, int] | None:
    raw = dispatch.get("coordinator_limits")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "authority",
        "max_turns_per_context",
        "input_token_slope_window",
        "max_input_token_slope",
    }:
        raise StateError("coordinator_limits must be a closed run-owned configuration")
    if not isinstance(raw.get("authority"), str) or not raw["authority"].strip():
        raise StateError("coordinator limits require explicit operator or safety authority")
    normalized: dict[str, int] = {}
    for field in (
        "max_turns_per_context",
        "input_token_slope_window",
        "max_input_token_slope",
    ):
        value = raw.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise StateError(f"coordinator limit {field} must be a positive configured integer")
        normalized[field] = value
    if not isinstance(dispatch.get("controller_package_digest"), str):
        raise StateError("coordinator limits require a run-owned package digest")
    return normalized


def _optional_artifact_sha256(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def _hash_artifact_set(paths: list[Path]) -> str:
    return sha256_bytes(
        canonical_bytes(
            [
                {"path": str(path), "sha256": sha256_file(path)}
                for path in sorted(paths)
                if path.is_file()
            ]
        )
    )


def _receipt_provider_usage(receipt_path: Path) -> dict[str, Any]:
    """Read provider usage from a current receipt or hash-bound legacy stdout."""
    receipt = read_json(receipt_path)
    usage = receipt.get("provider_usage")
    if isinstance(usage, dict) and usage.get("status") in {"recorded", "unknown"}:
        return usage
    stdout_path = Path(str(receipt.get("stdout_path", "")))
    artifact_hashes = receipt.get("artifact_sha256")
    expected = (
        artifact_hashes.get("stdout")
        if isinstance(artifact_hashes, dict)
        else None
    )
    unknown = {
        "status": "unknown",
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
    }
    if (
        not stdout_path.is_file()
        or not isinstance(expected, str)
        or sha256_file(stdout_path) != expected
    ):
        return unknown
    observed: list[dict[str, int]] = []
    for line in stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        raw = event.get("usage")
        if not isinstance(raw, dict):
            continue
        normalized: dict[str, int] = {}
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = raw.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                normalized = {}
                break
            normalized[field] = value
        if normalized:
            observed.append(normalized)
    if not observed or any(item != observed[-1] for item in observed[:-1]):
        return unknown
    return {"status": "recorded", **observed[-1]}


def _coordinator_receipt_path(
    artifact_dir: Path, dispatch: dict[str, Any], turn: int
) -> Path:
    slug = _coordinator_receipt_id(dispatch, turn).replace(":", "-").replace("/", "-")
    return artifact_dir / f"{slug}.receipt.json"


def _write_rollover_summary(
    *,
    dispatch: dict[str, Any],
    checkpoint_path: Path,
    artifact_dir: Path,
    prior_thread_id: str,
    prior_last_turn: int,
    generation: int,
    cause: str,
    turns_in_context: int,
    input_tokens: list[int | None],
    coordinator_turns_avoided: int,
) -> dict[str, Any]:
    if cause not in {
        "phase_boundary",
        "closure_boundary",
        "turn_limit",
        "input_token_slope_limit",
        "package_migration",
    }:
        raise StateError("unknown coordinator rollover cause")
    ledger_path = artifact_dir / "review-closure-ledger.v1.json"
    current_graph_path = artifact_dir / "repair-dependency-graph.v2.json"
    legacy_graph_path = artifact_dir / "repair-dependency-graph.v1.json"
    dependency_graph_path = (
        current_graph_path if current_graph_path.is_file() else legacy_graph_path
    )
    decisions_raw = dispatch.get("decision_record_path")
    decisions_path = (
        Path(decisions_raw).resolve()
        if isinstance(decisions_raw, str) and Path(decisions_raw).is_absolute()
        else artifact_dir / "decisions.unavailable"
    )
    unresolved: list[dict[str, Any]] = []
    for path in sorted(artifact_dir.glob("coordinator-output-*.json")):
        output = read_json(path)
        if output.get("judgment_reason"):
            raw_turn = path.stem.rsplit("-", 1)[-1]
            unresolved.append({
                "turn": int(raw_turn) if raw_turn.isdigit() else 0,
                "judgment_reason": output["judgment_reason"],
            })
    window = int(dispatch["coordinator_limits"]["input_token_slope_window"])
    token_window = input_tokens[-window:]
    usage_recorded = all(isinstance(value, int) for value in input_tokens)
    numeric_tokens = [int(value) for value in input_tokens if isinstance(value, int)]
    numeric_window = [int(value) for value in token_window if isinstance(value, int)]
    token_slope = (
        max(numeric_window) - min(numeric_window)
        if usage_recorded and len(numeric_window) > 1
        else (0 if usage_recorded else None)
    )
    body = {
        "protocol": ROLLOVER_PROTOCOL,
        "feature_run_id": dispatch["feature_run_id"],
        "generation": generation,
        "prior_last_turn": prior_last_turn,
        "cause": cause,
        "prior_thread_id": prior_thread_id,
        "checkpoint_sha256": sha256_file(checkpoint_path),
        "closure_ledger_sha256": _optional_artifact_sha256(ledger_path),
        "dependency_graph_sha256": _optional_artifact_sha256(dependency_graph_path),
        "recent_receipts_sha256": _hash_artifact_set(
            sorted(artifact_dir.glob("*COORDINATOR*.receipt.json"))[-window:]
        ),
        "decisions_sha256": (
            sha256_file(decisions_path)
            if decisions_path.is_file()
            else sha256_bytes(canonical_bytes([]))
        ),
        "unresolved_judgments_sha256": sha256_bytes(canonical_bytes(unresolved)),
        "controller_package_digest": dispatch["controller_package_digest"],
        "telemetry": {
            "status": "recorded" if usage_recorded else "unknown",
            "turns_in_context": turns_in_context,
            "input_tokens_in_context": (
                sum(numeric_tokens) if usage_recorded else None
            ),
            "input_token_slope": token_slope,
            "slope_window_turns": window,
            "coordinator_turns_avoided": coordinator_turns_avoided,
        },
    }
    body["summary_sha256"] = sha256_bytes(canonical_bytes(body))
    summary_path = artifact_dir / f"coordinator-rollover-{generation:04d}.v2.json"
    atomic_write_json(summary_path, body)
    return {**body, "summary_path": str(summary_path)}


def _validate_rollover_ack(output: dict[str, Any], summary: dict[str, Any]) -> None:
    ack = output.get("rollover_ack")
    expected = {
        "summary_sha256": summary["summary_sha256"],
        "controller_package_digest": summary["controller_package_digest"],
        "checkpoint_sha256": summary["checkpoint_sha256"],
        "closure_ledger_sha256": summary["closure_ledger_sha256"],
        "dependency_graph_sha256": summary["dependency_graph_sha256"],
    }
    if ack != expected:
        raise StateError("fresh coordinator did not acknowledge the exact rollover hashes")


def _settle_blocked(
    serial: Any,
    queue_path: Path,
    dispatch: dict[str, Any],
    blocker: dict[str, Any],
    token: str,
) -> dict[str, Any]:
    queue = read_json(queue_path)
    feature = _active_feature(queue, str(dispatch["feature_run_id"]))
    if feature.get("status") == "blocked":
        return {
            "status": "blocked",
            "feature_run_id": dispatch["feature_run_id"],
            "resume_token": token,
            "already_settled": True,
        }
    serial.atomic_mutate(
        queue_path,
        lambda current: serial.block_feature(
            current,
            index=dispatch["feature_index"],
            coordinator_id=str(dispatch["coordinator_id"]),
            lease_id=str(dispatch["lease_id"]),
            blocker=blocker,
            resume_token=token,
        ),
        expected_revision=int(queue.get("state_revision", 0)),
    )
    return {
        "status": "blocked",
        "feature_run_id": dispatch["feature_run_id"],
        "resume_token": token,
    }


def settle_existing_blocked(dispatch_path: Path) -> dict[str, Any]:
    """Settle a queue from an already-blocked checkpoint without launching a model."""
    dispatch = read_json(dispatch_path.resolve())
    base = Path(str(dispatch.get("base_worktree_path", ""))).resolve()
    queue_path = Path(str(dispatch.get("queue_path", ""))).resolve()
    if not base.is_dir() or not queue_path.is_absolute():
        raise StateError("dispatch requires absolute base_worktree_path and queue_path")
    try:
        queue_path.relative_to(base)
    except ValueError:
        raise StateError("dispatch queue_path escapes base worktree") from None
    checkpoint_path = _absolute_under(base, dispatch.get("checkpoint_path"), "checkpoint_path")
    checkpoint = read_json(checkpoint_path)
    if checkpoint.get("phase_state") != "blocked":
        raise StateError("checkpoint is not blocked")
    active = checkpoint.get("active_blocker")
    if not isinstance(active, dict):
        raise StateError("blocked checkpoint lacks active_blocker")
    blocker_class = active.get("blocker_class")
    resume_condition = active.get("resume_condition")
    if not isinstance(blocker_class, str) or not blocker_class or not isinstance(resume_condition, str):
        raise StateError("checkpoint active_blocker is incomplete")
    reason = active.get("reason") if isinstance(active.get("reason"), str) else blocker_class
    evidence_path = active.get("evidence_path")
    if isinstance(evidence_path, str) and evidence_path:
        evidence = read_json(Path(evidence_path).resolve())
        if isinstance(evidence.get("error"), str) and evidence["error"]:
            reason = evidence["error"]
    return _settle_blocked(
        _queue_state_module(),
        queue_path,
        dispatch,
        {
            "blocker_class": blocker_class,
            "reason": reason,
            "resume_condition": resume_condition,
        },
        uuid.uuid4().hex,
    )


def _write_turn_inputs(
    dispatch: dict[str, Any], checkpoint: dict[str, Any], artifact_dir: Path, turn: int,
    resume_thread_id: str | None,
) -> Path:
    prompt = artifact_dir / f"coordinator-turn-{turn}.prompt.md"
    schema = PACKAGE / "schemas" / "feature-coordinator-result.schema.json"
    base = Path(str(dispatch["base_worktree_path"])).resolve()
    checkpoint_path = _absolute_under(base, dispatch["checkpoint_path"], "checkpoint_path")
    previous_result = artifact_dir / f"controller-child-result-{turn - 1:06d}.json"
    rollover_summaries = _runtime_rollover_paths(artifact_dir)
    rollover_summary = (
        rollover_summaries[-1]
        if resume_thread_id is None and turn > 1 and rollover_summaries
        else None
    )
    bootstrap = (
        "You are the feature coordinator already launched and supervised by run_feature.py. "
        "Never invoke run_feature.py or start_planning.py, including for help or recovery; doing "
        "so would recursively enter the controller. Never invoke feature_queue_state.py, mutate the "
        "feature queue, acknowledge a feature, or release a dispatch lease; the supervising "
        "run_feature.py process exclusively owns those transitions. Never invoke run_exec.py inside this "
        "sandbox. When one model role is required, write its complete run_exec spec beneath the run artifact "
        "directory and return status=invoke with its absolute path in invocation_spec_path. When two or three "
        "independent roles are required, write a batch manifest with protocol "
        "implement-v13-codex/invocation-batch/1 and an invocations array of their absolute spec paths, then "
        "return the batch manifest path in invocation_spec_path. The outer "
        "controller will run it and persist the result at PREVIOUS_CHILD_RESULT_PATH before the next "
        "coordinator turn. Execute deterministic phase work directly with the other lower-level scripts named "
        "by the installed skill. Read the installed "
        f"skill at {PACKAGE / 'SKILL.md'} and its required references once, then continue from the "
        "durable checkpoint. You own no feature queue judgment: continue phase work until the "
        "checkpoint is durably blocked after all required revision opportunities, or until the "
        "feature result is written. Do not stop merely to report progress. Read run context from these paths; "
        f"Every brokered child prompt receives this controller-injected environment context:\n{EXECUTION_ENVIRONMENT_CONTEXT}\n\n"
        "During REVIEWING repairs, use review_closure.py and REVIEW_CLOSURE_LEDGER_PATH. Create the ledger "
        "from the triaged findings before launching any closure-test author, designer, fixer, or targeted "
        "reviewer. The outer controller rejects repair invocations that do not match the ledger's active "
        "finding, required independent role, complexity route, attempt-history hash, and state. "
        "For a normal first-attempt closure, write one implement-v13-codex/closure-program/1 manifest with "
        "the canonical author_test, optional design and design_review, fix, and targeted_review spec paths, "
        "and give author_test a workspace-write sandbox plus one to four normalized repository-relative "
        "allowed_write_paths naming only the supplemental test files it may create or update. All other "
        "review actions remain mutation-protected. "
        "then return that manifest as invocation_spec_path. The deterministic closure driver advances the "
        "ledger and runs the chain without coordinator turns between stages; it returns early for rejection "
        "or escalation. "
        "Before IMPLEMENTING, run implementation_partition.py with PLAN_PATH and "
        "IMPLEMENTATION_PARTITION_PATH. Every implementation_worker spec must copy exactly one group's "
        "group_id, step_ids, and write_paths into implementation_group_id, assigned_step_ids, and "
        "allowed_write_paths; dispatch dependency-ready groups in manifest order even when the plan is linear. "
        "do not ask the controller to embed their contents. PREVIOUS_CHILD_RESULT_PATH may not exist on the "
        "first turn.\n\n"
    )
    continuation = (
        "Continue the same supervised feature-coordinator thread from durable state. Do not reread the installed "
        "skill or repository orientation unless a referenced file hash changed. Never invoke run_feature.py, "
        "run_exec.py, or feature_queue_state.py. Return the next schema-bound coordinator action after consuming "
        "PREVIOUS_CHILD_RESULT_PATH and the current checkpoint.\n\n"
    )
    rollover_context = (
        "This is a fresh coordinator context after a controller-owned rollover. "
        "Read ROLLOVER_SUMMARY_PATH before any judgment and echo its exact summary, "
        "package, checkpoint, closure-ledger, and dependency-graph hashes in rollover_ack. "
        "Do not resume or claim the prior thread.\n\n"
        if rollover_summary is not None
        else ""
    )
    delta_scope = checkpoint.get("delta_resume_scope")
    delta_context = (
        (
            "This run resumed delta-scoped from a terminal block. The verified candidate commit "
            f"{delta_scope.get('candidate_commit_sha')} is already checked out in the worktree; the "
            "controller verified it against HEAD before reopening this checkpoint. Do not restart "
            "PLANNING or IMPLEMENTING and do not rebuild committed work. Close exactly the open "
            "closure fingerprints recorded in the checkpoint's delta_resume_scope through "
            "review_closure.py; for re-verification run the recorded verification_slice commands "
            "and closure-bound targeted review rather than the full certification suite. The full "
            "COMMITTING gates still run once, after every open finding is closed.\n\n"
        )
        if isinstance(delta_scope, dict)
        else ""
    )
    repair_effect_contract = (
        "For every architectural repair, the adversarial test, repair design, and design review must each "
        "emit the canonical repair-effect-contract. The controller compares the three classifications "
        "deterministically before ready_for_fix and consumes no fixer attempt on a conflict. Malformed role "
        "output may persist only controller-owned failure_checkpoint, blocked_queue, failure_summary, and "
        "failure_event state; success_result, success_receipt, integration_artifact, and "
        "dispatcher_acknowledgement must remain absent, and base_git_state must remain unchanged. Prose "
        "approval cannot override this structured contract. An operator-resolved blocked design must use "
        "review_closure.py resolve-design-contradiction before a fresh design.\n\n"
    )
    repair_model_policy = (
        "Current REVIEWING model policy: repair_designer must use gpt-5.6-terra with medium reasoning; "
        "code_fixer must use gpt-5.6-terra with medium reasoning; originating reviewers remain "
        "gpt-5.6-sol with medium reasoning. This policy applies on every continuation turn and overrides "
        "older coordinator memory.\n\n"
    )
    paths = (
        f"DISPATCH_PATH={artifact_dir / 'dispatch.v1.json'}\n"
        f"CHECKPOINT_PATH={checkpoint_path}\n"
        f"ARTIFACT_DIR={artifact_dir}\n"
        f"PLAN_PATH={artifact_dir / 'plan.v1.json'}\n"
        f"IMPLEMENTATION_PARTITION_PATH={artifact_dir / 'implementation-partition.v1.json'}\n"
        f"PLANNING_INPUT_MANIFEST_PATH={artifact_dir / 'planning-inputs.v1.json'}\n"
        f"REVIEW_CLOSURE_LEDGER_PATH={artifact_dir / 'review-closure-ledger.v1.json'}\n"
        f"PREVIOUS_CHILD_RESULT_PATH={previous_result}\n"
        f"ROLLOVER_SUMMARY_PATH={rollover_summary or ''}\n"
    )
    prompt.write_text(
        (bootstrap if resume_thread_id is None else continuation)
        + rollover_context
        + delta_context
        + repair_effect_contract
        + repair_model_policy
        + paths,
        encoding="utf-8",
    )
    spec_path = artifact_dir / f"coordinator-turn-{turn}.spec.json"
    spec: dict[str, Any] = {
        "receipt_id": _coordinator_receipt_id(dispatch, turn),
        "queue_run_id": dispatch["queue_run_id"],
        "feature_run_id": dispatch["feature_run_id"],
        "phase": "COORDINATOR",
        "phase_detail": "drive",
        "role": "feature_coordinator",
        "attempt": 1,
        "cwd": dispatch["worktree_path"],
        "prompt_path": str(prompt),
        "schema_path": str(schema),
        "artifact_dir": str(artifact_dir),
        "model": "gpt-5.6-sol",
        "reasoning": "medium",
        "sandbox": "workspace-write",
        "writable_roots": [str(Path(dispatch["base_worktree_path"]).resolve())],
        "controller_child": True,
        "wall_timeout_seconds": 43200,
        "expected": {"protocol": COORDINATOR_PROTOCOL},
    }
    if dispatch.get("controller_package_digest"):
        spec.update(
            {
                "controller_package_digest": dispatch["controller_package_digest"],
                "controller_package_path": str(
                    _package_path(Path(str(dispatch["base_worktree_path"])).resolve(), dispatch)
                ),
            }
        )
    if dispatch.get("controller_migration_journal_path"):
        spec["controller_migration_journal_path"] = str(
            _migration_path(Path(str(dispatch["base_worktree_path"])).resolve(), dispatch)
        )
        spec["controller_migration_receipt_sha256"] = dispatch[
            "controller_migration_receipt_sha256"
        ]
    if resume_thread_id is not None:
        spec["resume_thread_id"] = resume_thread_id
    atomic_write_json(spec_path, spec)
    return spec_path


def _artifact_path(artifact_dir: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise StateError(f"{label} must be a nonempty absolute path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise StateError(f"{label} must be absolute")
    path = candidate.resolve()
    try:
        path.relative_to(artifact_dir)
    except ValueError:
        raise StateError(f"{label} must be beneath the run artifact directory") from None
    return path


def _validate_requested_spec(
    dispatch: dict[str, Any], artifact_dir: Path, raw: Any
) -> Path:
    path = _artifact_path(artifact_dir, raw, "coordinator invocation spec")
    spec = read_json(path)
    if spec.get("feature_run_id") != dispatch.get("feature_run_id"):
        raise StateError("coordinator invocation feature_run_id mismatch")
    if spec.get("phase") == "COORDINATOR" or spec.get("role") == "feature_coordinator":
        raise StateError("coordinator cannot broker another feature coordinator")
    if spec.get("controller_child"):
        raise StateError("brokered child cannot be marked as a controller child")
    if dispatch.get("controller_package_digest"):
        spec["controller_package_digest"] = dispatch["controller_package_digest"]
        spec["controller_package_path"] = str(
            _package_path(Path(str(dispatch["base_worktree_path"])).resolve(), dispatch)
        )
        if dispatch.get("controller_migration_journal_path"):
            spec["controller_migration_journal_path"] = str(
                _migration_path(Path(str(dispatch["base_worktree_path"])).resolve(), dispatch)
            )
            spec["controller_migration_receipt_sha256"] = dispatch[
                "controller_migration_receipt_sha256"
            ]
        atomic_write_json(path, spec)
    validate_closure_invocation_spec(spec, artifact_dir)
    if spec.get("phase") == "IMPLEMENTING" and spec.get("role") == "implementation_worker":
        partition = ensure_partition(
            artifact_dir / "plan.v1.json",
            artifact_dir / "implementation-partition.v1.json",
        )
        validate_worker_spec(spec, partition)
    prompt_path = _artifact_path(artifact_dir, spec.get("prompt_path"), "brokered child prompt")
    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.startswith(EXECUTION_ENVIRONMENT_CONTEXT):
        atomic_write_bytes(
            prompt_path,
            (EXECUTION_ENVIRONMENT_CONTEXT + "\n\n" + prompt).encode("utf-8"),
        )
    return path


def _requested_specs(
    dispatch: dict[str, Any], artifact_dir: Path, output: dict[str, Any]
) -> list[Path]:
    if output.get("status") != "invoke":
        raise StateError("coordinator child request requires invoke status")
    request_path = _artifact_path(
        artifact_dir, output.get("invocation_spec_path"), "coordinator invocation_spec_path"
    )
    request = read_json(request_path)
    if request.get("protocol") != "implement-v13-codex/invocation-batch/1":
        return [_validate_requested_spec(dispatch, artifact_dir, str(request_path))]
    raw_specs = request.get("invocations")
    if not isinstance(raw_specs, list) or not 2 <= len(raw_specs) <= 3:
        raise StateError("coordinator invocation batch must contain two or three specs")
    paths = [_validate_requested_spec(dispatch, artifact_dir, raw) for raw in raw_specs]
    if len(paths) != len(set(paths)):
        raise StateError("coordinator invocation batch contains duplicate specs")
    repair_specs = [path for path in paths if read_json(path).get("closure_action")]
    if len(repair_specs) > 1:
        raise StateError("finding-level repair actions must run one closure group at a time")
    return paths


def _workspace_fingerprint(cwd: Path) -> str:
    """Hash the complete tracked diff plus untracked file content for reviewer mutation checks."""
    status = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True, capture_output=True,
    ).stdout
    diff = subprocess.run(
        ["git", "-C", str(cwd), "diff", "--binary", "HEAD"],
        check=True, capture_output=True,
    ).stdout
    untracked = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "--others", "--exclude-standard", "-z"],
        check=True, capture_output=True,
    ).stdout
    digest = hashlib.sha256(status + b"\0" + diff + b"\0" + untracked)
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        path = cwd / raw_path.decode("utf-8", errors="surrogateescape")
        digest.update(raw_path + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _workspace_snapshot(cwd: Path) -> dict[str, str]:
    """Return exact tracked/untracked file state for bounded author-test writes."""
    raw_paths = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True, capture_output=True,
    ).stdout
    snapshot: dict[str, str] = {}
    for raw_path in sorted(set(item for item in raw_paths.split(b"\0") if item)):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path = cwd / relative
        digest = hashlib.sha256()
        if path.is_symlink():
            digest.update(b"symlink\0" + os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        else:
            digest.update(b"missing\0")
        snapshot[relative] = digest.hexdigest()
    return snapshot


def _author_test_write_paths(spec: dict[str, Any], cwd: Path) -> set[str]:
    raw_paths = spec.get("allowed_write_paths")
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 4:
        raise StateError("author_test requires one to four allowed_write_paths")
    allowed: set[str] = set()
    for raw in raw_paths:
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
            raise StateError("author_test allowed_write_paths must be nonempty relative paths")
        target = (cwd / raw).resolve()
        try:
            relative = target.relative_to(cwd).as_posix()
        except ValueError:
            raise StateError("author_test allowed_write_paths escape the feature worktree") from None
        if relative != Path(raw).as_posix() or relative in {"", "."}:
            raise StateError("author_test allowed_write_paths must be normalized file paths")
        allowed.add(relative)
    if len(allowed) != len(raw_paths):
        raise StateError("author_test allowed_write_paths contain duplicates")
    return allowed


def _workspace_write_reviewer(spec: dict[str, Any]) -> bool:
    if spec.get("sandbox") != "workspace-write":
        return False
    role = str(spec.get("role", ""))
    return (
        spec.get("phase") in {"PLAN_REVIEW", "CODE_REVIEW"}
        or spec.get("closure_action") in {"design_review", "targeted_review"}
        or ("reviewer" in role and spec.get("closure_action") != "author_test")
    )


def _run_requested_spec(spec_path: Path) -> dict[str, Any]:
    try:
        spec = read_json(spec_path)
        protected = _workspace_write_reviewer(spec)
        cwd = Path(str(spec.get("cwd", ""))).resolve()
        author_test = spec.get("sandbox") == "workspace-write" and spec.get("closure_action") == "author_test"
        allowed_author_paths = _author_test_write_paths(spec, cwd) if author_test else set()
        before = _workspace_fingerprint(cwd) if protected else ""
        author_before = _workspace_snapshot(cwd) if author_test else {}
        try:
            receipt = run_exec(spec_path)
        finally:
            if protected and _workspace_fingerprint(cwd) != before:
                raise StateError("workspace-write reviewer mutated the feature tree")
            if author_test:
                author_after = _workspace_snapshot(cwd)
                changed = {
                    path for path in set(author_before) | set(author_after)
                    if author_before.get(path) != author_after.get(path)
                }
                disallowed = sorted(changed - allowed_author_paths)
                if disallowed:
                    raise StateError(
                        "author_test mutated paths outside allowed_write_paths: "
                        + ", ".join(disallowed)
                    )
        return {
            "status": str(receipt.get("status", "unknown")),
            "receipt_id": receipt.get("receipt_id", ""),
            "receipt_path": str(
                Path(str(receipt.get("output_path", ""))).with_name(
                    f"{str(receipt.get('receipt_id', '')).replace(':', '-')}.receipt.json"
                )
            ),
            "output_path": receipt.get("output_path", ""),
            "error": "",
            "failure_class": "",
            "retry_allowed": False,
            "terminal_cause": receipt.get("terminal_cause"),
            "invocation_spec_path": str(spec_path),
        }
    except Exception as exc:
        message = str(exc)
        spec = read_json(spec_path)
        slug = str(spec.get("receipt_id", "")).replace(":", "-").replace("/", "-")
        receipt_path = Path(str(spec.get("artifact_dir", ""))) / f"{slug}.receipt.json"
        terminal_cause = None
        if receipt_path.is_file():
            candidate = read_json(receipt_path)
            if isinstance(candidate.get("terminal_cause"), dict):
                terminal_cause = candidate["terminal_cause"]
        if terminal_cause is not None:
            failure_class = str(terminal_cause.get("class"))
            retry_allowed = terminal_cause.get("retryable") is True
        elif "workspace-write reviewer mutated" in message:
            failure_class = "reviewer_tree_mutation"
            retry_allowed = False
        elif "author_test mutated paths outside" in message:
            failure_class = "author_test_scope_violation"
            retry_allowed = False
        elif any(token in message for token in ("expected semantic field", "canonical repair-design", "expected identity")):
            failure_class = "identity_or_schema_preflight"
            retry_allowed = False
        else:
            failure_class = "child_invocation_failure"
            retry_allowed = True
        return {
            "status": "failed", "receipt_id": "", "receipt_path": "", "output_path": "",
            "error": f"{type(exc).__name__}: {exc}", "failure_class": failure_class,
            "retry_allowed": retry_allowed,
            "terminal_cause": terminal_cause,
            "invocation_spec_path": str(spec_path),
        }


def _broker_invocation(
    dispatch: dict[str, Any], artifact_dir: Path, output: dict[str, Any], turn: int
) -> dict[str, Any]:
    try:
        request_path = _artifact_path(
            artifact_dir, output.get("invocation_spec_path"), "coordinator invocation_spec_path"
        )
        request = read_json(request_path)
        if request.get("protocol") == CLOSURE_PROGRAM_PROTOCOL:
            closure = run_closure_program(
                request_path,
                artifact_dir,
                lambda _action, spec_path: _run_requested_spec(
                    _validate_requested_spec(dispatch, artifact_dir, str(spec_path))
                ),
            )
            result = {
                "protocol": "implement-v13-codex/controller-child-result/1",
                "status": "succeeded" if closure["status"] == "closed" else closure["status"],
                "receipt_id": "",
                "receipt_path": "",
                "output_path": "",
                "error": "",
                "invocation_spec_path": str(request_path),
                "closure_program_result": closure,
            }
        else:
            specs = _requested_specs(dispatch, artifact_dir, output)
            with ThreadPoolExecutor(max_workers=len(specs)) as executor:
                invocations = list(executor.map(_run_requested_spec, specs))
            if len(invocations) == 1:
                result = {"protocol": "implement-v13-codex/controller-child-result/1", **invocations[0]}
            else:
                result = {
                    "protocol": "implement-v13-codex/controller-child-batch-result/1",
                    "status": "succeeded" if all(item["status"] == "succeeded" for item in invocations) else "failed",
                    "invocations": invocations,
                    "invocation_spec_path": output.get("invocation_spec_path", ""),
                }
    except Exception as exc:
        result = {
            "protocol": "implement-v13-codex/controller-child-result/1",
            "status": "failed", "receipt_id": "", "receipt_path": "", "output_path": "",
            "error": f"{type(exc).__name__}: {exc}",
            "invocation_spec_path": output.get("invocation_spec_path", ""),
        }
    path = artifact_dir / f"controller-child-result-{turn:06d}.json"
    atomic_write_json(path, result)
    result["result_path"] = str(path)
    return result


def _invoke_real(
    dispatch: dict[str, Any], checkpoint: dict[str, Any], artifact_dir: Path, turn: int,
    resume_thread_id: str | None,
) -> tuple[dict[str, Any], str]:
    receipt = run_exec(_write_turn_inputs(dispatch, checkpoint, artifact_dir, turn, resume_thread_id))
    if receipt.get("status") != "succeeded":
        raise StateError("feature coordinator process did not succeed")
    output = read_json(Path(str(receipt["output_path"])))
    thread_id = receipt.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise StateError("feature coordinator receipt has no thread ID")
    return output, thread_id


def _recover_coordinator_position(
    artifact_dir: Path, feature_run_id: str, generation: str = "original"
) -> tuple[int, str | None]:
    """Recover the next turn and exact thread from this run's succeeded receipts."""
    middle = "" if generation == "original" else f"{generation}-"
    prefix = f"{feature_run_id}-COORDINATOR-drive-{middle}feature_coordinator-"
    receipts: list[tuple[int, str]] = []
    for path in artifact_dir.glob(f"{prefix}*-1.receipt.json"):
        suffix = path.name[len(prefix):]
        raw_turn, separator, tail = suffix.partition("-")
        if not separator or tail != "1.receipt.json" or not raw_turn.isdigit():
            raise StateError("malformed coordinator receipt filename")
        receipt = read_json(path)
        expected_id = (
            f"{feature_run_id}:COORDINATOR:drive:"
            + ("" if generation == "original" else f"{generation}:")
            + f"feature_coordinator:{raw_turn}:1"
        )
        if receipt.get("receipt_id") != expected_id or receipt.get("status") != "succeeded":
            raise StateError("coordinator recovery found a nonterminal or mismatched receipt")
        thread_id = receipt.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise StateError("coordinator recovery receipt has no thread ID")
        receipts.append((int(raw_turn), thread_id))
    if not receipts:
        return 1, None
    turns = sorted(turn for turn, _thread in receipts)
    if turns != list(range(1, turns[-1] + 1)):
        raise StateError("coordinator recovery receipts are not contiguous")
    receipt_threads = dict(receipts)
    summaries = _runtime_rollover_paths(artifact_dir)
    if not summaries:
        threads = {thread for _turn, thread in receipts}
        if len(threads) != 1:
            raise StateError("coordinator recovery receipts changed thread identity")
        return turns[-1] + 1, threads.pop()
    boundaries: list[tuple[int, str]] = []
    prior_boundary = 0
    for path in summaries:
        summary = read_json(path)
        if summary.get("protocol") not in {
            ROLLOVER_PROTOCOL,
            LEGACY_ROLLOVER_PROTOCOL,
        }:
            raise StateError("coordinator recovery found an invalid rollover protocol")
        recorded_hash = summary.get("summary_sha256")
        unsigned = dict(summary)
        unsigned.pop("summary_sha256", None)
        if recorded_hash != sha256_bytes(canonical_bytes(unsigned)):
            raise StateError("coordinator recovery found a stale rollover summary hash")
        boundary = summary.get("prior_last_turn")
        prior_thread = summary.get("prior_thread_id")
        if (
            not isinstance(boundary, int)
            or boundary <= prior_boundary
            or boundary not in receipt_threads
            or receipt_threads[boundary] != prior_thread
        ):
            raise StateError("coordinator rollover does not bind the prior receipt segment")
        segment_threads = {
            receipt_threads[index]
            for index in range(prior_boundary + 1, boundary + 1)
        }
        if len(segment_threads) != 1 or prior_thread not in segment_threads:
            raise StateError("pre-rollover coordinator segment changed thread identity")
        if boundary + 1 in receipt_threads and receipt_threads[boundary + 1] == prior_thread:
            raise StateError("coordinator resumed the pre-rollover thread")
        boundaries.append((boundary, prior_thread))
        prior_boundary = boundary
    final_threads = {
        receipt_threads[index]
        for index in range(prior_boundary + 1, turns[-1] + 1)
    }
    if len(final_threads) > 1:
        raise StateError("post-rollover coordinator segment changed thread identity")
    return turns[-1] + 1, (next(iter(final_threads)) if final_threads else None)


def _runtime_rollover_paths(artifact_dir: Path) -> list[Path]:
    """Return only hash-bound coordinator context rollovers.

    Controller migrations also persist a rollover provenance document.  Early
    migration packages used the coordinator-rollover protocol even though that
    document intentionally has no turn boundary or prior thread.  It is not a
    runtime context summary and must never participate in coordinator recovery.
    """
    runtime: list[Path] = []
    for path in sorted(
        [
            *artifact_dir.glob("coordinator-rollover-*.v1.json"),
            *artifact_dir.glob("coordinator-rollover-*.v2.json"),
        ]
    ):
        summary = read_json(path)
        protocol = summary.get("protocol")
        if protocol == MIGRATION_ROLLOVER_PROTOCOL:
            continue
        if (
            protocol in {ROLLOVER_PROTOCOL, LEGACY_ROLLOVER_PROTOCOL}
            and "controller_migration_id" in summary
            and "generation" not in summary
        ):
            continue
        runtime.append(path)
    return runtime


def _worktree_head(worktree: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise StateError("delta-scoped resume could not read the candidate worktree HEAD")
    return completed.stdout.strip()


def _resume_checkpoint_with_authorization(
    checkpoint_path: Path,
    checkpoint: dict[str, Any],
    authorization: Any,
    worktree: Path,
) -> dict[str, Any]:
    """Reopen a blocked checkpoint; a delta-scoped authorization imports the candidate."""
    if not isinstance(authorization, dict):
        raise StateError("blocked checkpoint lacks queue resume authorization")
    delta_scope = authorization.get("delta_scope")
    if delta_scope is None:
        return resume_blocked_checkpoint(
            checkpoint_path,
            int(checkpoint.get("state_revision", -1)),
            authorization,
        )
    if not isinstance(delta_scope, dict):
        raise StateError("delta-scoped resume authorization scope must be an object")
    ledger_path = Path(str(delta_scope.get("ledger_path", "")))
    if not ledger_path.is_absolute():
        raise StateError("delta scope ledger path must be absolute")
    validate_delta_scope(ledger_path, delta_scope)
    head = _worktree_head(worktree)
    if head != delta_scope.get("candidate_commit_sha"):
        raise StateError(
            "delta-scoped resume requires the worktree HEAD to equal the verified candidate commit"
        )
    return resume_checkpoint_delta_scoped(
        checkpoint_path,
        int(checkpoint.get("state_revision", -1)),
        authorization,
        delta_scope,
    )


def _prepare_resumed_run(
    *,
    dispatch: dict[str, Any],
    base: Path,
    queue_path: Path,
    checkpoint_path: Path,
    artifact_dir: Path,
    expected_migration_sha256: str | None,
    expected_package_digest: str | None,
    coordinator_id: str | None,
    lease_id: str | None,
) -> dict[str, Any]:
    """Validate, consume, and reopen one committed migrated run without a child."""
    if dispatch.get("dispatch_action") != "resume_existing_run":
        raise StateError("--resume-existing-run requires dispatch_action=resume_existing_run")
    for label, supplied, expected in (
        ("migration receipt", expected_migration_sha256, dispatch.get("controller_migration_receipt_sha256")),
        ("controller package", expected_package_digest, dispatch.get("controller_package_digest")),
        ("coordinator", coordinator_id, dispatch.get("coordinator_id")),
        ("lease", lease_id, dispatch.get("lease_id")),
    ):
        if not isinstance(supplied, str) or not supplied or supplied != expected:
            raise StateError(f"resumed dispatch {label} argument mismatch")
    package_root = _package_path(base, dispatch)
    journal_path = _migration_path(base, dispatch)
    serial = _queue_state_module()
    with locked(migration_authority_lock(journal_path)):
        journal = validate_committed_migration(
            journal_path,
            expected_package_digest=expected_package_digest,
            expected_receipt_sha256=expected_migration_sha256,
            allow_queue_advance=True,
        )
        if journal.get("migration_id") != dispatch.get("controller_migration_id"):
            raise StateError("resumed dispatch migration ID mismatch")
        queue = read_json(queue_path)
        feature = _active_feature(queue, str(dispatch["feature_run_id"]))
        lease = feature.get("dispatch_lease")
        if (
            feature.get("status") != "in_progress"
            or feature.get("controller_package_digest") != expected_package_digest
            or feature.get("controller_migration_id") != journal["migration_id"]
            or not isinstance(lease, dict)
            or lease.get("coordinator_id") != coordinator_id
            or lease.get("lease_id") != lease_id
            or lease.get("resumed") is not True
            or lease.get("launch_selected") is not True
        ):
            raise StateError("resumed dispatch queue/lease identity mismatch")
        checkpoint = read_json(checkpoint_path)
        if checkpoint.get("controller_package_digest") != expected_package_digest:
            raise StateError("resumed checkpoint package digest mismatch")
        authorization = feature.get("resume_authorization")
        delta_scoped = isinstance(authorization, dict) and isinstance(
            authorization.get("delta_scope"), dict
        )
        position = (
            checkpoint.get("phase"),
            checkpoint.get("phase_detail"),
            checkpoint.get("phase_state"),
        )
        if position not in {("REVIEWING", "fix", "blocked"), ("REVIEWING", "fix", "ready")}:
            if not (delta_scoped and checkpoint.get("phase_state") == "blocked"):
                raise StateError("resumed run may reopen only blocked REVIEWING/fix")
        schema_receipts = {
            path.name: preflight_response_schema(path)
            for path in production_response_schema_paths(package_root / "implement-v13-codex")
        }
        preflight_receipt = {
            "protocol": "implement-v13-codex/post-migration-preflight/1",
            "status": "passed",
            "feature_run_id": dispatch["feature_run_id"],
            "controller_package_digest": expected_package_digest,
            "controller_migration_receipt_sha256": expected_migration_sha256,
            "schemas": schema_receipts,
            "attempt_identity_created": False,
            "child_launched": False,
        }
        atomic_write_json(
            artifact_dir / "post-migration-schema-preflight.v1.json",
            preflight_receipt,
        )
        if lease.get("launch_consumed") is not True:
            serial.atomic_mutate(
                queue_path,
                lambda current: serial.consume_resumed_launch(
                    current,
                    feature_run_id=str(dispatch["feature_run_id"]),
                    coordinator_id=str(coordinator_id),
                    lease_id=str(lease_id),
                    migration_id=str(journal["migration_id"]),
                    package_digest=str(expected_package_digest),
                ),
                expected_revision=int(queue.get("state_revision", -1)),
            )
        if checkpoint.get("phase_state") == "blocked":
            if not isinstance(authorization, dict):
                raise StateError("resumed checkpoint lacks queue authorization")
            _resume_checkpoint_with_authorization(
                checkpoint_path,
                checkpoint,
                authorization,
                Path(str(dispatch["worktree_path"])),
            )
        return preflight_receipt


def drive(
    dispatch_path: Path,
    invoke: Callable[[dict[str, Any], dict[str, Any], Path, int, str | None], tuple[dict[str, Any], str]] = _invoke_real,
    *,
    resume_existing_run: bool = False,
    expected_migration_sha256: str | None = None,
    expected_package_digest: str | None = None,
    coordinator_id: str | None = None,
    lease_id: str | None = None,
) -> dict[str, Any]:
    dispatch = read_json(dispatch_path.resolve())
    base = Path(str(dispatch.get("base_worktree_path", ""))).resolve()
    queue_path = Path(str(dispatch.get("queue_path", ""))).resolve()
    if not base.is_dir() or not queue_path.is_absolute():
        raise StateError("dispatch requires absolute base_worktree_path and queue_path")
    try:
        queue_path.relative_to(base)
    except ValueError:
        raise StateError("dispatch queue_path escapes base worktree") from None
    checkpoint_path = _absolute_under(base, dispatch.get("checkpoint_path"), "checkpoint_path")
    artifact_dir = _absolute_under(base, dispatch.get("artifact_dir"), "artifact_dir")
    transaction_path = _absolute_under(base, dispatch.get("transaction_path"), "transaction_path")
    result_path = _absolute_under(base, dispatch.get("feature_result_path"), "feature_result_path")
    dispatch["worktree_path"] = str(
        _absolute_under(base, dispatch.get("worktree_path"), "worktree_path")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    limits = _coordinator_limits(dispatch)
    if dispatch.get("dispatch_action") == "resume_existing_run":
        if not resume_existing_run:
            raise StateError("resume_existing_run dispatch requires --resume-existing-run")
        _prepare_resumed_run(
            dispatch=dispatch,
            base=base,
            queue_path=queue_path,
            checkpoint_path=checkpoint_path,
            artifact_dir=artifact_dir,
            expected_migration_sha256=expected_migration_sha256,
            expected_package_digest=expected_package_digest,
            coordinator_id=coordinator_id,
            lease_id=lease_id,
        )
    elif resume_existing_run:
        raise StateError("--resume-existing-run cannot consume a fresh or reattach dispatch")
    serial = _queue_state_module()
    turn, thread_id = _recover_coordinator_position(
        artifact_dir,
        str(dispatch["feature_run_id"]),
        _coordinator_generation(dispatch),
    )
    rollover_paths = _runtime_rollover_paths(artifact_dir)
    rollover_generation = len(rollover_paths)
    pending_rollover = read_json(rollover_paths[-1]) if (
        rollover_paths
        and thread_id is None
        and read_json(rollover_paths[-1]).get("prior_last_turn") == turn - 1
    ) else None
    last_rollover_turn = (
        int(read_json(rollover_paths[-1])["prior_last_turn"])
        if rollover_paths
        else 0
    )
    context_turns = max(0, turn - 1 - last_rollover_turn)
    context_input_tokens: list[int | None] = []
    for prior_turn in range(last_rollover_turn + 1, turn):
        receipt_path = _coordinator_receipt_path(artifact_dir, dispatch, prior_turn)
        if not receipt_path.is_file():
            raise StateError("coordinator usage recovery receipt is missing")
        usage = _receipt_provider_usage(receipt_path)
        context_input_tokens.append(
            usage["input_tokens"] if usage.get("status") == "recorded" else None
        )
    context_phase: str | None = None
    context_closure: str | None = None
    coordinator_turns_avoided = 0
    child_result: dict[str, Any] | None = None
    last_phase: tuple[Any, Any, Any, Any] | None = None
    while True:
        queue = read_json(queue_path)
        feature = _active_feature(queue, str(dispatch["feature_run_id"]))
        if feature.get("status") in {"blocked", "done"}:
            return {"status": feature["status"], "feature_run_id": dispatch["feature_run_id"]}
        if result_path.is_file():
            serial.acknowledge_feature(queue_path, transaction_path, result_path)
            continue
        checkpoint = read_json(checkpoint_path)
        if checkpoint.get("phase_state") == "blocked":
            authorization = feature.get("resume_authorization")
            if not isinstance(authorization, dict):
                raise StateError("blocked checkpoint lacks queue resume authorization")
            checkpoint = _resume_checkpoint_with_authorization(
                checkpoint_path,
                checkpoint,
                authorization,
                Path(str(dispatch["worktree_path"])),
            )
        ledger_path = artifact_dir / "review-closure-ledger.v1.json"
        current_closure = (
            str(read_json(ledger_path).get("active_closure_id", ""))
            if ledger_path.is_file()
            else ""
        )
        current_phase_name = str(checkpoint.get("phase", ""))
        if context_phase is None:
            context_phase = current_phase_name
            context_closure = current_closure
        rollover_cause: str | None = None
        if (
            limits is not None
            and thread_id is not None
            and pending_rollover is None
        ):
            if current_phase_name != context_phase:
                rollover_cause = "phase_boundary"
            elif current_closure != context_closure and (
                current_closure or context_closure
            ):
                rollover_cause = "closure_boundary"
            elif context_turns >= limits["max_turns_per_context"]:
                rollover_cause = "turn_limit"
        if rollover_cause is not None:
            rollover_generation += 1
            pending_rollover = _write_rollover_summary(
                dispatch=dispatch,
                checkpoint_path=checkpoint_path,
                artifact_dir=artifact_dir,
                prior_thread_id=thread_id,
                prior_last_turn=turn - 1,
                generation=rollover_generation,
                cause=rollover_cause,
                turns_in_context=context_turns,
                input_tokens=context_input_tokens,
                coordinator_turns_avoided=coordinator_turns_avoided,
            )
            print(json.dumps({
                "type": "coordinator_context_rolled",
                "feature_run_id": dispatch["feature_run_id"],
                "cause": rollover_cause,
                "generation": rollover_generation,
                "summary_sha256": pending_rollover["summary_sha256"],
                "turns_avoided": coordinator_turns_avoided,
            }, sort_keys=True), file=sys.stderr, flush=True)
            thread_id = None
            context_turns = 0
            context_input_tokens = []
            coordinator_turns_avoided = 0
            context_phase = current_phase_name
            context_closure = current_closure
        current_phase = (
            checkpoint.get("phase"), checkpoint.get("phase_detail"),
            checkpoint.get("phase_state"), checkpoint.get("state_revision"),
        )
        if current_phase != last_phase:
            last_phase = _emit_controller_phase(checkpoint)
        coordinator_checkpoint = dict(checkpoint)
        if child_result is not None:
            coordinator_checkpoint["CONTROLLER_CHILD_RESULT"] = child_result
        checkpoint_revision = checkpoint.get("state_revision")
        try:
            invoked = invoke(
                dispatch, coordinator_checkpoint, artifact_dir, turn, thread_id
            )
            if len(invoked) == 3:
                output, completed_thread_id, provider_usage = invoked
            else:
                output, completed_thread_id = invoked
                receipt_path = _coordinator_receipt_path(
                    artifact_dir, dispatch, turn
                )
                provider_usage = (
                    _receipt_provider_usage(receipt_path)
                    if receipt_path.is_file()
                    else {
                        "status": "unknown",
                        "input_tokens": None,
                        "cached_input_tokens": None,
                        "output_tokens": None,
                    }
                )
            if thread_id is not None and completed_thread_id != thread_id:
                raise StateError("resumed feature coordinator returned a different thread ID")
            if pending_rollover is not None:
                if completed_thread_id == pending_rollover["prior_thread_id"]:
                    raise StateError("fresh coordinator resumed the pre-rollover thread")
                _validate_rollover_ack(output, pending_rollover)
                pending_rollover = None
            thread_id = completed_thread_id
        except Exception as exc:
            token = uuid.uuid4().hex
            blocker_class = (
                "coordinator_rollover_invalid"
                if "rollover" in str(exc).lower()
                or "pre-rollover thread" in str(exc).lower()
                else "coordinator_process_failure"
            )
            return _settle_blocked(
                serial,
                queue_path,
                dispatch,
                {
                    "blocker_class": blocker_class,
                    "reason": f"feature coordinator failed: {type(exc).__name__}: {exc}",
                    "resume_condition": "resolve the coordinator failure and resume run_feature.py",
                },
                token,
            )
        completed_turn = turn
        turn += 1
        atomic_write_json(
            artifact_dir / f"coordinator-output-{completed_turn:06d}.json",
            output,
        )
        if limits is not None:
            try:
                judgment_reason = output.get("judgment_reason")
                if (
                    judgment_reason is not None
                    and judgment_reason not in COORDINATOR_JUDGMENT_REASONS
                ):
                    raise StateError(
                        "coordinator supplied a non-enumerated judgment reason"
                    )
                if output.get("status") in {"continue", "blocked", "done"} and (
                    judgment_reason not in COORDINATOR_JUDGMENT_REASONS
                ):
                    raise StateError(
                        "coordinator judgment output lacks an enumerated reason"
                    )
                input_tokens = provider_usage.get("input_tokens")
                if provider_usage.get("status") != "recorded":
                    raise StateError(
                        "coordinator provider usage is unknown under configured limits"
                    )
                if (
                    not isinstance(input_tokens, int)
                    or isinstance(input_tokens, bool)
                    or input_tokens < 0
                ):
                    raise StateError("coordinator provider input_tokens is invalid")
            except StateError as exc:
                blocker = {
                    "blocker_class": "coordinator_judgment_invalid",
                    "reason": str(exc),
                    "resume_condition": (
                        "supply provider usage and one enumerated judgment reason"
                    ),
                }
                checkpoint = read_json(checkpoint_path)
                if checkpoint.get("phase_state") != "blocked":
                    block_checkpoint(
                        checkpoint_path,
                        int(checkpoint.get("state_revision", -1)),
                        blocker,
                    )
                return _settle_blocked(
                    serial,
                    queue_path,
                    dispatch,
                    blocker,
                    uuid.uuid4().hex,
                )
            context_turns += 1
            context_input_tokens.append(input_tokens)
            window = [
                int(value)
                for value in context_input_tokens[
                    -limits["input_token_slope_window"]:
                ]
                if isinstance(value, int)
            ]
            slope = max(window) - min(window) if len(window) > 1 else 0
            if slope > limits["max_input_token_slope"]:
                rollover_generation += 1
                pending_rollover = _write_rollover_summary(
                    dispatch=dispatch,
                    checkpoint_path=checkpoint_path,
                    artifact_dir=artifact_dir,
                    prior_thread_id=thread_id,
                    prior_last_turn=completed_turn,
                    generation=rollover_generation,
                    cause="input_token_slope_limit",
                    turns_in_context=context_turns,
                    input_tokens=context_input_tokens,
                    coordinator_turns_avoided=coordinator_turns_avoided,
                )
                token = uuid.uuid4().hex
                return _settle_blocked(
                    serial,
                    queue_path,
                    dispatch,
                    {
                        "blocker_class": "coordinator_limit_blocked",
                        "reason": "configured coordinator input-token slope limit was exceeded",
                        "resume_condition": "review rollover telemetry and resume in a fresh hash-bound context",
                    },
                    token,
                )
        if output.get("status") == "invoke":
            child_result = _broker_invocation(dispatch, artifact_dir, output, completed_turn)
            closure_result = child_result.get("closure_program_result")
            if isinstance(closure_result, dict):
                metrics = closure_result.get("metrics")
                if isinstance(metrics, dict) and isinstance(
                    metrics.get("coordinator_turns_avoided"), int
                ):
                    coordinator_turns_avoided += metrics["coordinator_turns_avoided"]
                if closure_result.get("status") == "deterministic_blocked":
                    blocker = closure_result.get("blocker")
                    if not isinstance(blocker, dict):
                        blocker = {
                            "blocker_class": str(
                                closure_result.get(
                                    "failure_class", "deterministic_repair_gate"
                                )
                            ),
                            "reason": "deterministic repair routing or gate blocked",
                            "resume_condition": (
                                "correct the exact deterministic evidence and resume"
                            ),
                        }
                    checkpoint = read_json(checkpoint_path)
                    if checkpoint.get("phase_state") != "blocked":
                        block_checkpoint(
                            checkpoint_path,
                            int(checkpoint.get("state_revision", -1)),
                            blocker,
                        )
                    return _settle_blocked(
                        serial,
                        queue_path,
                        dispatch,
                        blocker,
                        uuid.uuid4().hex,
                    )
            continue
        child_result = None
        checkpoint = read_json(checkpoint_path)
        current_phase = (
            checkpoint.get("phase"), checkpoint.get("phase_detail"),
            checkpoint.get("phase_state"), checkpoint.get("state_revision"),
        )
        if current_phase != last_phase:
            last_phase = _emit_controller_phase(checkpoint)
        if output.get("status") == "blocked" and checkpoint.get("phase_state") != "blocked":
            blocker = output.get("blocker")
            token = output.get("resume_token")
            if not isinstance(blocker, dict) or not isinstance(token, str) or not token:
                raise StateError("blocked coordinator output lacks blocker or resume token")
            checkpoint = block_checkpoint(
                checkpoint_path,
                int(checkpoint.get("state_revision", -1)),
                blocker,
            )
            last_phase = _emit_controller_phase(checkpoint)
            return _settle_blocked(serial, queue_path, dispatch, blocker, token)
        if checkpoint.get("phase_state") == "blocked":
            if output.get("status") != "blocked":
                raise StateError("blocked checkpoint lacks coordinator blocker output")
            blocker = output.get("blocker")
            token = output.get("resume_token")
            if not isinstance(blocker, dict) or not isinstance(token, str) or not token:
                raise StateError("blocked coordinator output lacks blocker or resume token")
            return _settle_blocked(serial, queue_path, dispatch, blocker, token)
        if output.get("status") == "continue" and checkpoint.get("state_revision") == checkpoint_revision:
            token = uuid.uuid4().hex
            return _settle_blocked(
                serial,
                queue_path,
                dispatch,
                {
                    "blocker_class": "coordinator_no_progress",
                    "reason": "coordinator returned continue without a durable checkpoint transition",
                    "resume_condition": "correct the coordinator phase action and resume run_feature.py",
                },
                token,
            )


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(CONTROLLER_CHILD_ENV) == "1":
        raise StateError("recursive run_feature.py invocation from a coordinator child is forbidden")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dispatch", type=Path)
    parser.add_argument("--resume-existing-run", action="store_true")
    parser.add_argument("--expected-migration-sha256")
    parser.add_argument("--expected-package-digest")
    parser.add_argument("--coordinator-id")
    parser.add_argument("--lease-id")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    print(
        json.dumps(
            drive(
                args.dispatch,
                resume_existing_run=args.resume_existing_run,
                expected_migration_sha256=args.expected_migration_sha256,
                expected_package_digest=args.expected_package_digest,
                coordinator_id=args.coordinator_id,
                lease_id=args.lease_id,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
