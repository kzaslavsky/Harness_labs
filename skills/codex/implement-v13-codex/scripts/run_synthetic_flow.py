#!/usr/bin/env python3
"""Certify the production phase coordinator without repository or Git mutation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import uuid


PHASE_PROTOCOL = "implement-v13-codex/synthetic-phase/1"
RESULT_PROTOCOL = "implement-v13-codex/synthetic-feature-result/1"
RECEIPT_PROTOCOL = "implement-v13-codex/process-receipt/1"
PHASE_CATALOG: tuple[tuple[str, str], ...] = (
    ("PLANNING", "planner_prepare"),
    ("PLANNING", "planner_run"),
    ("PLANNING", "plan_validate"),
    ("PLANNING", "plan_render"),
    ("PLAN_REVIEW", "review_dispatch"),
    ("PLAN_REVIEW", "review_collect"),
    ("PLAN_REVIEW", "revise"),
    ("PLAN_REVIEW", "revised_plan_validate"),
    ("IMPLEMENTING", "strategy_validate"),
    ("IMPLEMENTING", "workers_dispatch"),
    ("IMPLEMENTING", "workers_collect"),
    ("IMPLEMENTING", "integration_validate"),
    ("RUNTIME_SMOKE", "smoke_a_run"),
    ("RUNTIME_SMOKE", "smoke_a_fix"),
    ("RUNTIME_SMOKE", "smoke_a_rerun"),
    ("REVIEWING", "review_dispatch"),
    ("REVIEWING", "ui_walk_plan"),
    ("REVIEWING", "score"),
    ("REVIEWING", "fix"),
    ("REVIEWING", "rereview"),
    ("REVIEWING", "review_finalize"),
    ("COMMITTING", "smoke_b_run"),
    ("COMMITTING", "smoke_b_fix"),
    ("COMMITTING", "ui_walk_run"),
    ("COMMITTING", "full_venv_run"),
    ("COMMITTING", "full_venv_fix"),
    ("COMMITTING", "final_gates"),
    ("COMMITTING", "feature_commit"),
    ("COMMITTING", "manifest_commit"),
    ("COMMITTING", "merge_prepare"),
    ("COMMITTING", "merge"),
    ("COMMITTING", "cleanup"),
)
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_CODEX_VERSION = re.compile(r"^codex-cli\s+\S+")
_TERMINAL_ERRORS = {"turn.failed", "error"}


class SyntheticFlowError(RuntimeError):
    """Raised when synthetic coordinator evidence is incomplete or corrupt."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _catalog_hash() -> str:
    return _sha_bytes(json.dumps(PHASE_CATALOG, separators=(",", ":")).encode())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_bytes(path: Path, value: bytes) -> None:
    """Atomically copy an opaque dispatch payload without reserialization."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyntheticFlowError(f"invalid JSON artifact type={type(exc).__name__}: {path}") from None
    if not isinstance(value, dict):
        raise SyntheticFlowError(f"JSON artifact is not an object: {path}")
    return value


def _validate_dispatch(value: Mapping[str, Any]) -> None:
    required = {
        "protocol_version", "queue_run_id", "feature_run_id", "feature_index",
        "description", "base_branch", "engine", "runner", "dispatch_action",
        "coordinator_id", "lease_id", "decision_key", "decision_record",
        "planning_inputs", "run_directives", "branch", "worktree_name",
        "worktree_path", "artifact_dir", "artifact_root", "checkpoint",
        "checkpoint_path", "transaction_path", "feature_result_path",
        "merge_receipt", "cleanup_proof", "clearance_report",
    }
    missing = sorted(required - set(value))
    if missing:
        raise SyntheticFlowError(f"production dispatch is missing: {','.join(missing)}")
    if value.get("protocol_version") != "1.0" or value.get("engine") != "v13-codex" or value.get("runner") != "implement-v13-codex":
        raise SyntheticFlowError("dispatch protocol, engine, or runner mismatch")
    if value.get("dispatch_action") != "launch":
        raise SyntheticFlowError("synthetic start requires dispatch_action launch")
    for key in (
        "queue_run_id", "feature_run_id", "description", "base_branch",
        "coordinator_id", "lease_id", "decision_key", "decision_record",
        "branch", "worktree_name", "worktree_path", "artifact_dir",
        "artifact_root", "checkpoint", "checkpoint_path", "transaction_path",
        "feature_result_path", "merge_receipt", "cleanup_proof", "clearance_report",
    ):
        if not isinstance(value.get(key), str) or not value[key]:
            raise SyntheticFlowError(f"dispatch field must be a nonempty string: {key}")
    if value.get("feature_index") is None or isinstance(value.get("feature_index"), (dict, list, bool)):
        raise SyntheticFlowError("feature_index must be a scalar queue identity")
    if not isinstance(value.get("planning_inputs"), list) or not isinstance(value.get("run_directives"), list):
        raise SyntheticFlowError("planning_inputs and run_directives must be arrays")


def _validate_identity(value: Mapping[str, Any], state: Mapping[str, Any], ordinal: int, nonce: str) -> None:
    phase, detail = PHASE_CATALOG[ordinal]
    expected = {
        "protocol": PHASE_PROTOCOL,
        "queue_run_id": state["queue_run_id"],
        "feature_run_id": state["feature_run_id"],
        "dispatch_sha256": state["dispatch_sha256"],
        "ordinal": ordinal,
        "phase": phase,
        "phase_detail": detail,
        "role": f"synthetic_{detail}",
        "nonce": nonce,
        "statement": f"this was written by the {detail} agent",
    }
    if value != expected:
        mismatches = sorted(key for key in set(value) | set(expected) if value.get(key) != expected.get(key))
        raise SyntheticFlowError(f"phase identity mismatch: {','.join(mismatches)}")
    if not _HEX_32.fullmatch(nonce):
        raise SyntheticFlowError("invalid phase nonce")


def _events(path: Path) -> tuple[str, set[str]]:
    types: set[str] = set()
    threads: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SyntheticFlowError(f"unreadable Codex JSONL type={type(exc).__name__}") from None
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise SyntheticFlowError("Codex stdout contains invalid JSONL") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise SyntheticFlowError("Codex stdout contains an invalid event")
        types.add(event["type"])
        if event["type"] == "thread.started" and isinstance(event.get("thread_id"), str):
            threads.add(event["thread_id"])
    if len(threads) != 1 or "turn.completed" not in types or types & _TERMINAL_ERRORS:
        raise SyntheticFlowError("Codex event proof is not terminal-success")
    return next(iter(threads)), types


def _observed_thread(path: Path) -> str | None:
    """Return a started thread while tolerating an in-flight final JSONL line."""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def _process_fingerprint(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    started = completed.stdout.strip()
    if completed.returncode != 0 or not started:
        raise SyntheticFlowError(f"cannot fingerprint child pid {pid}")
    return f"{pid}:{started}"


def _resolve_codex() -> tuple[Path, str]:
    candidate = shutil.which("codex")
    if candidate is None:
        raise SyntheticFlowError("codex executable is unavailable")
    executable = Path(candidate).resolve()
    completed = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=15, check=False)
    version = completed.stdout.strip()
    if completed.returncode != 0 or not _CODEX_VERSION.match(version):
        raise SyntheticFlowError("resolved executable did not identify as codex-cli")
    return executable, version


def _argv(executable: Path, workspace: Path, final: Path, model: str, effort: str) -> list[str]:
    schema = Path(__file__).resolve().parent.parent / "schemas" / "synthetic-phase.schema.json"
    return [
        str(executable), "exec", "-C", str(workspace), "--skip-git-repo-check",
        "--ignore-user-config", "--strict-config", "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", 'approval_policy="never"', "--sandbox", "workspace-write",
        "--output-schema", str(schema), "-o", str(final), "--json", "-",
    ]


def _prompt(state: Mapping[str, Any], ordinal: int, nonce: str) -> str:
    phase, detail = PHASE_CATALOG[ordinal]
    return f"""You are the synthetic {detail} role in an implement-v13-codex coordinator certification.
Do not inspect any repository or planning context. Do not perform Git or feature work.
Use apply_patch to create markers/phase-marker.json containing exactly one JSON object and a trailing newline.
Return the identical JSON object as the entire final answer, with no Markdown.

protocol: {PHASE_PROTOCOL}
queue_run_id: {state['queue_run_id']}
feature_run_id: {state['feature_run_id']}
dispatch_sha256: {state['dispatch_sha256']}
ordinal: {ordinal}
phase: {phase}
phase_detail: {detail}
role: synthetic_{detail}
nonce: {nonce}
statement: this was written by the {detail} agent

The marker must be authored by this Codex process. Do not delegate and do not create another file.
"""


def _same_process(pid: int, pgid: int) -> bool:
    try:
        return os.getpgid(pid) == pgid
    except ProcessLookupError:
        return False


def _stop_owned(process: subprocess.Popen[bytes], pgid: int) -> None:
    if process.poll() is not None or not _same_process(process.pid, pgid):
        return
    os.killpg(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if _same_process(process.pid, pgid):
            os.killpg(pgid, signal.SIGKILL)
        process.wait()


def _artifact_proof(run_dir: Path, state: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
    ordinal = receipt.get("ordinal")
    if not isinstance(ordinal, int) or not 0 <= ordinal < len(PHASE_CATALOG):
        raise SyntheticFlowError("receipt ordinal is invalid")
    if receipt.get("protocol") != RECEIPT_PROTOCOL or receipt.get("status") != "succeeded" or receipt.get("exit_code") != 0:
        raise SyntheticFlowError(f"receipt {ordinal} is not terminal-success")
    phase, detail = PHASE_CATALOG[ordinal]
    if receipt.get("phase") != phase or receipt.get("phase_detail") != detail:
        raise SyntheticFlowError("receipt phase identity mismatch")
    paths = {key: Path(str(receipt.get(key))) for key in ("final_path", "marker_path", "stdout_path")}
    for key, path in paths.items():
        try:
            path.resolve().relative_to(run_dir.resolve())
        except ValueError:
            raise SyntheticFlowError("receipt artifact escapes run directory") from None
        if not path.is_file() or receipt.get(f"{key[:-5]}_sha256") != _sha_file(path):
            raise SyntheticFlowError(f"receipt artifact hash mismatch: {key}")
    final = _read_json(paths["final_path"])
    marker = _read_json(paths["marker_path"])
    _validate_identity(final, state, ordinal, str(receipt.get("nonce")))
    if marker != final:
        raise SyntheticFlowError("agent marker and final output differ")
    thread, _ = _events(paths["stdout_path"])
    if receipt.get("thread_id") != thread:
        raise SyntheticFlowError("receipt thread ID mismatch")
    return thread


def _run_phase(run_dir: Path, state: dict[str, Any], ordinal: int, executable: Path, timeout: int) -> dict[str, Any]:
    phase, detail = PHASE_CATALOG[ordinal]
    nonce = uuid.uuid4().hex
    phase_dir = run_dir / "phases" / f"{ordinal:02d}-{detail}"
    workspace = phase_dir / "workspace"
    markers = workspace / "markers"
    phase_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace.mkdir(mode=0o700)
    markers.mkdir(mode=0o700)
    stdout = phase_dir / "stdout.jsonl"
    stderr = phase_dir / "stderr.log"
    final = phase_dir / "final.json"
    marker = markers / "phase-marker.json"
    receipt_path = run_dir / "receipts" / f"{ordinal:02d}.json"
    prompt = _prompt(state, ordinal, nonce)
    argv = _argv(executable, workspace, final, state["model"], state["reasoning"])
    receipt: dict[str, Any] = {
        "protocol": RECEIPT_PROTOCOL,
        "receipt_id": f"{state['feature_run_id']}:{phase}:{detail}:synthetic_{detail}:0:1",
        "queue_run_id": state["queue_run_id"], "feature_run_id": state["feature_run_id"],
        "phase": phase, "phase_detail": detail, "role": f"synthetic_{detail}",
        "cycle": 0, "attempt": 1, "ordinal": ordinal, "nonce": nonce,
        "status": "prepared", "state_revision": 0, "prepared_at": _now(),
        "model": state["model"], "reasoning": state["reasoning"],
        "sandbox": "workspace-write", "cwd": str(workspace), "argv": argv,
        "prompt_sha256": _sha_bytes(prompt.encode()), "stdout_path": str(stdout),
        "stderr_path": str(stderr), "final_path": str(final), "marker_path": str(marker),
    }
    _write_json(receipt_path, receipt)
    with stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            process = subprocess.Popen(argv, cwd=workspace, stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
        except OSError as exc:
            receipt.update(status="failed", completed_at=_now(), validation_errors=[f"spawn_failed:{type(exc).__name__}"], state_revision=1)
            _write_json(receipt_path, receipt)
            raise SyntheticFlowError(f"phase {ordinal} could not launch Codex") from None
        pgid = os.getpgid(process.pid)
        receipt.update(
            status="spawned_unconfirmed", pid=process.pid, process_group_id=pgid,
            process_start_fingerprint=_process_fingerprint(process.pid),
            spawned_at=_now(), state_revision=1,
        )
        _write_json(receipt_path, receipt)
        assert process.stdin is not None
        process.stdin.write(prompt.encode())
        process.stdin.close()
        deadline = time.monotonic() + timeout
        while process.poll() is None and time.monotonic() < deadline:
            thread = _observed_thread(stdout)
            if thread is not None and receipt["status"] != "running":
                receipt.update(status="running", thread_id=thread, running_at=_now(), state_revision=2)
                _write_json(receipt_path, receipt)
            time.sleep(0.1)
        if process.poll() is None:
            _stop_owned(process, pgid)
            receipt.update(status="failed", exit_code=process.returncode, completed_at=_now(), validation_errors=["wall_timeout"], state_revision=int(receipt["state_revision"]) + 1)
            _write_json(receipt_path, receipt)
            raise SyntheticFlowError(f"phase {ordinal} exceeded its wall timeout") from None
        process.wait()
    if process.returncode != 0:
        receipt.update(status="failed", exit_code=process.returncode, completed_at=_now(), validation_errors=["nonzero_exit"], state_revision=int(receipt["state_revision"]) + 1)
        _write_json(receipt_path, receipt)
        raise SyntheticFlowError(f"phase {ordinal} codex exec exited nonzero")
    try:
        final_value = _read_json(final)
        marker_value = _read_json(marker)
        _validate_identity(final_value, state, ordinal, nonce)
        if final_value != marker_value:
            raise SyntheticFlowError("agent marker and final output differ")
        thread, event_types = _events(stdout)
        if thread in state["thread_ids"]:
            raise SyntheticFlowError("fresh phase invocation reused a Codex thread")
    except SyntheticFlowError as exc:
        receipt.update(
            status="failed", exit_code=0, completed_at=_now(),
            validation_errors=[str(exc)], state_revision=int(receipt["state_revision"]) + 1,
        )
        _write_json(receipt_path, receipt)
        raise
    if receipt["status"] != "running":
        receipt.update(status="running", thread_id=thread, running_at=_now(), state_revision=int(receipt["state_revision"]) + 1)
        _write_json(receipt_path, receipt)
    receipt.update(
        status="succeeded", exit_code=0, completed_at=_now(), thread_id=thread,
        event_types=sorted(event_types), validation_errors=[], state_revision=int(receipt["state_revision"]) + 1,
        final_sha256=_sha_file(final), marker_sha256=_sha_file(marker), stdout_sha256=_sha_file(stdout),
    )
    _write_json(receipt_path, receipt)
    return receipt


def _validate_prefix(run_dir: Path, state: Mapping[str, Any]) -> list[str]:
    next_ordinal = state.get("next_ordinal")
    if not isinstance(next_ordinal, int) or not 0 <= next_ordinal <= len(PHASE_CATALOG):
        raise SyntheticFlowError("checkpoint next_ordinal is invalid")
    threads = [_artifact_proof(run_dir, state, _read_json(run_dir / "receipts" / f"{ordinal:02d}.json")) for ordinal in range(next_ordinal)]
    if len(threads) != len(set(threads)):
        raise SyntheticFlowError("completed receipts do not have distinct Codex threads")
    return threads


def _reconcile_next(run_dir: Path, state: dict[str, Any]) -> None:
    ordinal = int(state["next_ordinal"])
    receipt_path = run_dir / "receipts" / f"{ordinal:02d}.json"
    if ordinal >= len(PHASE_CATALOG) or not receipt_path.exists():
        return
    receipt = _read_json(receipt_path)
    if receipt.get("status") != "succeeded":
        raise SyntheticFlowError("ambiguous nonterminal phase exists; overwrite is forbidden")
    thread = _artifact_proof(run_dir, state, receipt)
    if thread in state["thread_ids"]:
        raise SyntheticFlowError("reconciled receipt reuses a prior thread")
    state["thread_ids"].append(thread)
    state["next_ordinal"] = ordinal + 1
    state["state_revision"] = int(state["state_revision"]) + 1
    state["updated_at"] = _now()
    _write_json(run_dir / "checkpoint.json", state)


def _assert_isolated(path: Path) -> None:
    for ancestor in (path.resolve(), *path.resolve().parents):
        if (ancestor / "AGENTS.md").exists() or (ancestor / "CLAUDE.md").exists():
            raise SyntheticFlowError("run directory is beneath repository context")


def _start(args: argparse.Namespace, executable: Path, version: str) -> tuple[Path, dict[str, Any]]:
    dispatch_source = Path(args.dispatch).expanduser().resolve()
    dispatch_bytes = dispatch_source.read_bytes()
    dispatch = _read_json(dispatch_source)
    _validate_dispatch(dispatch)
    run_dir = (Path(args.run_dir).expanduser().resolve() if args.run_dir else Path(tempfile.gettempdir()).resolve() / "codex-v13-synthetic-runs" / f"sr_{uuid.uuid4().hex}")
    if run_dir.exists():
        raise SyntheticFlowError(f"new run directory already exists: {run_dir}")
    _assert_isolated(run_dir)
    run_dir.mkdir(mode=0o700, parents=True)
    os.chmod(run_dir, 0o700)
    (run_dir / "receipts").mkdir(mode=0o700)
    (run_dir / "phases").mkdir(mode=0o700)
    dispatch_copy = run_dir / "dispatch.json"
    _write_bytes(dispatch_copy, dispatch_bytes)
    state: dict[str, Any] = {
        "protocol_version": "1.0", "runner": "implement-v13-codex",
        "engine": "v13-codex", "mode": "synthetic",
        "queue_run_id": dispatch["queue_run_id"], "feature_run_id": dispatch["feature_run_id"],
        "feature_index": dispatch["feature_index"], "task": dispatch["description"],
        "base_branch": dispatch["base_branch"], "dispatch_action": dispatch["dispatch_action"],
        "dispatch_path": str(dispatch_copy), "dispatch_sha256": _sha_file(dispatch_copy),
        "catalog_sha256": _catalog_hash(), "catalog_size": len(PHASE_CATALOG),
        "phase": PHASE_CATALOG[0][0], "phase_detail": PHASE_CATALOG[0][1],
        "phase_state": "ready", "status": "running", "next_ordinal": 0,
        "thread_ids": [], "state_revision": 0, "model": args.model, "reasoning": args.effort,
        "codex_executable": str(executable), "codex_executable_sha256": _sha_file(executable),
        "codex_version": version, "created_at": _now(), "updated_at": _now(),
    }
    _write_json(run_dir / "checkpoint.json", state)
    return run_dir, state


def _resume(args: argparse.Namespace, executable: Path, version: str) -> tuple[Path, dict[str, Any]]:
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir() or run_dir.stat().st_mode & 0o077:
        raise SyntheticFlowError("run directory is absent or not mode 0700")
    _assert_isolated(run_dir)
    state = _read_json(run_dir / "checkpoint.json")
    if state.get("runner") != "implement-v13-codex" or state.get("mode") != "synthetic" or state.get("catalog_sha256") != _catalog_hash():
        raise SyntheticFlowError("checkpoint protocol or catalog mismatch")
    if state.get("codex_executable") != str(executable) or state.get("codex_executable_sha256") != _sha_file(executable) or state.get("codex_version") != version:
        raise SyntheticFlowError("Codex executable or version changed")
    if state.get("status") == "complete":
        raise SyntheticFlowError("run is already complete; use verify")
    if state.get("status") == "blocked":
        raise SyntheticFlowError("blocked run requires audit")
    dispatch = _read_json(Path(str(state["dispatch_path"])))
    _validate_dispatch(dispatch)
    if _sha_file(Path(str(state["dispatch_path"]))) != state.get("dispatch_sha256"):
        raise SyntheticFlowError("dispatch payload changed")
    observed = _validate_prefix(run_dir, state)
    if observed != state.get("thread_ids"):
        raise SyntheticFlowError("checkpoint thread index mismatch")
    _reconcile_next(run_dir, state)
    state["status"] = "running"
    state["updated_at"] = _now()
    _write_json(run_dir / "checkpoint.json", state)
    return run_dir, state


def _verify(args: argparse.Namespace, executable: Path, version: str) -> Path:
    """Revalidate a complete run without advancing or rewriting it."""
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir() or run_dir.stat().st_mode & 0o077:
        raise SyntheticFlowError("run directory is absent or not mode 0700")
    _assert_isolated(run_dir)
    state = _read_json(run_dir / "checkpoint.json")
    if state.get("runner") != "implement-v13-codex" or state.get("mode") != "synthetic":
        raise SyntheticFlowError("checkpoint is not a production synthetic run")
    if state.get("status") != "complete" or state.get("next_ordinal") != len(PHASE_CATALOG):
        raise SyntheticFlowError("verify requires a complete run")
    if state.get("catalog_sha256") != _catalog_hash():
        raise SyntheticFlowError("phase catalog changed")
    if state.get("codex_executable") != str(executable) or state.get("codex_executable_sha256") != _sha_file(executable) or state.get("codex_version") != version:
        raise SyntheticFlowError("Codex executable or version changed")
    dispatch_path = Path(str(state["dispatch_path"]))
    _validate_dispatch(_read_json(dispatch_path))
    if _sha_file(dispatch_path) != state.get("dispatch_sha256"):
        raise SyntheticFlowError("dispatch payload changed")
    threads = _validate_prefix(run_dir, state)
    if threads != state.get("thread_ids"):
        raise SyntheticFlowError("checkpoint thread index mismatch")
    result = _read_json(run_dir / "synthetic-feature-result.json")
    expected = {
        "protocol": RESULT_PROTOCOL,
        "status": "done",
        "synthetic": True,
        "queue_run_id": state["queue_run_id"],
        "feature_run_id": state["feature_run_id"],
        "feature_index": state["feature_index"],
        "dispatch_sha256": state["dispatch_sha256"],
        "catalog_sha256": state["catalog_sha256"],
        "phase_details_validated": len(PHASE_CATALOG),
        "distinct_thread_ids": len(PHASE_CATALOG),
        "repository_mutated": False,
        "git_operations": 0,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise SyntheticFlowError(f"terminal result mismatch: {key}")
    receipt_hashes = result.get("receipt_sha256")
    if not isinstance(receipt_hashes, dict):
        raise SyntheticFlowError("terminal result has no receipt hash index")
    for ordinal in range(len(PHASE_CATALOG)):
        relative = f"receipts/{ordinal:02d}.json"
        if receipt_hashes.get(relative) != _sha_file(run_dir / relative):
            raise SyntheticFlowError(f"terminal result receipt hash mismatch: {ordinal}")
    return run_dir


def _finish(run_dir: Path, state: dict[str, Any]) -> None:
    threads = _validate_prefix(run_dir, state)
    if len(threads) != len(PHASE_CATALOG):
        raise SyntheticFlowError("terminal receipts do not cover the catalog")
    receipts = [f"receipts/{ordinal:02d}.json" for ordinal in range(len(PHASE_CATALOG))]
    result = {
        "protocol": RESULT_PROTOCOL, "status": "done", "synthetic": True,
        "queue_run_id": state["queue_run_id"], "feature_run_id": state["feature_run_id"],
        "feature_index": state["feature_index"], "task": state["task"],
        "base_branch": state["base_branch"], "dispatch_sha256": state["dispatch_sha256"],
        "catalog_sha256": state["catalog_sha256"], "phase_details_validated": len(PHASE_CATALOG),
        "distinct_thread_ids": len(set(threads)), "repository_mutated": False, "git_operations": 0,
        "receipt_paths": receipts,
        "receipt_sha256": {path: _sha_file(run_dir / path) for path in receipts},
        "completed_at": _now(),
    }
    result_path = run_dir / "synthetic-feature-result.json"
    if result_path.exists():
        existing = _read_json(result_path)
        if {k: v for k, v in existing.items() if k != "completed_at"} != {k: v for k, v in result.items() if k != "completed_at"}:
            raise SyntheticFlowError("existing terminal result conflicts with receipts")
        result["completed_at"] = existing["completed_at"]
    _write_json(result_path, result)
    state.update(status="complete", phase=PHASE_CATALOG[-1][0], phase_detail=PHASE_CATALOG[-1][1], phase_state="complete", updated_at=_now())
    _write_json(run_dir / "checkpoint.json", state)


def _run(args: argparse.Namespace) -> int:
    executable, version = _resolve_codex()
    if args.command == "verify":
        run_dir = _verify(args, executable, version)
        print(f"production synthetic flow verified: {run_dir}", flush=True)
        return 0
    run_dir, state = (_start(args, executable, version) if args.command == "start" else _resume(args, executable, version))
    print(f"synthetic run directory: {run_dir}", flush=True)
    completed = 0
    try:
        while state["next_ordinal"] < len(PHASE_CATALOG):
            if args.stop_after is not None and completed >= args.stop_after:
                state.update(status="paused", phase_state="ready", updated_at=_now())
                _write_json(run_dir / "checkpoint.json", state)
                print(f"paused at ordinal {state['next_ordinal']}", flush=True)
                return 0
            ordinal = int(state["next_ordinal"])
            receipt = _run_phase(run_dir, state, ordinal, executable, args.timeout_seconds)
            state["thread_ids"].append(receipt["thread_id"])
            state["next_ordinal"] = ordinal + 1
            if state["next_ordinal"] < len(PHASE_CATALOG):
                state["phase"], state["phase_detail"] = PHASE_CATALOG[state["next_ordinal"]]
                state["phase_state"] = "ready"
            state["state_revision"] = int(state["state_revision"]) + 1
            state["updated_at"] = _now()
            _write_json(run_dir / "checkpoint.json", state)
            completed += 1
            print(f"validated {ordinal + 1:02d}/32 {receipt['phase']}.{receipt['phase_detail']}", flush=True)
    except SyntheticFlowError as exc:
        state.update(status="blocked", phase_state="blocked", blocked_reason=str(exc), updated_at=_now())
        _write_json(run_dir / "checkpoint.json", state)
        raise
    _finish(run_dir, state)
    print("production synthetic flow certified: 32/32 details, 32 distinct Codex threads", flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--dispatch", required=True, help="synthetic dispatch JSON")
    start.add_argument("--run-dir", help="new isolated mode-0700 directory")
    start.add_argument("--model", default="gpt-5.6-sol")
    start.add_argument("--effort", choices=("low", "medium"), default="low")
    resume = sub.add_parser("resume")
    resume.add_argument("run_dir")
    verify = sub.add_parser("verify")
    verify.add_argument("run_dir")
    for command in (start, resume, verify):
        command.add_argument("--stop-after", type=int, choices=range(1, 33))
        command.add_argument("--timeout-seconds", type=int, default=900)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the production synthetic phase coordinator."""
    try:
        return _run(_parser().parse_args(argv))
    except SyntheticFlowError as exc:
        print(f"synthetic flow blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
