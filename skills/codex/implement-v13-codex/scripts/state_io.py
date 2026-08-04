#!/usr/bin/env python3
"""Crash-durable JSON state helpers for implement-v13-codex."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class StateError(RuntimeError):
    """Raised when durable state is missing, malformed, or stale."""


def canonical_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def sha256_bytes(value: bytes) -> str:
    """Return the hexadecimal SHA-256 digest of bytes."""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a regular file without following a caller-controlled replacement."""
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object or raise a state error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"unreadable JSON state: {path} ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise StateError(f"JSON state must be an object: {path}")
    return value


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    """Atomically replace JSON state and fsync both file and directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = canonical_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Atomically replace a byte artifact and fsync both file and directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for one state transaction."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def cas_update(path: Path, expected_revision: int, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge fields into state when its revision matches the caller's witness."""
    lock_path = path.with_name(f".{path.name}.lock")
    with locked(lock_path):
        current = read_json(path)
        actual = current.get("state_revision")
        if actual != expected_revision:
            raise StateError(f"stale state revision: expected {expected_revision}, found {actual}")
        current.update(updates)
        current["state_revision"] = expected_revision + 1
        atomic_write_json(path, current)
        return current


@contextmanager
def ordered_authority_locks(lock_paths: Sequence[Path]) -> Iterator[None]:
    """Acquire an explicit caller-owned authority order, rejecting duplicates."""
    resolved = [path.resolve() for path in lock_paths]
    if len(resolved) != len(set(resolved)):
        raise StateError("authority lock order contains duplicate paths")
    descriptors: list[int] = []
    try:
        for lock_path in resolved:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def cas_update_locked(
    path: Path,
    expected_revision: int,
    updates: dict[str, Any],
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """CAS one document while its authority lock is already held."""
    current = read_json(path)
    if expected_sha256 is not None and sha256_file(path) != expected_sha256:
        raise StateError(f"stale state hash for {path}")
    actual = current.get("state_revision")
    if actual != expected_revision:
        raise StateError(f"stale state revision: expected {expected_revision}, found {actual}")
    updated = dict(current)
    updated.update(updates)
    updated["state_revision"] = expected_revision + 1
    atomic_write_json(path, updated)
    return updated
