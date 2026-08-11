"""Durable, tamper-evident audit journal for one harness run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic_ns, sleep
from typing import Any, Mapping

from .usage import build_run_summary

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]


AUDIT_PROTOCOL = "harness-audit-event/1"
CHECKPOINT_PROTOCOL = "harness-audit-checkpoint/1"
MANIFEST_PROTOCOL = "harness-audit-manifest/1"
_TERMINAL = frozenset({"succeeded", "failed", "blocked", "interrupted"})
_EVIDENCE_CLASSES = frozenset(
    {"production_lifecycle", "component", "synthetic", "fabricated_fixture"}
)
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")
_MEDIA_TYPE_SUFFIXES = {
    "application/json": "json",
    "application/ld+json": "json",
    "application/octet-stream": "bin",
    "application/pdf": "pdf",
    "application/x-ndjson": "jsonl",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "text/csv": "csv",
    "text/html": "html",
    "text/markdown": "md",
    "text/plain": "txt",
    "text/tab-separated-values": "tsv",
}


def _synchronized(method):
    def wrapper(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return wrapper


class AuditError(RuntimeError):
    """Raised when durable audit evidence is invalid or cannot be written."""


class AuditConflictError(AuditError):
    """Raised when a conditional audit mutation observes a newer checkpoint."""


@dataclass(frozen=True)
class AuditActor:
    id: str
    role: str
    parent_id: str | None = None


@dataclass(frozen=True)
class AuditArtifact:
    path: str
    sha256: str
    size_bytes: int
    media_type: str
    evidence_classification: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "evidence_classification": self.evidence_classification,
        }


class AuditJournal:
    """Sole writer for an append-only event chain and atomic checkpoint."""

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        *,
        actor: AuditActor,
        evidence_classification: str = "production_lifecycle",
    ) -> None:
        if not run_id or "/" in run_id:
            raise ValueError("run_id must be a non-empty path-safe name")
        if evidence_classification not in _EVIDENCE_CLASSES:
            raise ValueError("invalid audit evidence classification")
        self.run_dir = run_dir.resolve()
        self.run_id = run_id
        self.actor = actor
        self.evidence_classification = evidence_classification
        self.events_path = self.run_dir / "events.jsonl"
        self.checkpoint_path = self.run_dir / "checkpoint.json"
        self.manifest_path = self.run_dir / "manifest.json"
        self.summary_path = self.run_dir / "summary.json"
        self.artifacts_dir = self.run_dir / "artifacts"
        self._lock_path = self.run_dir / ".audit.lock"
        self._sequence = 0
        self._head_hash: str | None = None
        self._revision = 0
        self._artifact_number = 0
        self._started_at = _timestamp()
        self._mutex = threading.RLock()
        self._finalized = False

        self.run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(self.run_dir, 0o700)
        self.artifacts_dir.mkdir(mode=0o700)
        self.events_path.touch(mode=0o600)
        lock_descriptor = os.open(
            self._lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(lock_descriptor, b"\0")
            os.fsync(lock_descriptor)
        finally:
            os.close(lock_descriptor)
        self.append(
            "run_started",
            status="started",
            payload={"audit_protocol": AUDIT_PROTOCOL},
        )
        self.checkpoint("running", {"active_children": [], "active_sessions": []})

    @classmethod
    def open_existing(
        cls,
        run_dir: Path,
        *,
        actor: AuditActor,
    ) -> AuditJournal:
        run_dir = run_dir.resolve()
        verification = _verify_event_journal(run_dir)
        checkpoint = _load_json(run_dir / "checkpoint.json")
        _validate_checkpoint(checkpoint)
        if checkpoint.get("run_id") != verification["run_id"]:
            raise AuditError("checkpoint run identity does not match the journal")
        if (
            checkpoint.get("evidence_classification")
            != verification["evidence_classification"]
        ):
            raise AuditError(
                "checkpoint evidence classification does not match the journal"
            )
        checkpoint_sequence = checkpoint.get("sequence")
        if (
            not isinstance(checkpoint_sequence, int)
            or checkpoint_sequence > verification["event_count"]
        ):
            raise AuditError("checkpoint sequence is ahead of the journal")
        expected_checkpoint_head = (
            verification["event_hashes"][checkpoint_sequence - 1]
            if checkpoint_sequence
            else None
        )
        if checkpoint.get("head_hash") != expected_checkpoint_head:
            raise AuditError("checkpoint does not bind its journal position")
        checkpoint_lag = verification["event_count"] - checkpoint_sequence
        manifest_exists = (run_dir / "manifest.json").is_file()
        if checkpoint.get("status") in _TERMINAL and not manifest_exists:
            raise AuditError("terminal checkpoint is missing its manifest")
        if manifest_exists:
            cls.verify(run_dir)
        if checkpoint_lag:
            if manifest_exists:
                raise AuditError("finalized audit checkpoint lags its journal")
            prior_revision = checkpoint["revision"]
            checkpoint = {
                **checkpoint,
                "revision": prior_revision + 1,
                "status": "recovering",
                "sequence": verification["event_count"],
                "head_hash": verification["head_hash"],
                "updated_at": _timestamp(),
                "state": {
                    **checkpoint["state"],
                    "reconciled_from_revision": prior_revision,
                    "reconciled_event_count": checkpoint_lag,
                },
            }
            _atomic_write(
                run_dir / "checkpoint.json",
                (_canonical(checkpoint) + "\n").encode("utf-8"),
                mode=0o600,
            )
        instance = cls.__new__(cls)
        instance.run_dir = run_dir
        instance.run_id = verification["run_id"]
        instance.actor = actor
        instance.evidence_classification = verification["evidence_classification"]
        instance.events_path = run_dir / "events.jsonl"
        instance.checkpoint_path = run_dir / "checkpoint.json"
        instance.manifest_path = run_dir / "manifest.json"
        instance.summary_path = run_dir / "summary.json"
        instance.artifacts_dir = run_dir / "artifacts"
        instance._lock_path = run_dir / ".audit.lock"
        instance._sequence = verification["event_count"]
        instance._head_hash = verification["head_hash"]
        instance._revision = checkpoint["revision"]
        instance._artifact_number = len(list(instance.artifacts_dir.iterdir()))
        instance._started_at = checkpoint["started_at"]
        instance._mutex = threading.RLock()
        instance._finalized = instance.manifest_path.is_file()
        if checkpoint_lag:
            instance.append(
                "checkpoint_reconciled",
                status="succeeded",
                payload={
                    "prior_revision": prior_revision,
                    "reconciled_event_count": checkpoint_lag,
                },
                actor=actor,
            )
            instance.merge_checkpoint(status="running", updates={})
        return instance

    @_synchronized
    def write_artifact(
        self,
        kind: str,
        content: bytes | str | Mapping[str, Any] | list[Any],
        *,
        media_type: str = "application/json",
    ) -> AuditArtifact:
        self._ensure_writable()
        if isinstance(content, Mapping) or isinstance(content, list):
            raw = (_canonical(content) + "\n").encode("utf-8")
            fallback_suffix = "json"
        elif isinstance(content, str):
            raw = content.encode("utf-8")
            fallback_suffix = "txt"
        elif isinstance(content, bytes):
            raw = content
            fallback_suffix = "bin"
        else:
            raise TypeError("audit artifact content has an unsupported type")
        normalized_media_type = media_type.partition(";")[0].strip().lower()
        suffix = _MEDIA_TYPE_SUFFIXES.get(
            normalized_media_type,
            fallback_suffix,
        )
        self._artifact_number += 1
        safe_kind = _SAFE_NAME.sub("-", kind).strip("-") or "artifact"
        name = f"{self._artifact_number:06d}-{safe_kind}.{suffix}"
        path = self.artifacts_dir / name
        _atomic_write(path, raw, mode=0o600)
        digest = hashlib.sha256(raw).hexdigest()
        return AuditArtifact(
            path=str(path.relative_to(self.run_dir)),
            sha256=digest,
            size_bytes=len(raw),
            media_type=media_type,
            evidence_classification=self.evidence_classification,
        )

    @_synchronized
    def append(
        self,
        event_type: str,
        *,
        status: str,
        payload: Mapping[str, Any],
        actor: AuditActor | None = None,
        attempt_id: str | None = None,
        parent_attempt_id: str | None = None,
        session_id: str | None = None,
        backend_id: str | None = None,
        duration_ms: int | None = None,
        artifacts: tuple[AuditArtifact, ...] = (),
    ) -> dict[str, Any]:
        self._ensure_writable()
        event_actor = actor or self.actor
        event: dict[str, Any] = {
            "protocol": AUDIT_PROTOCOL,
            "run_id": self.run_id,
            "evidence_classification": self.evidence_classification,
            "sequence": self._sequence,
            "timestamp": _timestamp(),
            "monotonic_ns": monotonic_ns(),
            "event_type": event_type,
            "status": status,
            "actor": {
                "id": event_actor.id,
                "role": event_actor.role,
                "parent_id": event_actor.parent_id,
            },
            "attempt_id": attempt_id,
            "parent_attempt_id": parent_attempt_id,
            "session_id": session_id,
            "backend_id": backend_id,
            "duration_ms": duration_ms,
            "artifacts": [artifact.as_dict() for artifact in artifacts],
            "payload": dict(payload),
            "previous_hash": self._head_hash,
        }
        event_hash = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
        event["event_hash"] = event_hash
        encoded = (_canonical(event) + "\n").encode("utf-8")
        with self._locked():
            descriptor = _open_append_with_retry(
                self.events_path,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(self.run_dir)
        self._head_hash = event_hash
        self._sequence += 1
        return event

    @_synchronized
    def checkpoint(self, status: str, state: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_writable()
        self._revision += 1
        checkpoint = {
            "protocol": CHECKPOINT_PROTOCOL,
            "run_id": self.run_id,
            "evidence_classification": self.evidence_classification,
            "revision": self._revision,
            "status": status,
            "sequence": self._sequence,
            "head_hash": self._head_hash,
            "started_at": self._started_at,
            "updated_at": _timestamp(),
            "state": dict(state),
        }
        _atomic_write(
            self.checkpoint_path,
            (_canonical(checkpoint) + "\n").encode("utf-8"),
            mode=0o600,
        )
        return checkpoint

    @_synchronized
    def merge_checkpoint(
        self,
        *,
        status: str = "running",
        updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._ensure_writable()
        current = (
            _load_json(self.checkpoint_path).get("state", {})
            if self.checkpoint_path.is_file()
            else {}
        )
        if not isinstance(current, dict):
            raise AuditError("audit checkpoint state must be an object")
        merged = {**current, **dict(updates)}
        return self.checkpoint(status, merged)

    def checkpoint_state(self) -> dict[str, Any]:
        checkpoint = _load_json(self.checkpoint_path)
        state = checkpoint.get("state")
        if not isinstance(state, dict):
            raise AuditError("audit checkpoint state must be an object")
        return dict(state)

    def checkpoint_ids(self, name: str) -> set[str]:
        values = self.checkpoint_state().get(name, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise AuditError(f"audit checkpoint {name} must be a list of strings")
        return set(values)

    @_synchronized
    def compare_and_swap_checkpoint(
        self,
        *,
        expected_revision: int,
        expected_head_hash: str | None,
        status: str,
        state: Mapping[str, Any],
        event_type: str,
        event_status: str,
        payload: Mapping[str, Any],
        actor: AuditActor | None = None,
    ) -> dict[str, Any]:
        """Atomically append an event and advance an expected checkpoint.

        The ordinary journal methods deliberately remain small append/checkpoint
        primitives for existing callers.  A graph controller that has more than
        one contender needs one durable compare-and-swap boundary instead: the
        event and the successor checkpoint must describe the same accepted
        revision.  Holding the journal's interprocess lock across both writes
        prevents a second controller from appending between them.
        """

        if expected_revision < 0:
            raise ValueError("expected checkpoint revision must be non-negative")
        self._ensure_writable()
        with self._locked():
            verification = _verify_event_journal(self.run_dir)
            checkpoint = _load_json(self.checkpoint_path)
            _validate_checkpoint(checkpoint)
            if checkpoint.get("run_id") != self.run_id:
                raise AuditError("checkpoint run identity does not match the journal")
            if checkpoint.get("sequence") != verification["event_count"] or checkpoint.get(
                "head_hash"
            ) != verification["head_hash"]:
                raise AuditError("checkpoint does not bind the journal head")
            if checkpoint.get("revision") != expected_revision:
                raise AuditConflictError("audit checkpoint revision changed")
            if checkpoint.get("head_hash") != expected_head_hash:
                raise AuditConflictError("audit checkpoint head changed")

            # Refresh this instance while holding the same lock.  This makes a
            # reopened controller safe even when another process advanced the
            # journal after it was opened.
            self._sequence = verification["event_count"]
            self._head_hash = verification["head_hash"]
            self._revision = expected_revision
            event = self._append_locked(
                event_type,
                status=event_status,
                payload=payload,
                actor=actor,
            )
            self._revision += 1
            successor = {
                "protocol": CHECKPOINT_PROTOCOL,
                "run_id": self.run_id,
                "evidence_classification": self.evidence_classification,
                "revision": self._revision,
                "status": status,
                "sequence": self._sequence,
                "head_hash": self._head_hash,
                "started_at": checkpoint["started_at"],
                "updated_at": _timestamp(),
                "state": dict(state),
            }
            _atomic_write(
                self.checkpoint_path,
                (_canonical(successor) + "\n").encode("utf-8"),
                mode=0o600,
            )
            return {"event": event, "checkpoint": successor}

    @_synchronized
    def finalize(
        self,
        status: str,
        *,
        result: Mapping[str, Any],
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable()
        if status not in _TERMINAL:
            raise ValueError("audit final status must be terminal")
        result_artifact = self.write_artifact("final-result", result)
        self.append(
            "run_completed" if status == "succeeded" else "run_failed",
            status=status,
            payload={"terminal_status": status},
            artifacts=(result_artifact,),
        )
        checkpoint = self.checkpoint(
            status,
            {
                **({} if state is None else dict(state)),
                "active_children": [],
                "active_sessions": [],
            },
        )
        checkpoint_raw = self.checkpoint_path.read_bytes()
        artifacts = _artifact_inventory(
            self.run_dir,
            self.evidence_classification,
        )
        manifest = {
            "protocol": MANIFEST_PROTOCOL,
            "run_id": self.run_id,
            "evidence_classification": self.evidence_classification,
            "status": status,
            "started_at": self._started_at,
            "finished_at": _timestamp(),
            "event_count": self._sequence,
            "head_hash": self._head_hash,
            "checkpoint_sha256": hashlib.sha256(checkpoint_raw).hexdigest(),
            "checkpoint_revision": checkpoint["revision"],
            "artifacts": artifacts,
        }
        summary = build_run_summary(
            self.events_path,
            status=status,
            started_at=self._started_at,
            finished_at=manifest["finished_at"],
        )
        _atomic_write(
            self.summary_path,
            (_canonical(summary) + "\n").encode("utf-8"),
            mode=0o600,
        )
        manifest["summary_sha256"] = hashlib.sha256(
            self.summary_path.read_bytes()
        ).hexdigest()
        manifest_hash = hashlib.sha256(
            _canonical(manifest).encode("utf-8")
        ).hexdigest()
        manifest["manifest_hash"] = manifest_hash
        _atomic_write(
            self.manifest_path,
            (_canonical(manifest) + "\n").encode("utf-8"),
            mode=0o600,
        )
        self._finalized = True
        self.verify(self.run_dir)
        return manifest

    @classmethod
    def recover_interrupted(
        cls,
        run_dir: Path,
        *,
        actor: AuditActor,
        reason: str,
    ) -> dict[str, Any]:
        journal = cls.open_existing(run_dir, actor=actor)
        checkpoint = _load_json(journal.checkpoint_path)
        if checkpoint["status"] in _TERMINAL:
            if journal.manifest_path.is_file():
                return _load_json(journal.manifest_path)
            raise AuditError("terminal checkpoint is missing its manifest")
        journal.append(
            "recovery",
            status="interrupted",
            payload={
                "reason": reason,
                "recovered_checkpoint_revision": checkpoint["revision"],
                "recovered_state": checkpoint["state"],
            },
        )
        journal.checkpoint(
            "interrupted",
            {
                "active_children": [],
                "active_sessions": [],
                "recovered_from_revision": checkpoint["revision"],
            },
        )
        return journal.finalize(
            "interrupted",
            result={"status": "interrupted", "reason": reason},
        )

    @staticmethod
    def verify(run_dir: Path) -> dict[str, Any]:
        run_dir = run_dir.resolve()
        verification = _verify_event_journal(run_dir)
        previous = verification["head_hash"]
        run_id = verification["run_id"]
        evidence_classification = verification["evidence_classification"]
        count = verification["event_count"]
        checkpoint = _load_json(run_dir / "checkpoint.json")
        _validate_checkpoint(checkpoint)
        if checkpoint.get("run_id") != run_id:
            raise AuditError("checkpoint run identity does not match the journal")
        if checkpoint.get("evidence_classification") != evidence_classification:
            raise AuditError(
                "checkpoint evidence classification does not match the journal"
            )
        manifest_path = run_dir / "manifest.json"
        if checkpoint.get("status") in _TERMINAL and not manifest_path.is_file():
            raise AuditError("terminal checkpoint is missing its manifest")
        if checkpoint.get("head_hash") != previous:
            raise AuditError("checkpoint does not bind the journal head")
        if checkpoint.get("sequence") != count:
            raise AuditError("checkpoint sequence does not match the journal")
        if manifest_path.is_file():
            manifest = _load_json(manifest_path)
            _validate_manifest(manifest)
            supplied = manifest.pop("manifest_hash", None)
            computed = hashlib.sha256(
                _canonical(manifest).encode("utf-8")
            ).hexdigest()
            if supplied != computed:
                raise AuditError("audit manifest hash does not match")
            if manifest.get("head_hash") != previous:
                raise AuditError("audit manifest does not bind the journal head")
            if manifest.get("run_id") != run_id:
                raise AuditError("manifest run identity does not match the journal")
            if manifest.get("evidence_classification") != evidence_classification:
                raise AuditError(
                    "manifest evidence classification does not match the journal"
                )
            if manifest.get("event_count") != count:
                raise AuditError("manifest event count does not match the journal")
            if manifest.get("checkpoint_revision") != checkpoint.get("revision"):
                raise AuditError("manifest checkpoint revision does not match")
            if manifest.get("status") != checkpoint.get("status"):
                raise AuditError("manifest status does not match the checkpoint")
            checkpoint_digest = hashlib.sha256(
                (run_dir / "checkpoint.json").read_bytes()
            ).hexdigest()
            if manifest.get("checkpoint_sha256") != checkpoint_digest:
                raise AuditError("audit manifest checkpoint hash does not match")
            if "summary_sha256" in manifest:
                summary_path = run_dir / "summary.json"
                if not summary_path.is_file():
                    raise AuditError("audit manifest summary is missing")
                if manifest.get("summary_sha256") != hashlib.sha256(
                    summary_path.read_bytes()
                ).hexdigest():
                    raise AuditError("audit manifest summary hash does not match")
            for artifact in manifest.get("artifacts", []):
                _verify_artifact(run_dir, artifact)
            if manifest.get("artifacts") != _artifact_inventory(
                run_dir,
                evidence_classification,
            ):
                raise AuditError("manifest artifact inventory is incomplete")
        return {
            "run_id": run_id,
            "event_count": count,
            "head_hash": previous,
            "evidence_classification": evidence_classification,
        }

    def _locked(self):
        return _FileLock(self._lock_path)

    def _append_locked(
        self,
        event_type: str,
        *,
        status: str,
        payload: Mapping[str, Any],
        actor: AuditActor | None = None,
    ) -> dict[str, Any]:
        """Append while the caller holds ``_locked`` and has current metadata."""

        event_actor = actor or self.actor
        event: dict[str, Any] = {
            "protocol": AUDIT_PROTOCOL,
            "run_id": self.run_id,
            "evidence_classification": self.evidence_classification,
            "sequence": self._sequence,
            "timestamp": _timestamp(),
            "monotonic_ns": monotonic_ns(),
            "event_type": event_type,
            "status": status,
            "actor": {
                "id": event_actor.id,
                "role": event_actor.role,
                "parent_id": event_actor.parent_id,
            },
            "attempt_id": None,
            "parent_attempt_id": None,
            "session_id": None,
            "backend_id": None,
            "duration_ms": None,
            "artifacts": [],
            "payload": dict(payload),
            "previous_hash": self._head_hash,
        }
        event_hash = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
        event["event_hash"] = event_hash
        descriptor = _open_append_with_retry(self.events_path)
        try:
            os.write(descriptor, (_canonical(event) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.run_dir)
        self._head_hash = event_hash
        self._sequence += 1
        return event

    def _ensure_writable(self) -> None:
        if self._finalized:
            raise AuditError("audit run is already finalized")


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("r+b")
        if fcntl is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover
            raise AuditError("this platform has no supported file-lock API")
        return self

    def __exit__(self, exc_type, exc, traceback):
        assert self.stream is not None
        if fcntl is not None:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


def _verify_event_journal(run_dir: Path) -> dict[str, Any]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        raise AuditError("audit event journal is missing")
    previous: str | None = None
    run_id: str | None = None
    evidence_classification: str | None = None
    hashes: list[str] = []
    with events_path.open("r", encoding="utf-8") as stream:
        for count, line in enumerate(stream):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditError("audit event journal contains invalid JSON") from exc
            _validate_event(event)
            supplied_hash = event.pop("event_hash", None)
            computed_hash = hashlib.sha256(
                _canonical(event).encode("utf-8")
            ).hexdigest()
            if supplied_hash != computed_hash:
                raise AuditError(f"audit event {count} hash does not match")
            if event.get("sequence") != count:
                raise AuditError("audit event sequence is not contiguous")
            if event.get("previous_hash") != previous:
                raise AuditError("audit event hash chain is broken")
            if event.get("protocol") != AUDIT_PROTOCOL:
                raise AuditError("audit event protocol is invalid")
            if run_id is None:
                run_id = event.get("run_id")
            elif event.get("run_id") != run_id:
                raise AuditError("audit event run identity changed")
            if evidence_classification is None:
                evidence_classification = event.get("evidence_classification")
            elif event.get("evidence_classification") != evidence_classification:
                raise AuditError("audit evidence classification changed")
            for artifact in event.get("artifacts", []):
                _verify_artifact(run_dir, artifact)
            assert isinstance(supplied_hash, str)
            previous = supplied_hash
            hashes.append(supplied_hash)
    if not hashes or not isinstance(run_id, str):
        raise AuditError("audit event journal is empty")
    return {
        "run_id": run_id,
        "event_count": len(hashes),
        "head_hash": previous,
        "event_hashes": tuple(hashes),
        "evidence_classification": evidence_classification,
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _open_append_with_retry(path: Path) -> int:
    """Tolerate brief macOS EPERM/EACCES windows without losing an event."""

    delay = 0.005
    for attempt in range(8):
        try:
            return os.open(path, os.O_WRONLY | os.O_APPEND, 0o600)
        except PermissionError:
            if attempt == 7:
                raise
            sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"invalid audit JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"audit JSON must be an object: {path.name}")
    return value


def _artifact_inventory(
    run_dir: Path,
    evidence_classification: str,
) -> list[dict[str, Any]]:
    inventory = []
    for path in sorted((run_dir / "artifacts").iterdir()):
        if not path.is_file():
            raise AuditError("audit artifact inventory contains a non-file")
        raw = path.read_bytes()
        inventory.append(
            {
                "path": str(path.relative_to(run_dir)),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "media_type": "application/octet-stream",
                "evidence_classification": evidence_classification,
            }
        )
    return inventory


def _verify_artifact(run_dir: Path, artifact: Mapping[str, Any]) -> None:
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str):
        raise AuditError("audit artifact path is invalid")
    path = (run_dir / raw_path).resolve()
    try:
        path.relative_to((run_dir / "artifacts").resolve())
    except ValueError as exc:
        raise AuditError("audit artifact escapes the artifact directory") from exc
    if not path.is_file():
        raise AuditError(f"audit artifact is missing: {raw_path}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
        raise AuditError(f"audit artifact hash does not match: {raw_path}")
    if len(raw) != artifact.get("size_bytes"):
        raise AuditError(f"audit artifact size does not match: {raw_path}")


def _validate_event(event: Any) -> None:
    required = {
        "protocol",
        "run_id",
        "evidence_classification",
        "sequence",
        "timestamp",
        "monotonic_ns",
        "event_type",
        "status",
        "actor",
        "attempt_id",
        "parent_attempt_id",
        "session_id",
        "backend_id",
        "duration_ms",
        "artifacts",
        "payload",
        "previous_hash",
        "event_hash",
    }
    if not isinstance(event, dict) or set(event) != required:
        raise AuditError("audit event does not match its schema")
    actor = event["actor"]
    if (
        not isinstance(event["run_id"], str)
        or not event["run_id"]
        or event["evidence_classification"] not in _EVIDENCE_CLASSES
        or not isinstance(event["sequence"], int)
        or event["sequence"] < 0
        or not isinstance(event["monotonic_ns"], int)
        or event["monotonic_ns"] < 0
        or not isinstance(event["event_type"], str)
        or not event["event_type"]
        or not isinstance(event["status"], str)
        or not event["status"]
        or not isinstance(actor, dict)
        or set(actor) != {"id", "role", "parent_id"}
        or not isinstance(actor["id"], str)
        or not actor["id"]
        or not isinstance(actor["role"], str)
        or not actor["role"]
        or actor["parent_id"] is not None
        and not isinstance(actor["parent_id"], str)
        or not isinstance(event["payload"], dict)
        or not isinstance(event["artifacts"], list)
    ):
        raise AuditError("audit event does not match its schema")
    _validate_timestamp(event["timestamp"])
    for name in ("attempt_id", "parent_attempt_id", "session_id", "backend_id"):
        if event[name] is not None and not isinstance(event[name], str):
            raise AuditError("audit event identity does not match its schema")
    if event["duration_ms"] is not None and (
        not isinstance(event["duration_ms"], int) or event["duration_ms"] < 0
    ):
        raise AuditError("audit event duration does not match its schema")
    for name in ("previous_hash", "event_hash"):
        value = event[name]
        if value is not None and not _is_sha256(value):
            raise AuditError("audit event hash does not match its schema")
    for artifact in event["artifacts"]:
        _validate_artifact_descriptor(artifact)


def _validate_checkpoint(checkpoint: Mapping[str, Any]) -> None:
    required = {
        "protocol",
        "run_id",
        "evidence_classification",
        "revision",
        "status",
        "sequence",
        "head_hash",
        "started_at",
        "updated_at",
        "state",
    }
    if (
        set(checkpoint) != required
        or checkpoint["protocol"] != CHECKPOINT_PROTOCOL
        or not isinstance(checkpoint["run_id"], str)
        or not checkpoint["run_id"]
        or checkpoint["evidence_classification"] not in _EVIDENCE_CLASSES
        or not isinstance(checkpoint["revision"], int)
        or checkpoint["revision"] < 1
        or not isinstance(checkpoint["sequence"], int)
        or checkpoint["sequence"] < 1
        or not _is_sha256(checkpoint["head_hash"])
        or not isinstance(checkpoint["status"], str)
        or not checkpoint["status"]
        or not isinstance(checkpoint["state"], dict)
    ):
        raise AuditError("audit checkpoint does not match its schema")
    _validate_timestamp(checkpoint["started_at"])
    _validate_timestamp(checkpoint["updated_at"])


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "protocol",
        "run_id",
        "evidence_classification",
        "status",
        "started_at",
        "finished_at",
        "event_count",
        "head_hash",
        "checkpoint_sha256",
        "checkpoint_revision",
        "artifacts",
        "manifest_hash",
    }
    supplied = frozenset(manifest)
    if (
        supplied not in {frozenset(required), frozenset(required | {"summary_sha256"})}
        or manifest["protocol"] != MANIFEST_PROTOCOL
        or not isinstance(manifest["run_id"], str)
        or not manifest["run_id"]
        or manifest["evidence_classification"] not in _EVIDENCE_CLASSES
        or manifest["status"] not in _TERMINAL
        or not isinstance(manifest["event_count"], int)
        or manifest["event_count"] < 1
        or not _is_sha256(manifest["head_hash"])
        or not _is_sha256(manifest["checkpoint_sha256"])
        or not isinstance(manifest["checkpoint_revision"], int)
        or manifest["checkpoint_revision"] < 1
        or (
            "summary_sha256" in manifest
            and not _is_sha256(manifest["summary_sha256"])
        )
        or not isinstance(manifest["artifacts"], list)
        or not _is_sha256(manifest["manifest_hash"])
    ):
        raise AuditError("audit manifest does not match its schema")
    _validate_timestamp(manifest["started_at"])
    _validate_timestamp(manifest["finished_at"])
    for artifact in manifest["artifacts"]:
        _validate_artifact_descriptor(artifact)


def _validate_artifact_descriptor(artifact: Any) -> None:
    if (
        not isinstance(artifact, dict)
        or set(artifact)
        != {
            "path",
            "sha256",
            "size_bytes",
            "media_type",
            "evidence_classification",
        }
        or not isinstance(artifact["path"], str)
        or not artifact["path"]
        or not _is_sha256(artifact["sha256"])
        or not isinstance(artifact["size_bytes"], int)
        or artifact["size_bytes"] < 0
        or not isinstance(artifact["media_type"], str)
        or not artifact["media_type"]
        or artifact["evidence_classification"] not in _EVIDENCE_CLASSES
    ):
        raise AuditError("audit artifact descriptor does not match its schema")


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AuditError("audit timestamp does not match its schema")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise AuditError("audit timestamp does not match its schema") from exc


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None
