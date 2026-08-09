"""Verified, read-only projections of one audit journal.

Missing files are reported as unavailable; no outcome is inferred from their
absence.  This module is deliberately independent of the execution plane.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import AuditError, AuditJournal


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "blocked", "interrupted"})


def availability(state: str, reason: str | None = None) -> dict[str, str | None]:
    if state not in {"available", "partial", "unavailable"}:
        raise ValueError("invalid availability state")
    if state == "available" and reason is not None:
        raise ValueError("available evidence cannot have a reason")
    if state != "available" and not reason:
        raise ValueError("unavailable evidence requires a reason")
    return {"state": state, "reason": reason}


def project_run_metrics(run_dir: Path) -> dict[str, Any]:
    """Project only an ``AuditJournal.verify``-authenticated run directory."""
    supplied_directory = Path(run_dir)
    if supplied_directory.is_symlink():
        raise AuditError("run directory must not be a symlink")
    directory = supplied_directory.resolve()
    _reject_core_audit_symlinks(directory)
    verification = AuditJournal.verify(directory)
    checkpoint = _read_object(directory / "checkpoint.json")
    manifest = _optional_object(directory / "manifest.json")
    summary = _optional_object(directory / "summary.json")
    events = _read_events(directory / "events.jsonl")
    status = checkpoint.get("status")
    if not isinstance(status, str):
        raise AuditError("audit checkpoint status is invalid")
    return {
        "run_id": verification["run_id"],
        "status": status,
        "terminal": status in TERMINAL_STATUSES,
        "evidence_classification": verification["evidence_classification"],
        "checkpoint": checkpoint,
        "manifest": manifest,
        "summary": summary,
        "events": events,
        "event_count": verification["event_count"],
        "availability": {
            "journal": availability("available"),
            "manifest": availability("available") if manifest else availability("unavailable", "no terminal manifest exists"),
            "summary": availability("available") if summary else availability("unavailable", "summary is unavailable"),
        },
    }


class RunMetricsProjector:
    def project(self, run_dir: Path) -> dict[str, Any]:
        return project_run_metrics(run_dir)


def _reject_core_audit_symlinks(directory: Path) -> None:
    for name in ("events.jsonl", "checkpoint.json", "manifest.json", "summary.json"):
        if (directory / name).is_symlink():
            raise AuditError(f"{name} must not be a symlink")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"{path.name} must contain an object")
    return value


def _optional_object(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise AuditError(f"{path.name} must not be a symlink")
    return _read_object(path) if path.is_file() else None


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError("cannot read verified events") from exc
    if not all(isinstance(event, dict) for event in events):
        raise AuditError("event must be an object")
    return events
