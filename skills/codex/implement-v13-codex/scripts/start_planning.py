#!/usr/bin/env python3
"""Create the feature worktree and launch the first planner immediately."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from controller_package import source_package_digest, verify_controller_package
from feature_state import build_inputs, initialize_checkpoint, initialize_transaction, transition
from repair_preflight import probe_role_capabilities
from run_exec import run as run_exec
from validate_artifact import MANDATORY_REVIEW_CHARGES, MANDATORY_REVIEW_LENSES
from state_io import (
    StateError,
    atomic_write_bytes,
    atomic_write_json,
    cas_update,
    read_json,
    sha256_file,
)


MAX_PLANNER_CONTEXT_BYTES = 384 * 1024
PLANNER_START_TARGET_SECONDS = 60
PLANNER_PROCESS_LEAK_SAFETY_CEILING_SECONDS = 60 * 60
EXECUTION_ENVIRONMENT_CONTEXT = (
    "EXECUTION_ENVIRONMENT=macOS BSD userland; shell=zsh\n"
    "Use rc for command exit status; never assign zsh's read-only status parameter.\n"
    "Use rg --files for file discovery; GNU find -printf is unavailable.\n"
    "Run optional rg discovery separately and handle exit 1 explicitly; do not hide failures from required assertions."
)


REQUIRED_DISPATCH_FIELDS = {
    "queue_run_id",
    "feature_run_id",
    "feature_index",
    "description",
    "base_branch",
    "base_worktree_path",
    "dispatch_action",
    "branch",
    "worktree_name",
    "worktree_path",
    "artifact_dir",
    "checkpoint_path",
    "transaction_path",
    "planning_inputs",
    "run_directives",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _base_path(base: Path, value: Any, field: str) -> Path:
    raw = Path(str(value))
    if raw.is_absolute():
        raise StateError(f"{field} must be relative to base_worktree_path")
    resolved = (base / raw).resolve()
    if not _inside(resolved, base):
        raise StateError(f"{field} escapes base_worktree_path")
    return resolved


def _git(base: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(base), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise StateError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout.strip()


def _iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _planner_launch_elapsed(started_at: dt.datetime, spawned_at: str) -> float:
    return max(0.0, (_iso(spawned_at) - started_at).total_seconds())


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt_slug(receipt_id: str) -> str:
    return receipt_id.replace(":", "-").replace("/", "-")


def _compile_planner_context(manifest: dict[str, Any]) -> tuple[str, list[str], int]:
    """Compile every resolved planning input once in manifest order."""
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not all(isinstance(item, dict) for item in inputs):
        raise StateError("planning-input manifest inputs must be an object array")
    blocks: list[str] = []
    input_ids: list[str] = []
    total_bytes = 0
    for item in inputs:
        input_id = item.get("id")
        role = item.get("role")
        expected_sha256 = item.get("sha256")
        resolved_path = Path(str(item.get("resolved_path", "")))
        if not isinstance(input_id, str) or not input_id:
            raise StateError("planning input lacks a stable id")
        if not isinstance(role, str) or not role:
            raise StateError(f"planning input lacks a role: {input_id}")
        if not resolved_path.is_file():
            raise StateError(f"planning input is missing: {input_id}")
        if expected_sha256 != sha256_file(resolved_path):
            raise StateError(f"planning input changed before prompt compilation: {input_id}")
        payload = resolved_path.read_bytes()
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateError(f"planning input is not UTF-8: {input_id}") from exc
        total_bytes += len(payload)
        if total_bytes > MAX_PLANNER_CONTEXT_BYTES:
            raise StateError(
                f"planner context exceeds {MAX_PLANNER_CONTEXT_BYTES} bytes: {total_bytes}"
            )
        metadata = json.dumps(
            {
                "id": input_id,
                "role": role,
                "resolved_path": str(resolved_path),
                "sha256": expected_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        blocks.append(
            "\n".join(
                (
                    "<<<PLANNING_INPUT>>>",
                    metadata,
                    content.rstrip("\n"),
                    f"<<<END_PLANNING_INPUT:{input_id}>>>",
                )
            )
        )
        input_ids.append(input_id)
    return "\n\n".join(blocks), input_ids, total_bytes


def _write_task_bound_schema(artifact_dir: Path, task: str) -> Path:
    source = Path(__file__).resolve().parents[1] / "schemas" / "plan.schema.json"
    schema = read_json(source)
    properties = schema.get("properties")
    if not isinstance(properties, dict) or "task" not in properties:
        raise StateError("plan schema has no task property")
    properties["task"] = {"type": "string", "const": task}
    destination = artifact_dir / "planner-run.schema.json"
    atomic_write_json(destination, schema)
    return destination


def _record_planner_block(
    checkpoint_path: Path,
    artifact_dir: Path,
    receipt_path: Path,
    error: StateError,
) -> None:
    receipt = read_json(receipt_path) if receipt_path.is_file() else None
    failure_path = artifact_dir / "planner-run.failure.json"
    failure: dict[str, Any] = {
        "protocol": "implement-v13-codex/planner-failure/1",
        "status": "blocked",
        "phase": "PLANNING",
        "phase_detail": "planner_run",
        "blocker_class": "planner_process_failure",
        "error": str(error),
        "resume_condition": "launch a new planner attempt after correcting the recorded failure",
        "resolution_evidence": [],
        "recorded_at": _utc_now(),
    }
    if receipt is not None:
        failure.update(
            receipt_path=str(receipt_path.resolve()),
            receipt_sha256=sha256_file(receipt_path),
            receipt_status=receipt.get("status"),
            validation_errors=receipt.get("validation_errors", []),
        )
    atomic_write_json(failure_path, failure)
    current = read_json(checkpoint_path)
    if (
        current.get("phase") == "PLANNING"
        and current.get("phase_detail") == "planner_run"
        and current.get("phase_state") in {"running", "validating"}
    ):
        current = transition(
            checkpoint_path,
            int(current["state_revision"]),
            "PLANNING",
            "planner_run",
            "blocked",
        )
    blocker = {
        "phase": "PLANNING",
        "phase_detail": "planner_run",
        "blocker_class": failure["blocker_class"],
        "resume_condition": failure["resume_condition"],
        "resolution_evidence": [],
        "evidence_path": str(failure_path.resolve()),
        "evidence_sha256": sha256_file(failure_path),
        "at": failure["recorded_at"],
    }
    history = list(current.get("blocked_history", []))
    history.append(blocker)
    artifacts = dict(current.get("artifacts", {}))
    artifacts["planner_failure"] = {
        "path": str(failure_path.resolve()),
        "sha256": blocker["evidence_sha256"],
    }
    cas_update(
        checkpoint_path,
        int(current["state_revision"]),
        {
            "active_blocker": blocker,
            "blocked_history": history,
            "artifacts": artifacts,
        },
    )


def start(dispatch_path: Path) -> dict[str, Any]:
    """Run the bounded startup path and return planner-launch evidence."""
    started_monotonic = time.monotonic()
    started_at = dt.datetime.now(dt.timezone.utc)
    dispatch = read_json(dispatch_path.resolve())
    missing = sorted(REQUIRED_DISPATCH_FIELDS - dispatch.keys())
    if missing:
        raise StateError(f"dispatch missing fields: {', '.join(missing)}")
    if dispatch["dispatch_action"] == "resume_existing_run":
        raise StateError(
            "start_planning rejects resume_existing_run; use the run-owned run_feature.py recovery CLI"
        )
    if dispatch["dispatch_action"] != "launch":
        raise StateError("start_planning accepts only a fresh launch dispatch")
    if not isinstance(dispatch["planning_inputs"], list):
        raise StateError("planning_inputs must be an array")
    if not isinstance(dispatch["run_directives"], list) or any(
        not isinstance(value, str) or not value.strip() for value in dispatch["run_directives"]
    ):
        raise StateError("run_directives must be an array of nonempty strings")

    base = Path(str(dispatch["base_worktree_path"]))
    if not base.is_absolute():
        raise StateError("base_worktree_path must be absolute")
    base = base.resolve()
    if not base.is_dir():
        raise StateError("base_worktree_path is not a directory")
    if dispatch.get("controller_package_digest") is not None:
        raw_package = Path(str(dispatch.get("controller_package_path", "")))
        package_root = (
            raw_package.resolve()
            if raw_package.is_absolute()
            else (base / raw_package).resolve()
        )
        try:
            package_root.relative_to(base)
        except ValueError as exc:
            raise StateError("controller package path escapes base_worktree_path") from exc
        verify_controller_package(
            package_root, str(dispatch["controller_package_digest"])
        )
        if Path(__file__).resolve().parents[2] != package_root:
            raise StateError("start_planning.py is not executing from the run-owned controller package")
    worktree = _base_path(base, dispatch["worktree_path"], "worktree_path")
    artifact_dir = _base_path(base, dispatch["artifact_dir"], "artifact_dir")
    checkpoint_path = _base_path(base, dispatch["checkpoint_path"], "checkpoint_path")
    transaction_path = _base_path(base, dispatch["transaction_path"], "transaction_path")

    current_branch = _git(base, "branch", "--show-current")
    if current_branch != dispatch["base_branch"]:
        raise StateError(
            f"base branch mismatch: expected {dispatch['base_branch']}, found {current_branch}"
        )
    base_commit = _git(base, "rev-parse", "HEAD")
    if worktree.exists():
        raise StateError(f"fresh feature worktree already exists: {worktree}")

    # This is deliberately a direct controller operation. No model or foreign CLI
    # participates in Git setup.
    _git(base, "worktree", "add", "-b", str(dispatch["branch"]), str(worktree), base_commit)

    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    persisted_dispatch = artifact_dir / "dispatch.v1.json"
    atomic_write_json(persisted_dispatch, dispatch)
    declared_path = artifact_dir / "declared-planning-inputs.v1.json"
    atomic_write_json(declared_path, dispatch["planning_inputs"])

    checkpoint = initialize_checkpoint(
        checkpoint_path,
        {
            **dispatch,
            "task": dispatch["description"],
            "base_commit": base_commit,
        },
    )
    initialize_transaction(transaction_path, checkpoint)
    checkpoint = transition(
        checkpoint_path,
        checkpoint["state_revision"],
        "PLANNING",
        "planner_prepare",
        "running",
    )
    package_digest = str(
        dispatch.get("controller_package_digest")
        or source_package_digest(Path(__file__).resolve().parents[2])
    )
    capability_manifest = probe_role_capabilities(
        repository_root=worktree,
        artifact_dir=artifact_dir,
        feature_run_id=str(dispatch["feature_run_id"]),
        controller_package_digest=package_digest,
    )
    capability_path = artifact_dir / "capability-manifest.v2.json"
    capability_sha256 = sha256_file(capability_path)
    capability_artifacts = dict(checkpoint.get("artifacts", {}))
    capability_artifacts["capability_manifest"] = {
        "path": str(capability_path.resolve()),
        "sha256": capability_sha256,
        "production_real": capability_manifest["simulation_only"] is False,
    }
    checkpoint = cas_update(
        checkpoint_path,
        int(checkpoint["state_revision"]),
        {"artifacts": capability_artifacts},
    )
    if capability_manifest["status"] != "ready":
        checkpoint = transition(
            checkpoint_path,
            checkpoint["state_revision"],
            "PLANNING",
            "planner_prepare",
            "blocked",
        )
        failure_path = artifact_dir / "capability-probe.failure.json"
        atomic_write_json(
            failure_path,
            {
                "protocol": "implement-v13-codex/capability-failure/1",
                "status": "blocked",
                "phase": "PLANNING",
                "phase_detail": "planner_prepare",
                "blocker_class": "external_capability_unavailable",
                "capability_manifest_path": str(capability_path.resolve()),
                "capability_manifest_sha256": capability_sha256,
                "resume_condition": (
                    "provide production-real host sandbox and reviewer scratch "
                    "capabilities, then create a new probe receipt"
                ),
                "recorded_at": _utc_now(),
            },
        )
        blocker = {
            "phase": "PLANNING",
            "phase_detail": "planner_prepare",
            "blocker_class": "external_capability_unavailable",
            "resume_condition": (
                "provide production-real host sandbox and reviewer scratch "
                "capabilities, then create a new probe receipt"
            ),
            "resolution_evidence": [],
            "evidence_path": str(failure_path.resolve()),
            "evidence_sha256": sha256_file(failure_path),
            "at": _utc_now(),
        }
        blocked_artifacts = dict(checkpoint.get("artifacts", {}))
        blocked_artifacts["capability_failure"] = {
            "path": str(failure_path.resolve()),
            "sha256": blocker["evidence_sha256"],
        }
        checkpoint = cas_update(
            checkpoint_path,
            int(checkpoint["state_revision"]),
            {
                "active_blocker": blocker,
                "blocked_history": [
                    *checkpoint.get("blocked_history", []),
                    blocker,
                ],
                "artifacts": blocked_artifacts,
            },
        )
        raise StateError("external_capability_unavailable")
    manifest_path = artifact_dir / "planning-inputs.v1.json"
    manifest = build_inputs(worktree, artifact_dir, declared_path, manifest_path)
    prepare_receipt = artifact_dir / "planner-prepare.receipt.json"
    atomic_write_json(
        prepare_receipt,
        {
            "status": "succeeded",
            "phase": "PLANNING",
            "phase_detail": "planner_prepare",
            "worktree": str(worktree),
            "base_commit": base_commit,
            "planning_input_ids": [item["id"] for item in manifest["inputs"]],
            "capability_manifest_path": str(capability_path.resolve()),
            "capability_manifest_sha256": capability_sha256,
        },
    )
    checkpoint = transition(
        checkpoint_path,
        checkpoint["state_revision"],
        "PLANNING",
        "planner_prepare",
        "validating",
    )
    checkpoint = transition(
        checkpoint_path,
        checkpoint["state_revision"],
        "PLANNING",
        "planner_prepare",
        "complete",
        prepare_receipt,
    )
    checkpoint = transition(
        checkpoint_path,
        checkpoint["state_revision"],
        "PLANNING",
        "planner_run",
        "ready",
    )
    checkpoint = transition(
        checkpoint_path,
        checkpoint["state_revision"],
        "PLANNING",
        "planner_run",
        "running",
    )

    receipt_id = f"{dispatch['feature_run_id']}:PLANNING:planner_run:planner:0:1"
    receipt_path = artifact_dir / f"{_receipt_slug(receipt_id)}.receipt.json"
    try:
        context_bundle, context_ids, context_bytes = _compile_planner_context(manifest)
        context_compiled_at = _utc_now()
        prompt_path = artifact_dir / "planner-run.prompt.md"
        directives = json.dumps(dispatch["run_directives"], ensure_ascii=False)
        prompt = "\n".join(
            (
                "You are the read-only repository implementation planner for this feature.",
                f"Exact task identity; copy this byte-for-byte into the plan task field: {dispatch['description']}",
                f"Planning-input manifest: {manifest_path}",
                f"Planning-input manifest SHA-256: {sha256_file(manifest_path)}",
                f"Active run directives: {directives}",
                "Every required planning input is embedded below and is already loaded. Acknowledge every input ID, role, and SHA-256 in the plan.",
                "Do not load installed skills, reread an embedded input, read CLAUDE.md, or invoke Claude tooling.",
                EXECUTION_ENVIRONMENT_CONTEXT,
                "Inspect only task-directed repository source. Do not run broad repository-wide searches or read unrelated architecture, plans, or tests.",
                "The review_lenses array must begin with these exact ID/charge pairs in this exact order:",
                *(
                    f"- {lens_id}: {charge}"
                    for lens_id, charge in zip(MANDATORY_REVIEW_LENSES, MANDATORY_REVIEW_CHARGES)
                ),
                "Use those charge strings byte-for-byte. Follow them with zero to two task-specific lenses.",
                "Set parallelization.recommended to false whenever critical_path_share is greater than 0.60.",
                "Return only a schema-valid implementation plan with an acyclic task DAG, consistent effort arithmetic, disjoint write ownership, runtime contracts, and targeted tests.",
                "",
                "BEGIN COMPILED PLANNING CONTEXT",
                context_bundle,
                "END COMPILED PLANNING CONTEXT",
                "",
            )
        )
        atomic_write_bytes(prompt_path, prompt.encode("utf-8"))
        schema_path = _write_task_bound_schema(artifact_dir, str(dispatch["description"]))
        spec_path = artifact_dir / "planner-run.spec.json"
        atomic_write_json(
            spec_path,
            {
                "receipt_id": receipt_id,
                "queue_run_id": dispatch["queue_run_id"],
                "feature_run_id": dispatch["feature_run_id"],
                "phase": "PLANNING",
                "phase_detail": "planner_run",
                "role": "planner",
                "attempt": 1,
                "cwd": str(worktree),
                "prompt_path": str(prompt_path),
                "schema_path": str(schema_path),
                "artifact_dir": str(artifact_dir),
                "model": "gpt-5.6-sol",
                "reasoning": "medium",
                "sandbox": "read-only",
                "capability_manifest_path": str(capability_path.resolve()),
                "capability_manifest_sha256": capability_sha256,
                "wall_timeout_seconds": PLANNER_PROCESS_LEAK_SAFETY_CEILING_SECONDS,
                "expected": {
                    "protocol": "implement-v13-codex/1",
                    "task": dispatch["description"],
                },
                **(
                    {
                        "controller_package_digest": dispatch[
                            "controller_package_digest"
                        ],
                        "controller_package_path": str(package_root),
                    }
                    if dispatch.get("controller_package_digest") is not None
                    else {}
                ),
            },
        )
        planner_receipt = run_exec(spec_path)
        launched_after = _planner_launch_elapsed(
            started_at, str(planner_receipt["spawned_at"])
        )
        launch_benchmark = {
            "protocol": "implement-v13-codex/planner-start-benchmark/1",
            "metric": "planner_launch_elapsed_seconds",
            "target_seconds": PLANNER_START_TARGET_SECONDS,
            "observed_seconds": launched_after,
            "met": launched_after < PLANNER_START_TARGET_SECONDS,
            "action": "continue",
            "recorded_at": _utc_now(),
        }
        atomic_write_json(
            artifact_dir / "planner-start-benchmark.v1.json", launch_benchmark
        )
        checkpoint = transition(
            checkpoint_path,
            checkpoint["state_revision"],
            "PLANNING",
            "planner_run",
            "validating",
        )
        checkpoint = transition(
            checkpoint_path,
            checkpoint["state_revision"],
            "PLANNING",
            "planner_run",
            "complete",
            receipt_path,
        )
        checkpoint = transition(
            checkpoint_path,
            checkpoint["state_revision"],
            "PLANNING",
            "plan_validate",
            "ready",
        )
    except StateError as exc:
        _record_planner_block(checkpoint_path, artifact_dir, receipt_path, exc)
        raise
    startup = {
        "protocol": "implement-v13-codex/planner-startup/1",
        "status": "succeeded",
        "worktree": str(worktree),
        "branch": dispatch["branch"],
        "base_commit": base_commit,
        "planner_receipt_id": planner_receipt["receipt_id"],
        "planner_spawned_at": planner_receipt["spawned_at"],
        "planner_launched_after_seconds": launched_after,
        "planner_start_benchmark": launch_benchmark,
        "planner_context_compiled_at": context_compiled_at,
        "planner_context_input_ids": context_ids,
        "planner_context_bytes": context_bytes,
        "capability_manifest_path": str(capability_path.resolve()),
        "capability_manifest_sha256": capability_sha256,
        "next_phase": checkpoint["phase"],
        "next_phase_detail": checkpoint["phase_detail"],
        "next_phase_state": checkpoint["phase_state"],
        "total_elapsed_seconds": time.monotonic() - started_monotonic,
    }
    atomic_write_json(artifact_dir / "planner-startup.v1.json", startup)
    return startup


def start_and_drive(
    dispatch_path: Path,
    drive_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run planner startup and feature execution as one foreground lifecycle."""
    startup = start(dispatch_path)
    dispatch = read_json(dispatch_path.resolve())
    base = Path(str(dispatch["base_worktree_path"])).resolve()
    persisted_dispatch = _base_path(base, dispatch["artifact_dir"], "artifact_dir") / "dispatch.v1.json"
    checkpoint_path = _base_path(base, dispatch["checkpoint_path"], "checkpoint_path")
    checkpoint = read_json(checkpoint_path)
    print(json.dumps({
        "type": "controller.phase",
        "controller": "start_planning.py",
        "phase_authority": "durable_checkpoint",
        "process_liveness_only": True,
        "phase": checkpoint.get("phase"),
        "phase_detail": checkpoint.get("phase_detail"),
        "phase_state": checkpoint.get("phase_state"),
        "state_revision": checkpoint.get("state_revision"),
    }, sort_keys=True), file=sys.stderr, flush=True)
    if drive_fn is None:
        from run_feature import drive as drive_fn
    return {"planner": startup, "feature": drive_fn(persisted_dispatch)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dispatch", type=Path)
    args = parser.parse_args()
    bootstrap_dispatch = read_json(args.dispatch.resolve())
    if (
        bootstrap_dispatch.get("dispatch_action") == "launch"
        and bootstrap_dispatch.get("controller_package_digest") is not None
    ):
        base = Path(str(bootstrap_dispatch.get("base_worktree_path", ""))).resolve()
        raw_package = Path(str(bootstrap_dispatch.get("controller_package_path", "")))
        package_root = (
            raw_package.resolve()
            if raw_package.is_absolute()
            else (base / raw_package).resolve()
        )
        verify_controller_package(
            package_root, str(bootstrap_dispatch["controller_package_digest"])
        )
        run_owned_entrypoint = (
            package_root / "implement-v13-codex/scripts/start_planning.py"
        ).resolve()
        if Path(__file__).resolve() != run_owned_entrypoint:
            if bootstrap_dispatch.get("controller_entrypoint") != str(
                run_owned_entrypoint
            ):
                raise StateError("fresh dispatch controller_entrypoint mismatch")
            os.execv(
                sys.executable,
                [sys.executable, str(run_owned_entrypoint), str(args.dispatch.resolve())],
            )
    try:
        result = start_and_drive(args.dispatch)
    except StateError as exc:
        settlement = None
        try:
            failed_dispatch = read_json(args.dispatch)
        except StateError:
            failed_dispatch = {}
        if failed_dispatch.get("dispatch_action") != "resume_existing_run":
            try:
                from run_feature import settle_existing_blocked

                settlement = settle_existing_blocked(args.dispatch)
            except StateError:
                pass
        print(json.dumps({"status": "failed", "error": str(exc), "queue_settlement": settlement}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
