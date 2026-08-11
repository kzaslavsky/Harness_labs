#!/usr/bin/env python3
"""Durable queue-state operations for the feature controller."""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

ENGINE = "v13-codex"
CODEX_ENGINE_FIELD = "codex_engine"
RUNNER = "implement-v13-codex"
PROTOCOL_VERSION = "1.0"
VALID_STATUSES = {"pending", "in_progress", "done", "blocked"}
TRANSACTION_READY = "feature_result_written"
TRANSACTION_ACKED = "dispatcher_ack"
WAIT_PROTOCOL = "implement-v13-codex/queue-wait-status/1"
CONTROLLER_CHILD_ENV = "IMPLEMENT_V13_RUN_FEATURE_CHILD"
JsonObject = dict[str, Any]
PACKAGE_PROTOCOL = "implement-v13-codex/controller-package-manifest/1"
PACKAGE_VERSION = "1"


class SerialStateError(RuntimeError):
    """Base class for dispatcher state errors."""


class ConcurrentUpdateError(SerialStateError):
    """Raised when a compare-and-swap revision is stale."""


class QueuePausedError(SerialStateError):
    """Raised when a paused queue is asked to mutate or dispatch."""


class QueueBlockedError(SerialStateError):
    """Raised when the first unfinished feature is blocked."""


class AuthorizationError(SerialStateError):
    """Raised when an explicit adoption or resume authorization is invalid."""


class ForeignEngineError(SerialStateError):
    """Raised when a feature belongs to another execution engine."""


class DispatchLeaseError(SerialStateError):
    """Raised when a coordinator cannot prove ownership of an active lease."""


def _reject_model_coordinator_queue_write() -> None:
    if os.environ.get(CONTROLLER_CHILD_ENV) == "1":
        raise SerialStateError(
            "model coordinator children may not mutate the feature queue; run_feature.py owns settlement"
        )


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 form."""

    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _new_run_id(prefix: str) -> str:
    """Return a collision-resistant run identifier."""

    return f"{prefix}_{uuid.uuid4().hex}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise SerialStateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path | str) -> JsonObject:
    """Read one JSON object and reject duplicate keys."""

    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_pairs_no_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise SerialStateError(f"cannot read valid JSON object at {target}: {exc}") from exc
    if not isinstance(value, dict):
        raise SerialStateError(f"expected JSON object at {target}")
    return value


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_unlocked(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, existing_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


@contextlib.contextmanager
def _locked_paths(paths: Sequence[Path]) -> Iterator[None]:
    unique = sorted({path.resolve() for path in paths}, key=str)
    handles = []
    try:
        for path in unique:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(f".{path.name}.lock")
            handle = lock_path.open("a+", encoding="utf-8")
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def queue_document_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the exact bytes used by the feature queue writer."""
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def _revision(value: Mapping[str, Any]) -> int:
    revision = value.get("state_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise SerialStateError("state_revision must be a nonnegative integer")
    return revision


def _check_revision(value: Mapping[str, Any], expected: int | None) -> int:
    current = _revision(value)
    if expected is not None and current != expected:
        raise ConcurrentUpdateError(f"expected revision {expected}, found {current}")
    return current


def atomic_mutate(
    path: Path | str,
    mutator: Callable[[JsonObject], JsonObject],
    *,
    expected_revision: int | None = None,
) -> JsonObject:
    """Lock, CAS-check, mutate, fsync, and atomically replace one JSON object."""

    target = Path(path)
    with _locked_paths([target]):
        current = read_json(target)
        revision = _check_revision(current, expected_revision)
        updated = mutator(copy.deepcopy(current))
        if not isinstance(updated, dict):
            raise SerialStateError("mutator must return a JSON object")
        updated["state_revision"] = revision + 1
        _write_json_unlocked(target, updated)
        return copy.deepcopy(updated)


def validate_queue(queue: Mapping[str, Any]) -> None:
    """Validate the compatibility envelope required by the dispatcher."""

    if not isinstance(queue.get("features"), list):
        raise SerialStateError("queue.features must be a list")
    if not isinstance(queue.get("results", []), list):
        raise SerialStateError("queue.results must be a list")
    base_branch = queue.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch:
        raise SerialStateError("queue.base_branch must be a nonempty string")
    protocol = queue.get("protocol_version")
    if protocol is not None and protocol != PROTOCOL_VERSION:
        raise SerialStateError(f"unsupported immutable protocol_version: {protocol!r}")
    dispatcher = queue.get("dispatcher")
    if dispatcher is not None and dispatcher != "implement-v13-codex":
        raise SerialStateError(f"queue belongs to immutable dispatcher {dispatcher!r}")
    identity = queue.get("queue_identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise SerialStateError("queue_identity must be an object")
        expected_identity = {
            "base_branch": base_branch,
            "protocol_version": queue.get("protocol_version"),
            "dispatcher": queue.get("dispatcher"),
            "queue_run_id": queue.get("queue_run_id"),
        }
        if any(identity.get(field) != value for field, value in expected_identity.items()):
            raise SerialStateError("immutable queue identity does not match current queue fields")
    active = 0
    seen_indexes: list[Any] = []
    for feature in queue["features"]:
        if not isinstance(feature, dict):
            raise SerialStateError("every queue feature must be an object")
        if "index" not in feature or "description" not in feature:
            raise SerialStateError("every feature requires index and description")
        if any(feature["index"] == seen for seen in seen_indexes):
            raise SerialStateError(f"duplicate feature index: {feature['index']!r}")
        seen_indexes.append(feature["index"])
        status = feature.get("status")
        if status not in VALID_STATUSES:
            raise SerialStateError(f"invalid feature status: {status!r}")
        codex_engine = feature.get(CODEX_ENGINE_FIELD)
        if codex_engine is not None and codex_engine != ENGINE:
            raise SerialStateError(
                f"unsupported {CODEX_ENGINE_FIELD}: {codex_engine!r}"
            )
        if status == "in_progress":
            active += 1
        package_digest = feature.get("controller_package_digest")
        if package_digest is not None:
            if (
                not isinstance(package_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
                or feature.get("controller_package_protocol") != PACKAGE_PROTOCOL
                or feature.get("controller_package_version") != PACKAGE_VERSION
            ):
                raise SerialStateError("feature controller package binding is invalid")
            package_path = feature.get("controller_package_path")
            if not isinstance(package_path, str) or not package_path:
                raise SerialStateError("feature controller package path is missing")
        migration_id = feature.get("controller_migration_id")
        if migration_id is not None and (
            not isinstance(migration_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", migration_id) is None
            or package_digest is None
        ):
            raise SerialStateError("feature controller migration binding is invalid")
    if active > 1:
        raise SerialStateError("queue has more than one in-progress feature")
    _revision(queue)


def _effective_engine(feature: Mapping[str, Any]) -> str:
    """Return the Codex-local engine without changing Claude's engine field."""

    codex_engine = feature.get(CODEX_ENGINE_FIELD)
    if codex_engine is not None:
        return str(codex_engine)
    return str(feature.get("engine", "legacy"))


def inspect_queue(path: Path | str) -> JsonObject:
    """Return a read-only queue summary including applicable adoption tokens."""

    queue = read_json(path)
    validate_queue(queue)
    tokens = sorted(
        {
            f"adopt-pending:{feature.get('engine', 'legacy')}:{ENGINE}"
            for feature in queue["features"]
            if feature.get("status") == "pending" and _effective_engine(feature) != ENGINE
        }
    )
    active = next((item for item in queue["features"] if item.get("status") == "in_progress"), None)
    return {
        "paused": queue.get("paused") is True,
        "pause_reason": queue.get("pause_reason"),
        "run_directives": copy.deepcopy(queue.get("run_directives", [])),
        "state_revision": _revision(queue),
        "queue_run_id": queue.get("queue_run_id"),
        "current_index": queue.get("current_index"),
        "adoption_tokens": tokens,
        "codex_enabled_indexes": [
            feature["index"]
            for feature in queue["features"]
            if _effective_engine(feature) == ENGINE
        ],
        "active_dispatch": (
            {
                "feature_index": active.get("index"),
                "feature_run_id": active.get("feature_run_id"),
                "dispatch_lease": copy.deepcopy(active.get("dispatch_lease")),
            }
            if active
            else None
        ),
    }


def _watch_artifact(base_root: Path, relative: Any, fields: Sequence[str]) -> JsonObject:
    """Return a content-addressed, allowlisted summary without exposing raw artifacts."""

    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return {"exists": False}
    target = (base_root / relative).resolve()
    try:
        target.relative_to(base_root.resolve())
    except ValueError:
        raise SerialStateError("watched artifact escapes the base checkout") from None
    if not target.is_file():
        return {"exists": False}
    raw = target.read_bytes()
    document = read_json(target)
    summary: JsonObject = {"exists": True, "sha256": hashlib.sha256(raw).hexdigest()}
    for field in fields:
        value = document.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            summary[field] = value
    return summary


def queue_watch_snapshot(path: Path | str) -> JsonObject:
    """Build the minimal state packet needed to supervise one active feature."""

    queue_path = Path(path)
    queue = read_json(queue_path)
    validate_queue(queue)
    feature = next((item for item in queue["features"] if item.get("status") != "done"), None)
    observed: JsonObject = {
        "queue_revision": _revision(queue),
        "queue_paused": queue.get("paused") is True,
        "feature": None,
    }
    terminal = feature is None
    if feature is not None:
        base_root = _base_root_for_queue(queue_path)
        checkpoint = _watch_artifact(
            base_root, feature.get("checkpoint_path"), ("phase", "phase_detail", "phase_state", "status", "state_revision")
        )
        transaction = _watch_artifact(
            base_root, feature.get("transaction_path"), ("protocol", "state", "status", "state_revision")
        )
        result = _watch_artifact(
            base_root, feature.get("feature_result_path"), ("protocol", "status")
        )
        feature_status = feature.get("status")
        terminal = bool(
            feature_status in {"blocked", "done"}
            or transaction.get("state") in {TRANSACTION_READY, TRANSACTION_ACKED}
            or result.get("exists")
        )
        observed["feature"] = {
            "index": feature.get("index"),
            "feature_run_id": feature.get("feature_run_id"),
            "status": feature_status,
            "checkpoint": checkpoint,
            "transaction": transaction,
            "result": result,
        }
    fingerprint = hashlib.sha256(
        json.dumps(observed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"fingerprint": fingerprint, "terminal": terminal, "observed": observed}


def wait_for_queue_change(
    path: Path | str,
    *,
    since: str | None,
    timeout_seconds: float,
    interval_seconds: float = 0.25,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> JsonObject:
    """Wait in-process and emit one bounded packet instead of repeated queue reads."""

    if timeout_seconds < 0 or timeout_seconds > 55:
        raise SerialStateError("wait timeout must be between 0 and 55 seconds")
    if interval_seconds <= 0 or interval_seconds > 5:
        raise SerialStateError("wait interval must be greater than 0 and at most 5 seconds")
    if since is not None and re.fullmatch(r"[a-f0-9]{64}", since) is None:
        raise SerialStateError("since must be a SHA-256 fingerprint")
    deadline = monotonic() + timeout_seconds
    while True:
        snapshot = queue_watch_snapshot(path)
        changed = since is None or snapshot["fingerprint"] != since
        if changed or snapshot["terminal"]:
            return {
                "protocol": WAIT_PROTOCOL,
                "status": "terminal" if snapshot["terminal"] else ("snapshot" if since is None else "changed"),
                **snapshot,
            }
        if monotonic() >= deadline:
            feature = snapshot["observed"].get("feature")
            return {
                "protocol": WAIT_PROTOCOL,
                "status": "timeout",
                "fingerprint": snapshot["fingerprint"],
                "terminal": False,
                "feature_index": feature.get("index") if isinstance(feature, dict) else None,
                "feature_run_id": feature.get("feature_run_id") if isinstance(feature, dict) else None,
            }
        sleeper(min(interval_seconds, max(0.0, deadline - monotonic())))


def _assert_not_paused(queue: Mapping[str, Any]) -> None:
    if queue.get("paused") is True:
        reason = queue.get("pause_reason") or "no pause reason recorded"
        raise QueuePausedError(f"queue is paused: {reason}")


def _clear_pause_for_dispatch(queue: JsonObject, *, now: str) -> None:
    """Clear one operator pause only for the current pending feature."""
    if queue.get("paused") is not True:
        raise QueuePausedError("--clear-pause requires a paused queue")
    unfinished = next((item for item in queue["features"] if item.get("status") != "done"), None)
    if unfinished is None:
        raise SerialStateError("queue is complete")
    if unfinished.get("status") != "pending":
        raise QueuePausedError("--clear-pause requires the current feature to be pending")
    history = queue.get("pause_history", [])
    if not isinstance(history, list):
        raise SerialStateError("pause_history must be an array")
    history = copy.deepcopy(history)
    history.append(
        {
            "feature_index": unfinished["index"],
            "pause_reason": queue.get("pause_reason"),
            "cleared_at": now,
            "clearance": "explicit_operator_start",
        }
    )
    queue["pause_history"] = history
    queue["paused"] = False
    queue["pause_reason"] = None


def adopt_pending_engine(
    queue: JsonObject,
    *,
    from_engine: str,
    token: str,
    now: str | None = None,
    new_id: Callable[[str], str] = _new_run_id,
) -> JsonObject:
    """Adopt matching pending entries after explicit pending-only confirmation."""

    validate_queue(queue)
    _assert_not_paused(queue)
    if not from_engine or from_engine == ENGINE:
        raise AuthorizationError("from_engine must name a foreign engine")
    expected = f"adopt-pending:{from_engine}:{ENGINE}"
    if not hmac.compare_digest(token, expected):
        raise AuthorizationError(f"invalid adoption token; expected {expected!r}")
    for feature in queue["features"]:
        if feature.get("status") in {"in_progress", "blocked"} and _effective_engine(feature) != ENGINE:
            raise ForeignEngineError("cannot adopt pending entries while a foreign feature is active")
    updated = copy.deepcopy(queue)
    _ensure_queue_identity(updated, new_id)
    changed = 0
    timestamp = now or _utc_now()
    for feature in updated["features"]:
        engine = feature.get("engine", "legacy")
        if feature.get("status") == "pending" and engine == from_engine and _effective_engine(feature) != ENGINE:
            feature[CODEX_ENGINE_FIELD] = ENGINE
            feature["runner"] = RUNNER
            feature["codex_engine_adoption"] = {
                "from": engine,
                "to": ENGINE,
                "scope": "pending_only",
                "authorization_sha256": _digest(token),
                "adopted_at": timestamp,
            }
            changed += 1
    if changed == 0:
        raise AuthorizationError(f"no pending features use engine {from_engine!r}")
    updated.setdefault("engine_adoptions", []).append(
        {
            "from": from_engine,
            "to": ENGINE,
            "scope": "pending_only",
            "authorization_sha256": _digest(token),
            "adopted_at": timestamp,
            "count": changed,
        }
    )
    return updated


def _slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug or "feature"


def _decision_values(feature: Mapping[str, Any], now: str) -> tuple[str, str]:
    key = feature.get("decision_key")
    if key is None:
        index = feature["index"]
        key = f"Q{index + 1}" if isinstance(index, int) and not isinstance(index, bool) else f"Q{index}"
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key):
        raise SerialStateError("decision_key must be a safe nonempty identifier")
    record = feature.get("decision_record")
    if record is None:
        month = now[:7]
        record = f"docs/development/decisions/{month}-{_slug(key)}-decisions.md"
    if (
        not isinstance(record, str)
        or not record.startswith("docs/development/decisions/")
        or not record.endswith("-decisions.md")
        or ".." in Path(record).parts
        or Path(record).is_absolute()
    ):
        raise SerialStateError("decision_record must be a decisions Markdown path")
    return key, record


def resolve_planning_inputs(queue: Mapping[str, Any], feature: Mapping[str, Any]) -> list[JsonObject]:
    """Merge queue and feature planning inputs by stable id without interpretation."""

    merged: list[JsonObject] = []
    positions: dict[str, int] = {}
    for owner, values in (("queue", queue.get("planning_inputs", [])), ("feature", feature.get("planning_inputs", []))):
        if not isinstance(values, list):
            raise SerialStateError(f"{owner}.planning_inputs must be a list")
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
                raise SerialStateError(f"{owner} planning input requires a nonempty string id")
            item = copy.deepcopy(value)
            if item["id"] in positions:
                if owner == "queue":
                    raise SerialStateError(f"duplicate queue planning input id: {item['id']}")
                merged[positions[item["id"]]] = item
            else:
                positions[item["id"]] = len(merged)
                merged.append(item)
    return merged


def resolve_run_directives(queue: Mapping[str, Any], feature: Mapping[str, Any]) -> list[str]:
    """Forward only directives explicitly scoped to the active launch."""

    resolved: list[str] = []
    for owner, values in (
        ("queue.active_run_directives", queue.get("active_run_directives", [])),
        ("feature.run_directives", feature.get("run_directives", [])),
    ):
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise SerialStateError(f"{owner} must be a list of nonempty strings")
        resolved.extend(values)
    return resolved


def add_planning_input(
    queue: JsonObject,
    planning_input: Mapping[str, Any],
    *,
    feature_index: Any | None = None,
) -> JsonObject:
    """Add or replace one explicit planning input before its feature starts."""
    validate_queue(queue)
    _assert_not_paused(queue)
    if any(item.get("status") == "in_progress" for item in queue["features"]):
        raise SerialStateError("planning inputs cannot change while a feature is active")
    required = {"id", "path", "role", "revision", "update_policy"}
    if not required.issubset(planning_input):
        raise SerialStateError("planning input is missing required fields")
    input_id = planning_input.get("id")
    if (
        not isinstance(input_id, str)
        or not input_id
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", input_id) is None
    ):
        raise SerialStateError("planning input id must be path-safe")
    if planning_input.get("role") not in {"governing", "background", "acceptance", "seed_plan"}:
        raise SerialStateError("planning input role is invalid")
    if planning_input.get("revision") not in {"latest_on_base", "exact_sha256", "snapshot"}:
        raise SerialStateError("planning input revision is invalid")
    if planning_input.get("update_policy") not in {"immutable", "verify_only", "reconcile_if_affected"}:
        raise SerialStateError("planning input update_policy is invalid")
    if not isinstance(planning_input.get("path"), str) or not planning_input["path"]:
        raise SerialStateError("planning input path must be nonempty")
    updated = copy.deepcopy(queue)
    if feature_index is None:
        owner = updated
    else:
        owner = next((item for item in updated["features"] if item.get("index") == feature_index), None)
        if owner is None:
            raise SerialStateError(f"feature index not found: {feature_index!r}")
        if owner.get("status") != "pending":
            raise SerialStateError("feature planning input can change only while pending")
    values = owner.setdefault("planning_inputs", [])
    if not isinstance(values, list):
        raise SerialStateError("planning_inputs must be a list")
    replacement = copy.deepcopy(dict(planning_input))
    matches = [position for position, item in enumerate(values) if isinstance(item, dict) and item.get("id") == input_id]
    if len(matches) > 1:
        raise SerialStateError(f"duplicate planning input id: {input_id}")
    if matches:
        values[matches[0]] = replacement
    else:
        values.append(replacement)
    return updated


def _ensure_queue_identity(queue: JsonObject, new_id: Callable[[str], str]) -> None:
    queue.setdefault("base_branch", "main")
    queue.setdefault("protocol_version", PROTOCOL_VERSION)
    queue.setdefault("dispatcher", "implement-v13-codex")
    if "queue_run_id" not in queue:
        queue["queue_run_id"] = new_id("qr")
    if not isinstance(queue["queue_run_id"], str) or not queue["queue_run_id"]:
        raise SerialStateError("queue_run_id must be a nonempty string")
    identity = {
        "base_branch": queue["base_branch"],
        "protocol_version": queue["protocol_version"],
        "dispatcher": queue["dispatcher"],
        "queue_run_id": queue["queue_run_id"],
    }
    if "queue_identity" in queue:
        existing = queue["queue_identity"]
        if not isinstance(existing, dict) or any(existing.get(field) != value for field, value in identity.items()):
            raise SerialStateError("immutable queue identity cannot be changed")
    else:
        queue["queue_identity"] = identity


def _feature_paths(queue: Mapping[str, Any], feature: Mapping[str, Any], now: str) -> JsonObject:
    queue_run_id = str(queue["queue_run_id"])
    feature_run_id = str(feature["feature_run_id"])
    worktree_name = f"impl-codex-{feature_run_id}"
    worktree_path = f".claude/worktrees/{worktree_name}"
    artifact_dir = f"handoff/serial-runs/{queue_run_id}/{feature_run_id}"
    return {
        "branch": worktree_name,
        "worktree_name": worktree_name,
        "worktree_path": worktree_path,
        "artifact_dir": artifact_dir,
        "artifact_root": artifact_dir,
        "checkpoint": "docs/development/current_implementation_checkpoint.json",
        "checkpoint_path": f"{worktree_path}/docs/development/current_implementation_checkpoint.json",
        "transaction_path": f"{artifact_dir}/feature-transaction.v1.json",
        "feature_result_path": f"{artifact_dir}/feature-result.v1.json",
        "merge_receipt": f"{artifact_dir}/merge-receipt.v1.json",
        "cleanup_proof": f"{artifact_dir}/cleanup-proof.v1.json",
        "clearance_report": (
            f"handoff/serial-reports/{now[:10]}-f{_slug(feature['index'])}-{feature_run_id}.md"
        ),
    }


def _apply_feature_paths(queue: Mapping[str, Any], feature: JsonObject, now: str) -> None:
    expected = _feature_paths(queue, feature, now)
    for key, value in expected.items():
        if key in feature and feature[key] != value:
            raise SerialStateError(f"immutable feature path changed: {key}")
        feature[key] = value


def _new_dispatch_lease(
    *, coordinator_id: str, lease_id: str, now: str, resumed: bool = False
) -> JsonObject:
    if not coordinator_id or not lease_id:
        raise DispatchLeaseError("coordinator_id and lease_id must be nonempty")
    return {
        "coordinator_id": coordinator_id,
        "lease_id": lease_id,
        "state": "active",
        "issued_at": now,
        "resumed": resumed,
    }


def _require_lease(feature: Mapping[str, Any], coordinator_id: str, lease_id: str | None) -> None:
    lease = feature.get("dispatch_lease")
    if not isinstance(lease, dict) or lease.get("state") != "active":
        raise DispatchLeaseError("active feature has no active dispatch lease")
    if not lease_id:
        raise DispatchLeaseError("lease_id is required to reattach to an active feature")
    if not hmac.compare_digest(str(lease.get("coordinator_id", "")), coordinator_id) or not hmac.compare_digest(
        str(lease.get("lease_id", "")), lease_id
    ):
        raise DispatchLeaseError("coordinator identity or dispatch lease does not match")


def _assert_lease_available(queue: Mapping[str, Any], feature: Mapping[str, Any], lease_id: str) -> None:
    for item in queue["features"]:
        lease = item.get("dispatch_lease")
        if item is not feature and isinstance(lease, dict) and lease.get("lease_id") == lease_id:
            raise DispatchLeaseError("lease_id is already assigned to another feature")


def _controller_module() -> Any:
    path = (
        Path(__file__).resolve().parents[2]
        / "implement-v13-codex"
        / "scripts"
        / "controller_package.py"
    )
    spec = importlib.util.spec_from_file_location("controller_package_for_serial", path)
    if spec is None or spec.loader is None:
        raise SerialStateError("controller package authority is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _bind_fresh_controller_package(
    base_worktree: Path, feature: JsonObject
) -> None:
    controller = _controller_module()
    destination = (
        base_worktree / str(feature["artifact_dir"]) / "controller-package"
    ).resolve()
    try:
        destination.relative_to(base_worktree.resolve())
    except ValueError as exc:
        raise SerialStateError("run-owned controller package path escapes base worktree") from exc
    source_parent = Path(__file__).resolve().parents[2]
    manifest = controller.copy_controller_package(source_parent, destination)
    feature.update(
        {
            "controller_package_protocol": manifest["protocol"],
            "controller_package_version": manifest["package_version"],
            "controller_package_digest": manifest["manifest_digest"],
            "controller_package_path": str(destination.relative_to(base_worktree.resolve())),
        }
    )


def _migration_evidence(
    base_root: Path, feature: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[JsonObject, JsonObject]:
    migration = evidence.get("migration")
    if not isinstance(migration, Mapping):
        raise AuthorizationError("resolution evidence requires a committed controller migration")
    raw_path = migration.get("journal_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise AuthorizationError("migration journal path is missing")
    journal_path = Path(raw_path)
    journal_path = (
        journal_path.resolve()
        if journal_path.is_absolute()
        else (base_root.resolve() / journal_path).resolve()
    )
    try:
        journal_path.relative_to(base_root.resolve())
    except ValueError as exc:
        raise AuthorizationError("migration journal escapes the base checkout") from exc
    controller = _controller_module()
    try:
        journal = controller.validate_committed_migration(
            journal_path,
            expected_package_digest=str(migration.get("controller_package_digest", "")),
            expected_receipt_sha256=str(migration.get("migration_receipt_sha256", "")),
            # Runtime authorities necessarily advance after a committed
            # migration. A later block must remain resumable under that same
            # immutable migration receipt; resume evaluates the feature while
            # its durable status is `blocked`, before installing the next
            # active lease. The controller validator still requires matching
            # run/package/migration identities and monotonic revisions.
            allow_queue_advance=feature.get("status") in {"blocked", "in_progress"},
        )
    except Exception as exc:
        raise AuthorizationError(f"controller migration is not launch-authoritative: {exc}") from exc
    expected = {
        "migration_id": feature.get("controller_migration_id"),
        "controller_package_digest": feature.get("controller_package_digest"),
    }
    if (
        journal.get("migration_id") != expected["migration_id"]
        or migration.get("migration_id") != expected["migration_id"]
        or journal.get("new_package_digest") != expected["controller_package_digest"]
    ):
        raise AuthorizationError("controller migration identity does not match the blocked feature")
    return dict(migration), journal


def migrated_queue_document(
    queue: Mapping[str, Any],
    *,
    index: Any,
    expected_revision: int,
    binding: Mapping[str, Any],
    allow_rebind: bool = False,
) -> JsonObject:
    """Construct the sole permitted migrated queue representation."""
    validate_queue(queue)
    _check_revision(queue, expected_revision)
    required = {
        "controller_package_protocol",
        "controller_package_version",
        "controller_package_digest",
        "controller_package_path",
        "controller_migration_id",
    }
    if set(binding) != required:
        raise SerialStateError("queue migration binding is incomplete or overbroad")
    updated = copy.deepcopy(dict(queue))
    matches = [item for item in updated["features"] if item.get("index") == index]
    if len(matches) != 1:
        raise SerialStateError("queue migration feature identity is ambiguous")
    feature = matches[0]
    if feature.get("status") != "blocked":
        raise SerialStateError("queue migration requires a blocked feature")
    for key, value in binding.items():
        existing = feature.get(key)
        if existing not in {None, value} and not allow_rebind:
            raise SerialStateError(f"queue migration conflicts with existing {key}")
        feature[key] = value
    updated["state_revision"] = expected_revision + 1
    validate_queue(updated)
    return updated


def cas_migrate_feature_locked(
    queue_path: Path,
    *,
    expected_revision: int,
    index: Any,
    binding: Mapping[str, Any],
    expected_sha256: str,
    allow_rebind: bool = False,
) -> JsonObject:
    """Migration-only queue CAS; caller already holds queue authority."""
    current_bytes = queue_path.read_bytes()
    if hashlib.sha256(current_bytes).hexdigest() != expected_sha256:
        raise ConcurrentUpdateError("queue migration hash witness is stale")
    current = read_json(queue_path)
    updated = migrated_queue_document(
        current,
        index=index,
        expected_revision=expected_revision,
        binding=binding,
        allow_rebind=allow_rebind,
    )
    _write_json_unlocked(queue_path, updated)
    return updated


def prepare_dispatch(
    queue: JsonObject,
    *,
    base_worktree_path: Path | str,
    coordinator_id: str,
    clear_pause: bool = False,
    lease_id: str | None = None,
    now: str | None = None,
    new_id: Callable[[str], str] = _new_run_id,
    bind_controller_package: bool = False,
) -> tuple[JsonObject, JsonObject]:
    """Prepare exactly one feature and return updated queue plus dispatch payload."""

    base_worktree = Path(base_worktree_path)
    if not base_worktree.is_absolute():
        raise SerialStateError("base_worktree_path must be absolute")
    validate_queue(queue)
    updated = copy.deepcopy(queue)
    timestamp = now or _utc_now()
    if clear_pause:
        _clear_pause_for_dispatch(updated, now=timestamp)
    else:
        _assert_not_paused(updated)
    _ensure_queue_identity(updated, new_id)
    unfinished = next((item for item in updated["features"] if item.get("status") != "done"), None)
    if unfinished is None:
        raise SerialStateError("queue is complete")
    status = unfinished["status"]
    if status == "blocked":
        raise QueueBlockedError(f"feature {unfinished['index']!r} is blocked and requires authorized resume")
    if _effective_engine(unfinished) != ENGINE:
        raise ForeignEngineError(
            f"feature {unfinished['index']!r} uses {unfinished.get('engine', 'legacy')!r}; explicit pending adoption is required"
        )
    dispatch_action = "reattach"
    resumed_launch = False
    if status == "pending":
        unfinished["status"] = "in_progress"
        unfinished.setdefault("feature_run_id", new_id("fr"))
        unfinished.setdefault("attempt", 1)
        unfinished.setdefault("resume_count", 0)
        unfinished.setdefault("runner", RUNNER)
        key, record = _decision_values(unfinished, timestamp)
        unfinished["decision_key"] = key
        unfinished["decision_record"] = record
        unfinished.setdefault("started_at", timestamp)
        lease_value = lease_id or new_id("lease")
        _assert_lease_available(updated, unfinished, lease_value)
        unfinished["dispatch_lease"] = _new_dispatch_lease(
            coordinator_id=coordinator_id, lease_id=lease_value, now=timestamp
        )
        dispatch_action = "launch"
    elif status != "in_progress":
        raise SerialStateError(f"cannot dispatch status {status!r}")
    else:
        _require_lease(unfinished, coordinator_id, lease_id)
        if unfinished["dispatch_lease"].pop("launch_authorized", False):
            resumed_launch = (
                unfinished["dispatch_lease"].get("resumed") is True
                and isinstance(unfinished.get("controller_migration"), dict)
            )
            dispatch_action = "resume_existing_run" if resumed_launch else "launch"
            if resumed_launch:
                if unfinished["dispatch_lease"].get("launch_selected") is True:
                    raise DispatchLeaseError("resumed launch was already selected")
                unfinished["dispatch_lease"]["launch_selected"] = True
                unfinished["dispatch_lease"]["launch_selected_at"] = timestamp
    if unfinished.get("runner") != RUNNER:
        raise ForeignEngineError("active feature runner does not match implement-v13-codex")
    if not unfinished.get("feature_run_id"):
        raise SerialStateError("active feature is missing feature_run_id")
    key, record = _decision_values(unfinished, timestamp)
    unfinished["decision_key"] = key
    unfinished["decision_record"] = record
    _apply_feature_paths(updated, unfinished, str(unfinished.get("started_at", timestamp)))
    if status == "pending" and bind_controller_package:
        _bind_fresh_controller_package(base_worktree.resolve(), unfinished)
    if resumed_launch:
        migration = unfinished.get("controller_migration")
        if not isinstance(migration, dict):
            raise AuthorizationError("resumed launch lacks committed migration binding")
        _migration_evidence(
            base_worktree.resolve(),
            unfinished,
            {"migration": migration},
        )
    updated["current_index"] = unfinished["index"]
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "queue_run_id": updated["queue_run_id"],
        "feature_run_id": unfinished["feature_run_id"],
        "feature_index": unfinished["index"],
        "description": unfinished["description"],
        "base_branch": updated["base_branch"],
        "base_worktree_path": str(base_worktree),
        "engine": ENGINE,
        "runner": RUNNER,
        "dispatch_action": dispatch_action,
        "coordinator_id": unfinished["dispatch_lease"]["coordinator_id"],
        "lease_id": unfinished["dispatch_lease"]["lease_id"],
        "decision_key": key,
        "decision_record": record,
        "planning_inputs": resolve_planning_inputs(updated, unfinished),
        "run_directives": resolve_run_directives(updated, unfinished),
    }
    for package_field in (
        "controller_package_protocol",
        "controller_package_version",
        "controller_package_digest",
        "controller_package_path",
        "controller_migration_id",
    ):
        if package_field in unfinished:
            payload[package_field] = unfinished[package_field]
    if unfinished.get("controller_package_path"):
        run_package = (
            base_worktree.resolve() / str(unfinished["controller_package_path"])
        ).resolve()
        entrypoint = (
            "run_feature.py"
            if resumed_launch
            else "start_planning.py"
        )
        payload["controller_entrypoint"] = str(
            run_package / "implement-v13-codex" / "scripts" / entrypoint
        )
    if resumed_launch:
        migration = unfinished["controller_migration"]
        payload.update(
            {
                "controller_migration_journal_path": migration["journal_path"],
                "controller_migration_receipt_sha256": migration[
                    "migration_receipt_sha256"
                ],
                "coordinator_rollover_path": migration["rollover_summary_path"],
            }
        )
    for key_name in (
        "branch",
        "worktree_name",
        "worktree_path",
        "artifact_dir",
        "artifact_root",
        "checkpoint",
        "checkpoint_path",
        "transaction_path",
        "feature_result_path",
        "merge_receipt",
        "cleanup_proof",
        "clearance_report",
    ):
        payload[key_name] = unfinished[key_name]
    return updated, payload


def consume_resumed_launch(
    queue: JsonObject,
    *,
    feature_run_id: str,
    coordinator_id: str,
    lease_id: str,
    migration_id: str,
    package_digest: str,
    now: str | None = None,
) -> JsonObject:
    """Consume exactly one selected resumed-run launch lease."""
    validate_queue(queue)
    updated = copy.deepcopy(queue)
    matches = [
        item
        for item in updated["features"]
        if item.get("feature_run_id") == feature_run_id
    ]
    if len(matches) != 1:
        raise DispatchLeaseError("resumed launch feature identity is ambiguous")
    feature = matches[0]
    if feature.get("status") != "in_progress":
        raise DispatchLeaseError("resumed launch feature is not in progress")
    _require_lease(feature, coordinator_id, lease_id)
    lease = feature["dispatch_lease"]
    if (
        lease.get("resumed") is not True
        or lease.get("launch_selected") is not True
        or lease.get("launch_consumed") is True
    ):
        raise DispatchLeaseError("resumed launch lease is absent or already consumed")
    if (
        feature.get("controller_migration_id") != migration_id
        or feature.get("controller_package_digest") != package_digest
    ):
        raise DispatchLeaseError("resumed launch controller identity mismatch")
    lease["launch_consumed"] = True
    lease["launch_consumed_at"] = now or _utc_now()
    return updated


def block_feature(
    queue: JsonObject,
    *,
    index: Any,
    coordinator_id: str,
    lease_id: str,
    blocker: Mapping[str, Any],
    resume_token: str,
    now: str | None = None,
) -> JsonObject:
    """Park the active first feature and retain a token-gated resume contract."""

    _reject_model_coordinator_queue_write()
    validate_queue(queue)
    _assert_not_paused(queue)
    unfinished = next((item for item in queue["features"] if item.get("status") != "done"), None)
    if unfinished is None or unfinished.get("index") != index:
        raise SerialStateError("only the first unfinished feature may be blocked")
    if unfinished.get("status") != "in_progress":
        raise SerialStateError("only an in-progress feature may be blocked")
    _require_lease(unfinished, coordinator_id, lease_id)
    required = ("blocker_class", "reason", "resume_condition")
    if not isinstance(blocker, Mapping) or any(
        not isinstance(blocker.get(field), str) or not str(blocker[field]).strip() for field in required
    ):
        raise SerialStateError("blocker requires blocker_class, reason, and resume_condition")
    if not resume_token:
        raise AuthorizationError("a nonempty resume token is required")
    updated = copy.deepcopy(queue)
    target = next(item for item in updated["features"] if item["index"] == index)
    timestamp = now or _utc_now()
    target["status"] = "blocked"
    target["blocker_class"] = blocker["blocker_class"]
    target["block_reason"] = blocker["reason"]
    target["resume_condition"] = blocker["resume_condition"]
    target["blocked_at"] = timestamp
    target["resume_token_sha256"] = _digest(resume_token)
    target["blocker"] = copy.deepcopy(dict(blocker))
    target["dispatch_lease"]["state"] = "blocked"
    target["dispatch_lease"]["released_at"] = timestamp
    updated["current_index"] = target["index"]
    return updated


def _expected_resume_identity(queue: Mapping[str, Any], feature: Mapping[str, Any]) -> JsonObject:
    identity = {
        "queue_run_id": queue.get("queue_run_id"),
        "feature_run_id": feature.get("feature_run_id"),
        "feature_index": feature.get("index"),
        "base_branch": queue.get("base_branch"),
        "worktree_path": feature.get("worktree_path"),
        "checkpoint_path": feature.get("checkpoint_path"),
        "transaction_path": feature.get("transaction_path"),
    }
    if feature.get("controller_package_digest") is not None:
        identity["controller_package_digest"] = feature.get("controller_package_digest")
    if feature.get("controller_migration_id") is not None:
        identity["controller_migration_id"] = feature.get("controller_migration_id")
    return identity


def _validate_resume_artifacts(
    base_root: Path,
    queue: Mapping[str, Any],
    feature: Mapping[str, Any],
    resolution_evidence: Mapping[str, Any],
) -> None:
    artifacts = resolution_evidence.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise AuthorizationError("resolution evidence requires checkpoint and transaction artifacts")
    expected_identity = {
        "queue_run_id": queue.get("queue_run_id"),
        "feature_run_id": feature.get("feature_run_id"),
        "feature_index": feature.get("index"),
        "base_branch": queue.get("base_branch"),
    }
    for name, path_field in (("checkpoint", "checkpoint_path"), ("transaction", "transaction_path")):
        path = _resolve_local_artifact(base_root, feature.get(path_field), path_field)
        supplied_hash = artifacts.get(f"{name}_sha256")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if not isinstance(supplied_hash, str) or not hmac.compare_digest(supplied_hash, actual_hash):
            raise AuthorizationError(f"{name} identity hash does not match surviving artifact")
        document = read_json(path)
        for field, expected in expected_identity.items():
            if document.get(field) != expected:
                raise AuthorizationError(f"{name} identity field does not match: {field}")
        if document.get("controller_package_digest") != feature.get("controller_package_digest"):
            raise AuthorizationError(f"{name} controller package digest does not match")


DELTA_SCOPE_PROTOCOL = "implement-v13-codex/delta-resume-scope/1"


def _validate_delta_scope_binding(
    base_root: Path,
    feature: Mapping[str, Any],
    delta_scope: Mapping[str, Any],
) -> None:
    if not isinstance(delta_scope, Mapping) or delta_scope.get("protocol") != DELTA_SCOPE_PROTOCOL:
        raise AuthorizationError("delta scope requires the delta-resume-scope protocol")
    if delta_scope.get("feature_run_id") != feature.get("feature_run_id"):
        raise AuthorizationError("delta scope belongs to another feature run")
    raw_path = delta_scope.get("ledger_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise AuthorizationError("delta scope requires the frozen ledger path")
    ledger_path = Path(raw_path)
    if not ledger_path.is_absolute():
        ledger_path = base_root / ledger_path
    ledger_path = ledger_path.resolve()
    try:
        ledger_path.relative_to(base_root.resolve())
    except ValueError as exc:
        raise AuthorizationError("delta scope ledger escapes the base checkout") from exc
    if not ledger_path.is_file():
        raise AuthorizationError("delta scope frozen ledger is missing")
    supplied_hash = delta_scope.get("ledger_sha256")
    actual_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    if not isinstance(supplied_hash, str) or not hmac.compare_digest(supplied_hash, actual_hash):
        raise AuthorizationError("delta scope ledger hash does not match the frozen review ledger")


def resume_blocked_feature(
    queue: JsonObject,
    *,
    index: Any,
    token: str,
    resolution_evidence: Mapping[str, Any],
    coordinator_id: str,
    lease_id: str,
    base_root: Path,
    now: str | None = None,
    require_controller_migration: bool = False,
    delta_scope: Mapping[str, Any] | None = None,
) -> JsonObject:
    """Resume one blocked feature after matching authorization and evidence."""

    validate_queue(queue)
    _assert_not_paused(queue)
    updated = copy.deepcopy(queue)
    _ensure_queue_identity(updated, _new_run_id)
    feature = next((item for item in updated["features"] if item["index"] == index), None)
    if feature is None:
        raise SerialStateError(f"feature index not found: {index!r}")
    if feature.get("status") != "blocked":
        raise SerialStateError("only a blocked feature can be resumed")
    first_unfinished = next((item for item in updated["features"] if item.get("status") != "done"), None)
    if first_unfinished is not feature:
        raise SerialStateError("only the first unfinished feature may be resumed")
    if any(item.get("status") == "in_progress" for item in updated["features"]):
        raise SerialStateError("cannot resume while another feature is in progress")
    if _effective_engine(feature) != ENGINE or feature.get("runner") != RUNNER:
        raise ForeignEngineError("blocked feature belongs to a foreign runner")
    expected_hash = feature.get("resume_token_sha256")
    if not isinstance(expected_hash, str) or not hmac.compare_digest(_digest(token), expected_hash):
        raise AuthorizationError("resume token does not match blocked feature")
    if not isinstance(resolution_evidence, Mapping) or not resolution_evidence:
        raise AuthorizationError("nonempty resolution evidence is required")
    identity = resolution_evidence.get("identity")
    expected_identity = _expected_resume_identity(updated, feature)
    if not isinstance(identity, Mapping) or dict(identity) != expected_identity:
        raise AuthorizationError("resolution evidence identity does not match blocked feature")
    _validate_resume_artifacts(base_root, updated, feature, resolution_evidence)
    if delta_scope is not None:
        _validate_delta_scope_binding(base_root, feature, delta_scope)
    if require_controller_migration or feature.get("controller_package_digest") is not None:
        migration, journal = _migration_evidence(
            base_root, feature, resolution_evidence
        )
    else:
        migration, journal = {}, {}
    target = feature
    timestamp = now or _utc_now()
    history = {
        key: copy.deepcopy(target[key])
        for key in (
            "blocker_class",
            "block_reason",
            "resume_condition",
            "blocked_at",
            "blocker",
            "dispatch_lease",
        )
        if key in target
    }
    history["resumed_at"] = timestamp
    target.setdefault("blocked_history", []).append(history)
    target["resume_authorization"] = {
        "authorization_sha256": _digest(token),
        "resolution_evidence": copy.deepcopy(dict(resolution_evidence)),
        "authorized_at": timestamp,
    }
    if delta_scope is not None:
        target["resume_authorization"]["delta_scope"] = copy.deepcopy(dict(delta_scope))
    if migration:
        target["controller_migration"] = {
            **migration,
            "rollover_summary_path": journal["authorities"]["rollover"]["path"],
        }
    target["status"] = "in_progress"
    target["attempt"] = int(target.get("attempt", 1)) + 1
    target["resume_count"] = int(target.get("resume_count", 0)) + 1
    _assert_lease_available(updated, target, lease_id)
    target["dispatch_lease"] = _new_dispatch_lease(
        coordinator_id=coordinator_id, lease_id=lease_id, now=timestamp, resumed=True
    )
    target["dispatch_lease"]["launch_authorized"] = True
    for key in (
        "blocker_class",
        "block_reason",
        "resume_condition",
        "resume_token_sha256",
        "blocked_at",
        "blocker",
    ):
        target.pop(key, None)
    updated["current_index"] = target["index"]
    return updated


def _find_feature(queue: Mapping[str, Any], index: Any) -> Mapping[str, Any]:
    matches = [item for item in queue["features"] if item["index"] == index]
    if len(matches) != 1:
        raise SerialStateError(f"expected exactly one feature with index {index!r}")
    return matches[0]


def _base_root_for_queue(queue_path: Path) -> Path:
    resolved = queue_path.resolve()
    if resolved.parent.name == "development" and resolved.parent.parent.name == "docs":
        return resolved.parents[2]
    return resolved.parent


def _git_output(base: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(base), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise SerialStateError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout.strip()


def _assert_base_worktree_identity(
    queue_path: Path,
    base_worktree_path: Path,
    queue: Mapping[str, Any],
) -> None:
    """Reject a wrong or detached base before dispatch mutates the queue."""
    base = base_worktree_path.resolve()
    if not base.is_dir():
        raise SerialStateError("base_worktree_path is not a directory")
    if _base_root_for_queue(queue_path) != base:
        raise SerialStateError("queue path is not inside base_worktree_path")
    expected_branch = queue.get("base_branch")
    if not isinstance(expected_branch, str) or not expected_branch:
        raise SerialStateError("queue base_branch is missing")
    branch = _git_output(base, "branch", "--show-current")
    if not branch:
        raise SerialStateError(f"base worktree is detached; expected branch {expected_branch}")
    if branch != expected_branch:
        raise SerialStateError(
            f"base branch mismatch: expected {expected_branch}, found {branch}"
        )
    _git_output(base, "rev-parse", "HEAD")


def _resolve_local_artifact(base_root: Path, relative: Any, field: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise SerialStateError(f"{field} must be a nonempty base-relative path")
    candidate = (base_root / relative).resolve()
    try:
        candidate.relative_to(base_root.resolve())
    except ValueError as exc:
        raise SerialStateError(f"{field} escapes the base checkout") from exc
    if not candidate.is_file():
        raise SerialStateError(f"referenced {field} is missing or not a file: {relative}")
    return candidate


def _assert_expected_path(actual: Path, base_root: Path, expected_relative: Any, field: str) -> None:
    expected = (base_root / str(expected_relative)).resolve()
    if actual.resolve() != expected:
        raise SerialStateError(f"{field} path does not match dispatched metadata")


def _validate_transaction_history(transaction: Mapping[str, Any]) -> None:
    history = transaction.get("history")
    if not isinstance(history, list):
        raise SerialStateError("transaction history must be a list")
    states = [entry.get("state") for entry in history if isinstance(entry, dict)]
    required = [
        "prepared",
        "feature_committed",
        "manifest_committed",
        "merge_prepared",
        "merged",
        "cleanup_complete",
        "feature_result_written",
    ]
    if states[: len(required)] != required:
        raise SerialStateError("transaction history lacks the required ordered terminal states")


def _validate_local_proofs(
    base_root: Path,
    feature: Mapping[str, Any],
    transaction: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    expected_fields = ("merge_receipt", "clearance_report", "cleanup_proof")
    for field in expected_fields:
        if result.get(field) != feature.get(field):
            raise SerialStateError(f"feature result {field} does not match dispatched path")
    artifacts = {
        "manifest": _resolve_local_artifact(base_root, result.get("manifest"), "manifest"),
        "merge_receipt": _resolve_local_artifact(
            base_root, result.get("merge_receipt"), "merge_receipt"
        ),
        "clearance_report": _resolve_local_artifact(
            base_root, result.get("clearance_report"), "clearance_report"
        ),
        "cleanup_proof": _resolve_local_artifact(
            base_root, result.get("cleanup_proof"), "cleanup_proof"
        ),
    }
    manifest = str(result["manifest"])
    if not manifest.startswith("docs/development/runs/") or not manifest.endswith(".md"):
        raise SerialStateError("manifest must be a run manifest under docs/development/runs")
    for field, path in artifacts.items():
        expected_hash = transaction.get(f"{field}_sha256")
        if expected_hash is not None:
            if not isinstance(expected_hash, str) or not hmac.compare_digest(
                hashlib.sha256(path.read_bytes()).hexdigest(), expected_hash
            ):
                raise SerialStateError(f"{field} hash does not match transaction proof")
    merge_receipt = read_json(artifacts["merge_receipt"])
    merge_expected = {
        "protocol": "implement-v13-codex/merge-receipt/1",
        "queue_run_id": result.get("queue_run_id"),
        "feature_run_id": result.get("feature_run_id"),
        "base_head_after": result.get("base_head"),
        "manifest": result.get("manifest"),
        "ancestry_verified": True,
    }
    if any(merge_receipt.get(field) != value for field, value in merge_expected.items()):
        raise SerialStateError("merge receipt identity or ancestry proof is invalid")
    actual_manifest_hash = hashlib.sha256(artifacts["manifest"].read_bytes()).hexdigest()
    if merge_receipt.get("manifest_sha256") != actual_manifest_hash:
        raise SerialStateError("merge receipt manifest hash is invalid")
    if not isinstance(merge_receipt.get("cleanup"), dict) or not isinstance(
        merge_receipt.get("guards"), dict
    ):
        raise SerialStateError("merge receipt cleanup and guards proofs must be objects")


def _validate_ack(
    queue: Mapping[str, Any],
    transaction: Mapping[str, Any],
    result: Mapping[str, Any],
    base_root: Path,
) -> Mapping[str, Any]:
    validate_queue(queue)
    _assert_not_paused(queue)
    if result.get("protocol") != "implement-v13-codex/feature-result/1" or result.get("status") != "done":
        raise SerialStateError("feature result protocol or status is invalid")
    if transaction.get("protocol") != "implement-v13-codex/feature-transaction/1":
        raise SerialStateError("feature transaction protocol is invalid")
    feature = _find_feature(queue, result.get("feature_index"))
    expected_queue_run_id = queue.get("queue_run_id")
    if (
        not expected_queue_run_id
        or transaction.get("queue_run_id") != expected_queue_run_id
        or result.get("queue_run_id") != expected_queue_run_id
    ):
        raise SerialStateError("acknowledgment queue_run_id mismatch")
    expected_feature_run_id = feature.get("feature_run_id")
    if (
        not expected_feature_run_id
        or transaction.get("feature_run_id") != expected_feature_run_id
        or result.get("feature_run_id") != expected_feature_run_id
    ):
        raise SerialStateError("acknowledgment feature_run_id mismatch")
    if transaction.get("feature_index") != feature.get("index"):
        raise SerialStateError("acknowledgment feature_index mismatch")
    if transaction.get("base_branch") != queue.get("base_branch"):
        raise SerialStateError("acknowledgment base_branch mismatch")
    if feature.get("status") not in {"in_progress", "done"}:
        raise SerialStateError("feature must be in_progress or idempotently done")
    if transaction.get("state") not in {TRANSACTION_READY, TRANSACTION_ACKED}:
        raise SerialStateError("transaction is not ready for dispatcher acknowledgment")
    _validate_transaction_history(transaction)
    required_result_fields = (
        "completed_at",
        "manifest",
        "merge_receipt",
        "clearance_report",
        "base_head",
        "cleanup_proof",
    )
    if any(not isinstance(result.get(field), str) or not result[field] for field in required_result_fields):
        raise SerialStateError("feature result is missing required proof fields")
    if transaction.get("base_head") is not None and transaction.get("base_head") != result.get("base_head"):
        raise SerialStateError("feature result base_head does not match transaction proof")
    _validate_local_proofs(base_root, feature, transaction, result)
    return feature


def acknowledge_feature(
    queue_path: Path | str,
    transaction_path: Path | str,
    result_path: Path | str,
    *,
    expected_queue_revision: int | None = None,
    expected_transaction_revision: int | None = None,
    now: str | None = None,
) -> tuple[JsonObject, JsonObject]:
    """Atomically update each acknowledgment file with crash-safe idempotency."""

    _reject_model_coordinator_queue_write()
    queue_target = Path(queue_path)
    transaction_target = Path(transaction_path)
    result_target = Path(result_path)
    with _locked_paths([queue_target, transaction_target, result_target]):
        queue = read_json(queue_target)
        transaction = read_json(transaction_target)
        result_bytes = result_target.read_bytes()
        result = read_json(result_target)
        queue_revision = _check_revision(queue, expected_queue_revision)
        transaction_revision = _check_revision(transaction, expected_transaction_revision)
        base_root = _base_root_for_queue(queue_target)
        feature_for_path = _find_feature(queue, result.get("feature_index"))
        _assert_expected_path(
            transaction_target, base_root, feature_for_path.get("transaction_path"), "transaction"
        )
        _assert_expected_path(
            result_target, base_root, feature_for_path.get("feature_result_path"), "feature result"
        )
        feature = _validate_ack(queue, transaction, result, base_root)
        expected_result_hash = transaction.get("feature_result_sha256")
        if expected_result_hash is not None and (
            not isinstance(expected_result_hash, str)
            or not hmac.compare_digest(hashlib.sha256(result_bytes).hexdigest(), expected_result_hash)
        ):
            raise SerialStateError("feature result hash does not match transaction proof")
        timestamp = now or _utc_now()
        updated_queue = copy.deepcopy(queue)
        target = next(item for item in updated_queue["features"] if item["index"] == feature["index"])
        matching_results = [
            item
            for item in updated_queue.get("results", [])
            if isinstance(item, dict) and item.get("feature_run_id") == result["feature_run_id"]
        ]
        if target.get("status") == "done" and not matching_results:
            raise SerialStateError("done feature is missing its matching queue result")
        expected_queue_result = copy.deepcopy(result)
        expected_queue_result["status"] = "done"
        if matching_results and (
            len(matching_results) != 1
            or any(matching_results[0].get(key) != value for key, value in expected_queue_result.items())
        ):
            raise SerialStateError("existing queue result does not match immutable feature result")
        if target.get("status") != "done":
            target["status"] = "done"
            target["completed_at"] = result.get("completed_at", timestamp)
            updated_queue.setdefault("results", []).append(expected_queue_result)
        lease = target.get("dispatch_lease")
        if isinstance(lease, dict) and lease.get("state") != "complete":
            lease["state"] = "complete"
            lease["completed_at"] = result.get("completed_at", timestamp)
        updated_queue["state_revision"] = queue_revision + (0 if updated_queue == queue else 1)
        if updated_queue != queue:
            _write_json_unlocked(queue_target, updated_queue)

        updated_transaction = copy.deepcopy(transaction)
        if transaction.get("state") != TRANSACTION_ACKED:
            updated_transaction["state"] = TRANSACTION_ACKED
            updated_transaction["dispatcher_ack"] = {
                "dispatcher": "implement-v13-codex",
                "acknowledged_at": timestamp,
                "queue_state_revision": updated_queue["state_revision"],
                "feature_result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            }
            updated_transaction.setdefault("history", []).append(
                {
                    "state": TRANSACTION_ACKED,
                    "at": timestamp,
                    "evidence": copy.deepcopy(updated_transaction["dispatcher_ack"]),
                }
            )
            updated_transaction["state_revision"] = transaction_revision + 1
            _write_json_unlocked(transaction_target, updated_transaction)
        return copy.deepcopy(updated_queue), copy.deepcopy(updated_transaction)


def _parse_index(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _command_inspect(args: argparse.Namespace) -> JsonObject:
    return inspect_queue(args.queue)


def _command_wait(args: argparse.Namespace) -> JsonObject:
    return wait_for_queue_change(
        args.queue,
        since=args.since,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
    )


def _command_adopt(args: argparse.Namespace) -> JsonObject:
    return atomic_mutate(
        args.queue,
        lambda queue: adopt_pending_engine(queue, from_engine=args.from_engine, token=args.token),
        expected_revision=args.expected_revision,
    )


def _command_add_input(args: argparse.Namespace) -> JsonObject:
    planning_input = read_json(args.input)
    index = _parse_index(args.index) if args.index is not None else None
    return atomic_mutate(
        args.queue,
        lambda queue: add_planning_input(queue, planning_input, feature_index=index),
        expected_revision=args.expected_revision,
    )


def _command_dispatch(args: argparse.Namespace) -> JsonObject:
    payload: JsonObject = {}

    def mutate(queue: JsonObject) -> JsonObject:
        nonlocal payload
        _assert_base_worktree_identity(args.queue, args.base_worktree_path, queue)
        updated, payload = prepare_dispatch(
            queue,
            base_worktree_path=args.base_worktree_path,
            coordinator_id=args.coordinator_id,
            clear_pause=args.clear_pause,
            lease_id=args.lease_id,
            bind_controller_package=True,
        )
        return updated

    observed = read_json(args.queue)
    unfinished = next(
        (item for item in observed["features"] if item.get("status") != "done"), None
    )
    migration = unfinished.get("controller_migration") if isinstance(unfinished, dict) else None
    lock_paths: list[Path] = []
    if isinstance(migration, dict) and isinstance(migration.get("journal_path"), str):
        journal = Path(migration["journal_path"])
        if not journal.is_absolute():
            journal = (_base_root_for_queue(args.queue) / journal).resolve()
        lock_paths.append(_controller_module().migration_authority_lock(journal))
    with _locked_paths(lock_paths):
        atomic_mutate(args.queue, mutate, expected_revision=args.expected_revision)
        payload["queue_path"] = str(args.queue.resolve())
        if args.output is not None:
            _write_json_unlocked(args.output, payload)
    return payload


def _command_block(args: argparse.Namespace) -> JsonObject:
    blocker = read_json(args.blocker)
    return atomic_mutate(
        args.queue,
        lambda queue: block_feature(
            queue,
            index=_parse_index(args.index),
            coordinator_id=args.coordinator_id,
            lease_id=args.lease_id,
            blocker=blocker,
            resume_token=args.resume_token,
        ),
        expected_revision=args.expected_revision,
    )


def _command_resume(args: argparse.Namespace) -> JsonObject:
    evidence = read_json(args.evidence)
    migration = evidence.get("migration")
    if not isinstance(migration, dict) or not isinstance(migration.get("journal_path"), str):
        raise AuthorizationError("resume evidence requires migration journal path")
    journal = Path(migration["journal_path"])
    if not journal.is_absolute():
        journal = (_base_root_for_queue(args.queue) / journal).resolve()
    delta_scope = read_json(args.delta_scope) if args.delta_scope is not None else None
    with _locked_paths([_controller_module().migration_authority_lock(journal)]):
        return atomic_mutate(
            args.queue,
            lambda queue: resume_blocked_feature(
                queue,
                index=_parse_index(args.index),
                token=args.token,
                resolution_evidence=evidence,
                coordinator_id=args.coordinator_id,
                lease_id=args.lease_id,
                base_root=_base_root_for_queue(args.queue),
                require_controller_migration=True,
                delta_scope=delta_scope,
            ),
            expected_revision=args.expected_revision,
        )


def _command_ack(args: argparse.Namespace) -> JsonObject:
    queue, transaction = acknowledge_feature(
        args.queue,
        args.transaction,
        args.feature_result,
        expected_queue_revision=args.expected_revision,
        expected_transaction_revision=args.expected_transaction_revision,
    )
    return {"queue": queue, "transaction": transaction}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("queue", type=Path)
    inspect_parser.set_defaults(handler=_command_inspect)

    wait_parser = subparsers.add_parser("wait")
    wait_parser.add_argument("queue", type=Path)
    wait_parser.add_argument("--since")
    wait_parser.add_argument("--timeout-seconds", type=float, default=55)
    wait_parser.add_argument("--interval-seconds", type=float, default=0.25)
    wait_parser.set_defaults(handler=_command_wait)

    adopt_parser = subparsers.add_parser("adopt")
    adopt_parser.add_argument("queue", type=Path)
    adopt_parser.add_argument("--from-engine", required=True)
    adopt_parser.add_argument("--token", required=True)
    adopt_parser.add_argument("--expected-revision", type=int)
    adopt_parser.set_defaults(handler=_command_adopt)

    input_parser = subparsers.add_parser("add-input")
    input_parser.add_argument("queue", type=Path)
    input_parser.add_argument("input", type=Path)
    input_parser.add_argument("--index")
    input_parser.add_argument("--expected-revision", type=int)
    input_parser.set_defaults(handler=_command_add_input)

    dispatch_parser = subparsers.add_parser("dispatch")
    dispatch_parser.add_argument("queue", type=Path)
    dispatch_parser.add_argument("--base-worktree-path", required=True, type=Path)
    dispatch_parser.add_argument("--coordinator-id", required=True)
    dispatch_parser.add_argument("--clear-pause", action="store_true")
    dispatch_parser.add_argument("--lease-id")
    dispatch_parser.add_argument("--expected-revision", type=int)
    dispatch_parser.add_argument("--output", type=Path)
    dispatch_parser.set_defaults(handler=_command_dispatch)

    block_parser = subparsers.add_parser("block")
    block_parser.add_argument("queue", type=Path)
    block_parser.add_argument("--index", required=True)
    block_parser.add_argument("--coordinator-id", required=True)
    block_parser.add_argument("--lease-id", required=True)
    block_parser.add_argument("--blocker", required=True, type=Path)
    block_parser.add_argument("--resume-token", required=True)
    block_parser.add_argument("--expected-revision", type=int)
    block_parser.set_defaults(handler=_command_block)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("queue", type=Path)
    resume_parser.add_argument("--index", required=True)
    resume_parser.add_argument("--token", required=True)
    resume_parser.add_argument("--evidence", required=True, type=Path)
    resume_parser.add_argument("--coordinator-id", required=True)
    resume_parser.add_argument("--lease-id", required=True)
    resume_parser.add_argument("--expected-revision", type=int)
    resume_parser.add_argument("--delta-scope", type=Path)
    resume_parser.set_defaults(handler=_command_resume)

    ack_parser = subparsers.add_parser("ack")
    ack_parser.add_argument("queue", type=Path)
    ack_parser.add_argument("transaction", type=Path)
    ack_parser.add_argument("feature_result", type=Path)
    ack_parser.add_argument("--expected-revision", type=int)
    ack_parser.add_argument("--expected-transaction-revision", type=int)
    ack_parser.set_defaults(handler=_command_ack)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the feature queue state command-line interface."""

    args = _parser().parse_args(argv)
    try:
        result = args.handler(args)
    except SerialStateError as exc:
        print(json.dumps({"status": "blocked", "error": type(exc).__name__, "message": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
