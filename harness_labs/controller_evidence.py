"""Content-addressed evidence storage used by the hybrid controller."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .audit import AuditJournal


class EvidenceError(ValueError):
    """Raised for unknown, malformed, or mismatched evidence references."""


@dataclass(frozen=True)
class EvidenceRecord:
    ref: str
    kind: str
    sha256: str
    media_type: str
    producer_task_id: str
    size_bytes: int
    audit_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "producer_task_id": self.producer_task_id,
            "size_bytes": self.size_bytes,
            "audit_path": self.audit_path,
        }


class EvidenceCatalog:
    """Store bounded evidence by digest and expose metadata separately."""

    def __init__(self, *, audit: AuditJournal | None = None) -> None:
        self._audit = audit
        self._records: dict[str, EvidenceRecord] = {}
        self._content: dict[str, bytes] = {}
        self._mutex = threading.RLock()

    @property
    def audit(self) -> AuditJournal | None:
        """Return the journal that owns durable evidence, when configured."""

        return self._audit

    def add(
        self,
        *,
        kind: str,
        content: bytes | str | Mapping[str, Any] | list[Any],
        media_type: str,
        producer_task_id: str,
    ) -> EvidenceRecord:
        if not kind.strip() or not media_type.strip() or not producer_task_id.strip():
            raise EvidenceError("evidence metadata must be non-empty")
        raw = _encode(content)
        digest = hashlib.sha256(raw).hexdigest()
        ref = f"artifact:sha256:{digest}"
        with self._mutex:
            existing = self._records.get(ref)
            if existing is not None:
                return existing
            audit_path = None
            if self._audit is not None:
                artifact = self._audit.write_artifact(
                    kind,
                    raw,
                    media_type=media_type,
                )
                audit_path = artifact.path
            record = EvidenceRecord(
                ref=ref,
                kind=kind,
                sha256=digest,
                media_type=media_type,
                producer_task_id=producer_task_id,
                size_bytes=len(raw),
                audit_path=audit_path,
            )
            self._records[ref] = record
            self._content[ref] = raw
            return record

    def metadata(self, ref: str) -> EvidenceRecord:
        with self._mutex:
            try:
                return self._records[ref]
            except KeyError as exc:
                raise EvidenceError(f"unknown evidence reference: {ref}") from exc

    def open(self, ref: str) -> bytes:
        with self._mutex:
            try:
                return self._content[ref]
            except KeyError as exc:
                raise EvidenceError(f"unknown evidence reference: {ref}") from exc

    def contains(self, ref: str) -> bool:
        with self._mutex:
            return ref in self._records

    def list(self) -> tuple[EvidenceRecord, ...]:
        with self._mutex:
            return tuple(self._records[ref] for ref in sorted(self._records))

    def restore(self, record: Mapping[str, Any], content: bytes) -> EvidenceRecord:
        """Restore one checkpoint-bound record without writing a duplicate artifact."""

        value = EvidenceRecord(
            ref=str(record["ref"]),
            kind=str(record["kind"]),
            sha256=str(record["sha256"]),
            media_type=str(record["media_type"]),
            producer_task_id=str(record["producer_task_id"]),
            size_bytes=int(record["size_bytes"]),
            audit_path=(
                str(record["audit_path"])
                if record.get("audit_path") is not None
                else None
            ),
        )
        if hashlib.sha256(content).hexdigest() != value.sha256:
            raise EvidenceError("restored evidence digest does not match")
        if value.ref != f"artifact:sha256:{value.sha256}":
            raise EvidenceError("restored evidence reference does not match digest")
        with self._mutex:
            self._records[value.ref] = value
            self._content[value.ref] = bytes(content)
        return value


def _encode(content: bytes | str | Mapping[str, Any] | list[Any]) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (Mapping, list)):
        return (
            json.dumps(
                content,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    raise EvidenceError("unsupported evidence content type")
