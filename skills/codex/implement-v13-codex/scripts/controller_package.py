#!/usr/bin/env python3
"""Bind and explicitly migrate the bounded v13 Codex controller package."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Callable

from state_io import (
    StateError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    cas_update_locked,
    ordered_authority_locks,
    read_json,
    sha256_bytes,
    sha256_file,
)


PACKAGE_PROTOCOL = "implement-v13-codex/controller-package-manifest/1"
PACKAGE_VERSION = "1"
MIGRATION_PROTOCOL = "implement-v13-codex/controller-migration/1"
PROPOSAL_PROTOCOL = "implement-v13-codex/controller-migration-proposal/1"
PACKAGE_NAMES = ("implement-v13-codex", "serial-implement-codex")
INCLUDED_TOP_LEVEL = {
    "SKILL.md",
    "agents",
    "builtins",
    "examples",
    "references",
    "schemas",
    "scripts",
}
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store"}
AUTHORITY_ORDER = (
    "queue",
    "dispatch",
    "checkpoint",
    "transaction",
    "ledger",
    "rollover",
    "journal",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _source_parent() -> Path:
    return _package_root().parent


def _iter_source_files(source_parent: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for package_name in PACKAGE_NAMES:
        package = source_parent / package_name
        if not package.is_dir() or package.is_symlink():
            raise StateError(f"controller package source is unavailable: {package}")
        for top_name in sorted(INCLUDED_TOP_LEVEL):
            top = package / top_name
            if not top.exists():
                continue
            candidates = [top] if top.is_file() else sorted(top.rglob("*"))
            for path in candidates:
                if path.name in EXCLUDED_NAMES or any(part in EXCLUDED_NAMES for part in path.parts):
                    continue
                if path.is_symlink():
                    raise StateError(f"controller package source contains a symlink: {path}")
                if path.is_file():
                    relative = path.relative_to(source_parent).as_posix()
                    files.append((relative, path))
    if not files:
        raise StateError("controller package source manifest is empty")
    return sorted(files)


def _manifest_without_digest(source_parent: Path) -> dict[str, Any]:
    rows = []
    for relative, path in _iter_source_files(source_parent):
        mode = stat.S_IMODE(path.stat().st_mode)
        executable = bool(mode & 0o111)
        rows.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "mode": 0o500 if executable else 0o400,
                "executable": executable,
            }
        )
    return {
        "protocol": PACKAGE_PROTOCOL,
        "package_version": PACKAGE_VERSION,
        "files": rows,
    }


def source_package_digest(source_parent: Path | None = None) -> str:
    parent = (source_parent or _source_parent()).resolve()
    return sha256_bytes(canonical_bytes(_manifest_without_digest(parent)))


def copy_controller_package(source_parent: Path, destination: Path) -> dict[str, Any]:
    """Copy exactly the two controller packages and make the result read-only."""
    source_parent = source_parent.resolve()
    if not destination.is_absolute():
        raise StateError("run-owned controller package destination must be absolute")
    if destination.exists():
        return verify_controller_package(destination)
    destination.mkdir(parents=True, mode=0o700)
    manifest = _manifest_without_digest(source_parent)
    for row in manifest["files"]:
        relative = Path(row["path"])
        source = source_parent / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chmod(target, int(row["mode"]))
    manifest["manifest_digest"] = sha256_bytes(canonical_bytes(manifest))
    atomic_write_json(destination / "controller-package-manifest.v1.json", manifest, mode=0o400)
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        os.chmod(directory, 0o500)
    os.chmod(destination, 0o500)
    return verify_controller_package(destination)


def verify_controller_package(destination: Path, expected_digest: str | None = None) -> dict[str, Any]:
    destination = destination.resolve()
    manifest_path = destination / "controller-package-manifest.v1.json"
    manifest = read_json(manifest_path)
    if manifest.get("protocol") != PACKAGE_PROTOCOL or manifest.get("package_version") != PACKAGE_VERSION:
        raise StateError("unsupported controller package manifest")
    digest = manifest.get("manifest_digest")
    unsigned = {key: copy.deepcopy(value) for key, value in manifest.items() if key != "manifest_digest"}
    calculated = sha256_bytes(canonical_bytes(unsigned))
    if digest != calculated or (expected_digest is not None and digest != expected_digest):
        raise StateError("controller package manifest digest mismatch")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise StateError("controller package manifest files must be nonempty")
    expected_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise StateError("controller package manifest row must be an object")
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in expected_paths
        ):
            raise StateError("controller package manifest path is unsafe or duplicated")
        target = destination / relative
        if target.is_symlink() or not target.is_file():
            raise StateError(f"controller package file is missing or unsafe: {relative}")
        try:
            target.resolve().relative_to(destination)
        except ValueError as exc:
            raise StateError(f"controller package path escapes: {relative}") from exc
        if sha256_file(target) != row.get("sha256"):
            raise StateError(f"controller package file hash mismatch: {relative}")
        if stat.S_IMODE(target.stat().st_mode) != row.get("mode"):
            raise StateError(f"controller package file mode mismatch: {relative}")
        if bool(target.stat().st_mode & 0o111) is not row.get("executable"):
            raise StateError(f"controller package executable bit mismatch: {relative}")
        expected_paths.add(relative)
    actual_paths = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != manifest_path.name
    }
    if actual_paths != expected_paths:
        raise StateError("controller package contains an unmanifested or missing file")
    return manifest


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise StateError(f"cannot load controller authority module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _binding(migration_id: str, package_digest: str) -> dict[str, Any]:
    return {
        "controller_package_protocol": PACKAGE_PROTOCOL,
        "controller_package_version": PACKAGE_VERSION,
        "controller_package_digest": package_digest,
        "controller_package_path": str(_package_root().parent),
        "controller_migration_id": migration_id,
    }


def _require_absolute_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise StateError(f"{label} must be an absolute regular file")
    return path.resolve()


def _identity(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: document.get(key)
        for key in ("queue_run_id", "feature_run_id", "feature_index", "base_branch")
    }


def _assert_identity(expected: dict[str, Any], document: dict[str, Any], label: str) -> None:
    for key, value in expected.items():
        if document.get(key) != value:
            raise StateError(f"{label} identity mismatch: {key}")


def _journal_hash(path: Path) -> str:
    return sha256_file(path)


def _write_journal(path: Path, journal: dict[str, Any], label: str) -> None:
    journal["journal_revision"] = int(journal.get("journal_revision", -1)) + 1
    atomic_write_json(path, journal)
    crash_after = os.environ.get("IMPLEMENT_V13_MIGRATION_CRASH_AFTER")
    if crash_after == label:
        raise StateError(f"injected migration crash after {label}")


def _expected_post_document(
    document: dict[str, Any], expected_revision: int, binding: dict[str, Any]
) -> dict[str, Any]:
    if document.get("state_revision") != expected_revision:
        raise StateError("migration proposal revision witness is stale")
    updated = copy.deepcopy(document)
    updated.update(binding)
    updated["state_revision"] = expected_revision + 1
    return updated


def _queue_bytes(serial: Any, document: dict[str, Any]) -> bytes:
    return serial.queue_document_bytes(document)


def _state_bytes(document: dict[str, Any]) -> bytes:
    return canonical_bytes(document)


def _proposal_inputs(
    *,
    proposal_path: Path,
    queue_path: Path,
    dispatch_path: Path,
    checkpoint_path: Path,
    transaction_path: Path,
    ledger_path: Path,
    journal_path: Path,
    expected_queue_revision: int,
    expected_dispatch_sha256: str,
    expected_checkpoint_revision: int,
    expected_transaction_revision: int,
    expected_ledger_revision: int,
    certified_package_digest: str,
    authorization_path: Path,
) -> dict[str, Any]:
    paths = {
        "proposal": _require_absolute_file(proposal_path, "proposal"),
        "queue": _require_absolute_file(queue_path, "queue"),
        "dispatch": _require_absolute_file(dispatch_path, "dispatch"),
        "checkpoint": _require_absolute_file(checkpoint_path, "checkpoint"),
        "transaction": _require_absolute_file(transaction_path, "transaction"),
        "ledger": _require_absolute_file(ledger_path, "ledger"),
        "authorization": _require_absolute_file(authorization_path, "authorization evidence"),
        "journal": journal_path.resolve(),
    }
    if not journal_path.is_absolute():
        raise StateError("journal must be absolute")
    proposal = read_json(paths["proposal"])
    authorization = read_json(paths["authorization"])
    if proposal.get("protocol") != PROPOSAL_PROTOCOL:
        raise StateError("unsupported controller migration proposal")
    authorization_sha256 = sha256_file(paths["authorization"])
    if proposal.get("authorization_evidence_sha256") != authorization_sha256:
        raise StateError("migration authorization hash mismatch")
    if proposal.get("new_package_digest") != certified_package_digest:
        raise StateError("migration proposal package digest mismatch")
    package_root = _package_root().parent
    verify_controller_package(package_root, certified_package_digest)
    documents = {
        name: read_json(paths[name])
        for name in ("queue", "dispatch", "checkpoint", "transaction", "ledger")
    }
    existing_journal = read_json(paths["journal"]) if paths["journal"].is_file() else None
    expected_identity = proposal.get("identity")
    if not isinstance(expected_identity, dict) or not all(
        expected_identity.get(key) is not None
        for key in ("queue_run_id", "feature_run_id", "feature_index", "base_branch")
    ):
        raise StateError("migration proposal identity is incomplete")
    dispatch = documents["dispatch"]
    if dispatch.get("dispatch_action") != "launch":
        raise StateError("original migration dispatch must be the immutable fresh launch")
    if sha256_file(paths["dispatch"]) != expected_dispatch_sha256:
        raise StateError("immutable dispatch hash witness is stale")
    _assert_identity(expected_identity, dispatch, "dispatch")
    checkpoint = documents["checkpoint"]
    transaction = documents["transaction"]
    ledger = documents["ledger"]
    _assert_identity(expected_identity, checkpoint, "checkpoint")
    _assert_identity(expected_identity, transaction, "transaction")
    if ledger.get("feature_run_id") != expected_identity["feature_run_id"]:
        raise StateError("ledger identity mismatch")
    if (
        checkpoint.get("phase"),
        checkpoint.get("phase_detail"),
        checkpoint.get("phase_state"),
    ) not in {
        ("REVIEWING", "fix", "blocked"),
        ("REVIEWING", "fix", "ready"),
    }:
        raise StateError("migration requires blocked queue at REVIEWING/fix")
    queue_matches = [
        item
        for item in documents["queue"].get("features", [])
        if item.get("index") == expected_identity["feature_index"]
    ]
    if len(queue_matches) != 1 or queue_matches[0].get("status") != "blocked":
        raise StateError("migration requires the exact feature to be blocked in the queue")
    existing_digest = queue_matches[0].get("controller_package_digest")
    old_package_identity = proposal.get("old_package_identity")
    if existing_digest is not None and existing_digest != certified_package_digest:
        if old_package_identity != existing_digest:
            raise StateError("chained migration old package identity mismatch")
        for name, document in (
            ("checkpoint", checkpoint),
            ("transaction", transaction),
            ("ledger", ledger),
        ):
            if document.get("controller_package_digest") != existing_digest:
                raise StateError(f"chained migration {name} package identity mismatch")
    if transaction.get("state") != "prepared":
        raise StateError("migration requires prepared feature transaction")
    witnesses = {
        "queue": expected_queue_revision,
        "checkpoint": expected_checkpoint_revision,
        "transaction": expected_transaction_revision,
        "ledger": expected_ledger_revision,
    }
    for name, revision in witnesses.items():
        observed_revision = documents[name].get("state_revision")
        if observed_revision == revision:
            continue
        authority = (
            existing_journal.get("authorities", {}).get(name)
            if isinstance(existing_journal, dict)
            else None
        )
        if (
            observed_revision != revision + 1
            or not isinstance(authority, dict)
            or sha256_file(paths[name]) != authority.get("post_sha256")
            or existing_journal.get("new_package_digest") != certified_package_digest
        ):
            raise StateError(f"{name} revision witness is stale")
        reconstructed = copy.deepcopy(documents[name])
        if name == "queue":
            matches = [
                item
                for item in reconstructed.get("features", [])
                if item.get("index") == expected_identity["feature_index"]
            ]
            if len(matches) != 1:
                raise StateError("cannot reconstruct migrated queue prefix")
            target = matches[0]
        else:
            target = reconstructed
        for field in (
            "controller_package_protocol",
            "controller_package_version",
            "controller_package_digest",
            "controller_package_path",
            "controller_migration_id",
        ):
            target.pop(field, None)
        reconstructed["state_revision"] = revision
        documents[name] = reconstructed
    return {
        "paths": paths,
        "proposal": proposal,
        "authorization": authorization,
        "authorization_sha256": authorization_sha256,
        "documents": documents,
        "identity": expected_identity,
        "witnesses": witnesses,
        "certified_package_digest": certified_package_digest,
        "existing_journal": existing_journal,
    }


def _build_plan(inputs: dict[str, Any], serial: Any) -> dict[str, Any]:
    proposal = inputs["proposal"]
    migration_id = sha256_bytes(
        canonical_bytes(
            {
                "proposal_sha256": sha256_file(inputs["paths"]["proposal"]),
                "authorization_sha256": inputs["authorization_sha256"],
                "new_package_digest": inputs["certified_package_digest"],
            }
        )
    )
    binding = _binding(migration_id, inputs["certified_package_digest"])
    documents = inputs["documents"]
    feature_index = inputs["identity"]["feature_index"]
    queue_target = next(
        item
        for item in documents["queue"]["features"]
        if item.get("index") == feature_index
    )
    queue_post = serial.migrated_queue_document(
        documents["queue"],
        index=feature_index,
        expected_revision=inputs["witnesses"]["queue"],
        binding=binding,
        allow_rebind=queue_target.get("controller_package_digest") is not None,
    )
    checkpoint_post = _expected_post_document(
        documents["checkpoint"], inputs["witnesses"]["checkpoint"], binding
    )
    transaction_post = _expected_post_document(
        documents["transaction"], inputs["witnesses"]["transaction"], binding
    )
    ledger_post = _expected_post_document(
        documents["ledger"], inputs["witnesses"]["ledger"], binding
    )
    rollover_path_raw = proposal.get("rollover_summary_path")
    if not isinstance(rollover_path_raw, str) or not Path(rollover_path_raw).is_absolute():
        raise StateError("proposal rollover_summary_path must be absolute")
    rollover_path = Path(rollover_path_raw).resolve()
    rollover = {
        "protocol": "implement-v13-codex/controller-migration-rollover/1",
        **inputs["identity"],
        **binding,
        "old_coordinator_thread": proposal.get("old_coordinator_thread"),
        "old_context_state": "historical",
        "next_context": "fresh",
        "reason": proposal.get("reason"),
    }
    post_documents = {
        "queue": queue_post,
        "checkpoint": checkpoint_post,
        "transaction": transaction_post,
        "ledger": ledger_post,
    }
    authorities: dict[str, Any] = {}
    for name in ("queue", "checkpoint", "transaction", "ledger"):
        serializer = _queue_bytes if name == "queue" else lambda _serial, value: _state_bytes(value)
        authorities[name] = {
            "path": str(inputs["paths"][name]),
            "pre_sha256": sha256_file(inputs["paths"][name]),
            "pre_revision": inputs["witnesses"][name],
            "post_sha256": sha256_bytes(serializer(serial, post_documents[name])),
            "post_revision": inputs["witnesses"][name] + 1,
            "acknowledged": False,
        }
    dispatch_hash = sha256_file(inputs["paths"]["dispatch"])
    authorities["dispatch"] = {
        "path": str(inputs["paths"]["dispatch"]),
        "pre_sha256": dispatch_hash,
        "post_sha256": dispatch_hash,
        "immutable": True,
        "acknowledged": False,
    }
    authorities["rollover"] = {
        "path": str(rollover_path),
        "pre_sha256": None,
        "post_sha256": sha256_bytes(canonical_bytes(rollover)),
        "acknowledged": False,
    }
    journal = {
        "protocol": MIGRATION_PROTOCOL,
        "migration_id": migration_id,
        "state": "prepared",
        "old_package_identity": proposal.get("old_package_identity", "legacy_unfrozen"),
        "new_package_digest": inputs["certified_package_digest"],
        "authorization_evidence_sha256": inputs["authorization_sha256"],
        "identity": copy.deepcopy(inputs["identity"]),
        "state_schema_versions": copy.deepcopy(proposal.get("state_schema_versions", {})),
        "reason": proposal.get("reason"),
        "authorities": authorities,
        "write_order": list(AUTHORITY_ORDER),
        "invariant_results": [
            "identity_match",
            "blocked_REVIEWING_fix",
            "transaction_prepared",
            "original_dispatch_immutable",
            "legacy_receipts_preserved",
        ],
        "journal_revision": -1,
    }
    return {
        "journal": journal,
        "binding": binding,
        "post_documents": post_documents,
        "rollover": rollover,
        "rollover_path": rollover_path,
    }


def _authority_lock(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def migration_authority_lock(journal_path: Path) -> Path:
    return journal_path.parent / ".controller-migration-authority.lock"


def _apply_document(
    *,
    name: str,
    path: Path,
    expected: dict[str, Any],
    post: dict[str, Any],
    inputs: dict[str, Any],
    serial: Any,
    review: Any,
) -> None:
    current_hash = sha256_file(path)
    if current_hash == expected["post_sha256"]:
        return
    if current_hash != expected["pre_sha256"]:
        raise StateError(f"{name} migration witness conflicts with current bytes")
    if name == "queue":
        serial.cas_migrate_feature_locked(
            path,
            expected_revision=expected["pre_revision"],
            index=inputs["identity"]["feature_index"],
            binding=_binding(
                inputs["plan"]["journal"]["migration_id"],
                inputs["certified_package_digest"],
            ),
            expected_sha256=expected["pre_sha256"],
            allow_rebind=True,
        )
    elif name == "ledger":
        review.cas_save_ledger(
            path,
            expected["pre_revision"],
            _binding(
                inputs["plan"]["journal"]["migration_id"],
                inputs["certified_package_digest"],
            ),
            expected_sha256=expected["pre_sha256"],
        )
    else:
        cas_update_locked(
            path,
            expected["pre_revision"],
            _binding(
                inputs["plan"]["journal"]["migration_id"],
                inputs["certified_package_digest"],
            ),
            expected_sha256=expected["pre_sha256"],
        )
    if sha256_file(path) != expected["post_sha256"]:
        raise StateError(f"{name} CAS did not produce the proposed post hash")


def migrate_run(inputs: dict[str, Any], *, commit: bool) -> dict[str, Any]:
    package_parent = _package_root().parent
    serial = _load_module(
        package_parent / "serial-implement-codex" / "scripts" / "serial_state.py",
        "serial_state_for_controller_migration",
    )
    review = _load_module(
        _package_root() / "scripts" / "review_closure.py",
        "review_closure_for_controller_migration",
    )
    plan = _build_plan(inputs, serial)
    inputs["plan"] = plan
    if not commit:
        return {
            "status": "dry_run",
            "migration_id": plan["journal"]["migration_id"],
            "new_package_digest": inputs["certified_package_digest"],
            "authorities": plan["journal"]["authorities"],
            "original_dispatch_immutable": True,
        }
    paths = inputs["paths"]
    journal_path = paths["journal"]
    lock_paths = [
        migration_authority_lock(journal_path),
        _authority_lock(paths["queue"]),
        _authority_lock(paths["dispatch"]),
        _authority_lock(paths["checkpoint"]),
        _authority_lock(paths["transaction"]),
        _authority_lock(paths["ledger"]),
        _authority_lock(journal_path),
    ]
    with ordered_authority_locks(lock_paths):
        if journal_path.exists():
            journal = read_json(journal_path)
            if journal.get("migration_id") != plan["journal"]["migration_id"]:
                raise StateError("existing migration journal belongs to another proposal")
            if journal.get("state") == "committed":
                validate_committed_migration(journal_path)
                return {
                    "status": "committed",
                    "migration_id": journal["migration_id"],
                    "migration_receipt_sha256": _journal_hash(journal_path),
                    "recovered": False,
                    "no_op": True,
                }
        else:
            journal = plan["journal"]
            _write_journal(journal_path, journal, "prepared_journal")
        for name in ("queue", "dispatch", "checkpoint", "transaction", "ledger", "rollover"):
            authority = journal["authorities"][name]
            path = Path(authority["path"])
            if name == "dispatch":
                if sha256_file(path) != authority["pre_sha256"]:
                    raise StateError("immutable original dispatch changed during migration")
            elif name == "rollover":
                if path.exists():
                    if sha256_file(path) != authority["post_sha256"]:
                        raise StateError("rollover summary conflicts with migration proposal")
                else:
                    atomic_write_json(path, plan["rollover"])
                    if os.environ.get("IMPLEMENT_V13_MIGRATION_CRASH_AFTER") == "rollover_write":
                        raise StateError("injected migration crash after rollover_write")
            else:
                _apply_document(
                    name=name,
                    path=path,
                    expected=authority,
                    post=plan["post_documents"][name],
                    inputs=inputs,
                    serial=serial,
                    review=review,
                )
                if os.environ.get("IMPLEMENT_V13_MIGRATION_CRASH_AFTER") == f"{name}_write":
                    raise StateError(f"injected migration crash after {name}_write")
            if not authority.get("acknowledged"):
                authority["acknowledged"] = True
                authority["observed_post_sha256"] = sha256_file(path)
                _write_journal(journal_path, journal, f"{name}_ack")
        for name, authority in journal["authorities"].items():
            if not authority.get("acknowledged"):
                raise StateError(f"migration authority is not acknowledged: {name}")
            if sha256_file(Path(authority["path"])) != authority["post_sha256"]:
                raise StateError(f"migration authority read-back mismatch: {name}")
        journal["state"] = "validated"
        journal["validated_at"] = _utc_now()
        _write_journal(journal_path, journal, "validated_journal")
        journal["state"] = "committed"
        journal["committed_at"] = _utc_now()
        _write_journal(journal_path, journal, "committed_journal")
        validate_committed_migration(journal_path)
        return {
            "status": "committed",
            "migration_id": journal["migration_id"],
            "migration_receipt_sha256": _journal_hash(journal_path),
            "recovered": True,
            "no_op": False,
        }


def validate_committed_migration(
    journal_path: Path,
    *,
    expected_package_digest: str | None = None,
    expected_receipt_sha256: str | None = None,
    allow_queue_advance: bool = False,
) -> dict[str, Any]:
    journal = read_json(journal_path)
    if journal.get("protocol") != MIGRATION_PROTOCOL or journal.get("state") != "committed":
        raise StateError("controller migration is not committed")
    if expected_package_digest is not None and journal.get("new_package_digest") != expected_package_digest:
        raise StateError("committed migration package digest mismatch")
    if expected_receipt_sha256 is not None and sha256_file(journal_path) != expected_receipt_sha256:
        raise StateError("committed migration receipt hash mismatch")
    if journal.get("write_order") != list(AUTHORITY_ORDER):
        raise StateError("committed migration authority order mismatch")
    authorities = journal.get("authorities")
    if not isinstance(authorities, dict) or set(authorities) != {
        "queue", "dispatch", "checkpoint", "transaction", "ledger", "rollover"
    }:
        raise StateError("committed migration authorities are incomplete")
    for name, authority in authorities.items():
        if not isinstance(authority, dict) or authority.get("acknowledged") is not True:
            raise StateError(f"committed migration authority is unacknowledged: {name}")
        path = Path(str(authority.get("path", "")))
        if not path.is_absolute() or not path.is_file():
            raise StateError(f"committed migration authority path is unavailable: {name}")
        observed = sha256_file(path)
        if (
            name in {"queue", "checkpoint", "transaction", "ledger"}
            and allow_queue_advance
            and observed != authority.get("post_sha256")
        ):
            document = read_json(path)
            if name == "queue":
                matches = [
                    item
                    for item in document.get("features", [])
                    if item.get("feature_run_id")
                    == journal["identity"]["feature_run_id"]
                ]
                target = matches[0] if len(matches) == 1 else {}
            else:
                target = document
            identity = journal["identity"]
            if name == "queue":
                identity_matches = (
                    target.get("feature_run_id") == identity["feature_run_id"]
                    and target.get("index") == identity["feature_index"]
                    and document.get("queue_run_id") == identity["queue_run_id"]
                    and document.get("base_branch") == identity["base_branch"]
                )
            elif name == "ledger":
                identity_matches = (
                    target.get("feature_run_id") == identity["feature_run_id"]
                )
            else:
                identity_matches = (
                    target.get("feature_run_id") == identity["feature_run_id"]
                    and target.get("queue_run_id") == identity["queue_run_id"]
                    and target.get("feature_index") == identity["feature_index"]
                    and target.get("base_branch") == identity["base_branch"]
                )
            if (
                identity_matches
                and target.get("controller_package_digest")
                == journal["new_package_digest"]
                and target.get("controller_migration_id") == journal["migration_id"]
                and int(document.get("state_revision", -1))
                >= int(authority.get("post_revision", 0))
            ):
                continue
        if observed != authority.get("post_sha256") or observed != authority.get("observed_post_sha256"):
            raise StateError(f"committed migration authority hash mismatch: {name}")
    return journal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    copy_parser = sub.add_parser("copy")
    copy_parser.add_argument("--source-parent", required=True, type=Path)
    copy_parser.add_argument("--destination", required=True, type=Path)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("package", type=Path)
    verify_parser.add_argument("--expected-digest")
    migrate = sub.add_parser("migrate-run")
    for name in ("proposal", "queue", "dispatch", "checkpoint", "transaction", "ledger", "journal"):
        migrate.add_argument(f"--{name}", required=True, type=Path)
    migrate.add_argument("--expected-queue-revision", required=True, type=int)
    migrate.add_argument("--expected-dispatch-sha256", required=True)
    migrate.add_argument("--expected-checkpoint-revision", required=True, type=int)
    migrate.add_argument("--expected-transaction-revision", required=True, type=int)
    migrate.add_argument("--expected-ledger-revision", required=True, type=int)
    migrate.add_argument("--certified-package-digest", required=True)
    migrate.add_argument("--authorization-evidence", required=True, type=Path)
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--commit", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "copy":
            result = copy_controller_package(args.source_parent, args.destination)
        elif args.command == "verify":
            result = verify_controller_package(args.package, args.expected_digest)
        else:
            inputs = _proposal_inputs(
                proposal_path=args.proposal,
                queue_path=args.queue,
                dispatch_path=args.dispatch,
                checkpoint_path=args.checkpoint,
                transaction_path=args.transaction,
                ledger_path=args.ledger,
                journal_path=args.journal,
                expected_queue_revision=args.expected_queue_revision,
                expected_dispatch_sha256=args.expected_dispatch_sha256,
                expected_checkpoint_revision=args.expected_checkpoint_revision,
                expected_transaction_revision=args.expected_transaction_revision,
                expected_ledger_revision=args.expected_ledger_revision,
                certified_package_digest=args.certified_package_digest,
                authorization_path=args.authorization_evidence,
            )
            result = migrate_run(inputs, commit=args.commit)
    except (StateError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": type(exc).__name__, "message": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
