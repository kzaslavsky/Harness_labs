"""Append-only convergence campaign ledger (CC-01, ``state-ledger``).

flock+fsync JSONL journal for one campaign's cross-round state, following
the ``JoinConflictResolutionStore`` durability discipline
(``harness_labs/plangraph/plan_graph_join.py``): every mutation folds the
full journal under an exclusive advisory lock, appends the new record(s),
flushes, and fsyncs before the lock releases, so state is derived purely by
replay and two writers serialized by ``flock`` cannot corrupt or interleave
the journal.

Record kinds (``state-ledger``): ``campaign_opened``; ``finding_opened``;
``finding_fix_claimed`` (projected from round success; never terminal, and
only accepted from a key currently ``open``); ``finding_fixed`` (only from
an ``observed_fixed`` verdict citing a capture cell and the assertion
evaluated, and only when that cell is recorded and not ``unstable``);
``finding_invalidated`` (an ``invalidated`` verdict against an active key);
``finding_reopened`` (carries ``reason``; ``reason: "base_rebase"`` is
stall-exempt and demotes every currently-``fixed`` key to ``fix_claimed``
instead of ``open``; a subsequent ``reopened`` verdict against a
rebase-demoted key is also stall-exempt); ``finding_ruled`` (one of the
closed ``RULING_DISPOSITIONS``; only ``waive`` enters the exclusion set);
``confirmed_good`` (excluded only with a machine-checkable assertion —
``{kind: file|test_id|selector|command, referent}`` — otherwise recorded as
``watch`` and left to route normally through the open set);
``target_amended`` (carries ``invalidation_scope``; a scopeless amendment
— ``invalidation_scope: None`` — sets the derived :meth:`is_blocked` state
until a later amendment states a scope); ``capture_coverage``. One
bookkeeping record,
``audit_ingested``, marks a sealed ``audit_result`` digest as folded
(idempotent re-ingest) and records which prior open/``fix_claimed`` keys it
left unmentioned (``unobserved`` — blocks the derived :meth:`success` view;
an explicit ``unobserved`` verdict counts the same as an omitted key).

Per-key derived status (not itself a wire record type): ``open`` ->
``fix_claimed`` -> ``fixed`` (an ``observed_fixed`` verdict) | back to
``open`` (a ``reopened`` verdict against a ``fix_claimed`` or ``fixed`` key;
against ``fix_claimed`` it is counted as an unsuccessful repair claim unless
the key is currently rebase-demoted; or a new finding re-emitted against an
already-``fixed``/``fix_claimed`` key); ``open``/``fix_claimed`` ->
``excluded`` (``waive`` ruling or a machine-checkable ``confirmed_good``) or
``amended`` (``amend_criterion`` ruling) or ``invalidated`` (an
``invalidated`` verdict) — all three are terminal and closed.

``audit_result`` ingest shape (one sealed, content-addressed artifact)::

    {
      "digest": "<content-addressed identity of this artifact>",
      "findings": [
        {"file": ..., "subject": ..., "required_paths": [...],
         "confidence": "C" | "S" | "C+S" | None, "supersedes_key": None,
         # optional semantic-envelope pass-through fields:
         "id": ..., "statement": ..., "category": ..., "severity": ...,
         "requires_disposition": ..., "evidence_refs": [...],
         "source_finding_ids": [...]},
        ...
      ],
      "verdicts": [
        {"key": [file, subject], "verdict": "observed_fixed" | "reopened"
                                             | "unobserved" | "invalidated",
         # observed_fixed only:
         "capture_cell": "<cell id>", "assertion": "<assertion evaluated>"},
        ...
      ],
      "confirmed_good": [
        {"key": [file, subject], "assertion": {"kind": ..., ...} | None,
         "reason": "..."},
        ...
      ],
      "capture_coverage": {"<cell id>": "ok" | "unreachable" | "unstable"},
    }

Every prior ``open``/``fix_claimed`` key the audit's ``verdicts`` do not
mention is folded into that ingest's ``unobserved`` set automatically —
callers never author ``unobserved`` entries themselves.

Record kinds are also exported as named module-level constants
(``RECORD_KIND_*``) so outside consumers — notably
``harness_labs.plangraph.finding_history`` (em-history) — never need a
record-kind string literal of their own. :meth:`ConvergenceLedger.key_lineage`
is the public read-only per-key view those consumers fold: every record
touching a finding key, grouped by key, each annotated with its journal
ordinal.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness_labs.core.controller_results import FINDING_SEVERITIES
from harness_labs.core.convergence_contract import (
    CAPTURE_CELL_STATUSES,
    RULING_DISPOSITIONS,
    VERDICT_KINDS,
)

Key = tuple[str, str]

PROTOCOL = "convergence-campaign-ledger/1"

# -- record-kind constants (em-history) --------------------------------
#
# Named so consumers outside this module (``harness_labs.plangraph.
# finding_history``, EM-B) never need to spell a record-kind string
# literal themselves. No kind here is new: this is the same vocabulary
# ``RECORD_TYPES`` has always enforced, merely given names.

RECORD_KIND_CAMPAIGN_OPENED = "campaign_opened"
RECORD_KIND_FINDING_OPENED = "finding_opened"
RECORD_KIND_FINDING_FIX_CLAIMED = "finding_fix_claimed"
RECORD_KIND_FINDING_FIXED = "finding_fixed"
RECORD_KIND_FINDING_REOPENED = "finding_reopened"
RECORD_KIND_FINDING_INVALIDATED = "finding_invalidated"
RECORD_KIND_FINDING_RULED = "finding_ruled"
RECORD_KIND_CONFIRMED_GOOD = "confirmed_good"
RECORD_KIND_TARGET_AMENDED = "target_amended"
RECORD_KIND_CAPTURE_COVERAGE = "capture_coverage"
RECORD_KIND_AUDIT_INGESTED = "audit_ingested"

_BASE_REBASE_REASON = "base_rebase"
_REPAIR_CLAIM_FAILED_REASON = "repair_claim_failed"
_FINDING_REEMITTED_REASON = "finding_reemitted"
_REBASE_REOBSERVATION_REASON = "rebase_reobservation_failed"
_REGRESSION_REOPENED_REASON = "regression_reopened"

_ACTIVE_STATUSES = ("open", "fix_claimed")
_CLOSED_STATUSES = ("fixed", "excluded", "amended", "invalidated")

_CONFIRMED_GOOD_ASSERTION_KINDS = frozenset(
    {"file", "test_id", "selector", "command"}
)


class ConvergenceLedgerError(ValueError):
    """Raised on a malformed ingest, ruling, or a corrupt journal."""


class ConvergenceLedger:
    """One campaign's append-only cross-round ledger at ``path``."""

    protocol = PROTOCOL

    RECORD_TYPES = frozenset({
        RECORD_KIND_CAMPAIGN_OPENED, RECORD_KIND_FINDING_OPENED,
        RECORD_KIND_FINDING_FIX_CLAIMED, RECORD_KIND_FINDING_FIXED,
        RECORD_KIND_FINDING_REOPENED, RECORD_KIND_FINDING_INVALIDATED,
        RECORD_KIND_FINDING_RULED, RECORD_KIND_CONFIRMED_GOOD,
        RECORD_KIND_TARGET_AMENDED, RECORD_KIND_CAPTURE_COVERAGE,
        RECORD_KIND_AUDIT_INGESTED,
    })

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- campaign lifecycle -------------------------------------------------

    def open_campaign(
        self,
        *,
        domain: str,
        target: Mapping[str, Any],
        base_commit: str,
        merge_base: str | None = None,
        predecessor_graph_id: str | None = None,
        seed_audit_digest: str | None = None,
        repo_identity_branch: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(domain, str) or not domain.strip():
            raise ConvergenceLedgerError("campaign domain must be a non-empty string")
        if not isinstance(target, Mapping) or not target:
            raise ConvergenceLedgerError("campaign target must be a non-empty object")
        for pinned_field in ("kind", "digest", "snapshot_path"):
            value = target.get(pinned_field)
            if not isinstance(value, str) or not value.strip():
                raise ConvergenceLedgerError(
                    f"campaign target is missing a non-empty {pinned_field!r}"
                )
        if not isinstance(base_commit, str) or not base_commit.strip():
            raise ConvergenceLedgerError(
                "campaign base_commit must be a non-empty string"
            )
        record = {
            "type": "campaign_opened",
            "domain": domain,
            "target": dict(target),
            "base_commit": base_commit,
            "merge_base": merge_base,
            "predecessor_graph_id": predecessor_graph_id,
            "seed_audit_digest": seed_audit_digest,
            "repo_identity_branch": repo_identity_branch,
            "config": dict(config) if config else {},
        }
        with self._locked() as handle:
            state = self._fold(handle)
            if state["campaign"] is not None:
                raise ConvergenceLedgerError(
                    "campaign_opened is already recorded for this ledger"
                )
            self._append(handle, record)
        return record

    # -- audit ingest ---------------------------------------------------

    def ingest_audit(self, audit_result: Mapping[str, Any]) -> dict[str, Any]:
        """Fold one sealed ``audit_result`` artifact into the ledger.

        Validates the entire artifact before writing anything, so a
        malformed finding fails the whole ingest with no partial state
        (``contracts-finding``). Idempotent by ``digest``: re-ingesting an
        already-folded digest changes no ledger state.
        """

        if not isinstance(audit_result, Mapping):
            raise ConvergenceLedgerError("audit_result must be an object")
        digest = audit_result.get("digest")
        if not isinstance(digest, str) or not digest.strip():
            raise ConvergenceLedgerError(
                "audit_result is missing a non-empty 'digest'"
            )

        raw_findings = audit_result.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ConvergenceLedgerError("audit_result 'findings' must be a list")
        findings = [_validate_finding(item) for item in raw_findings]

        raw_verdicts = audit_result.get("verdicts", [])
        if not isinstance(raw_verdicts, list):
            raise ConvergenceLedgerError("audit_result 'verdicts' must be a list")
        verdicts = [_validate_verdict(item) for item in raw_verdicts]

        raw_confirmed = audit_result.get("confirmed_good", [])
        if not isinstance(raw_confirmed, list):
            raise ConvergenceLedgerError(
                "audit_result 'confirmed_good' must be a list"
            )
        confirmed = [_validate_confirmed_good(item) for item in raw_confirmed]

        raw_coverage = audit_result.get("capture_coverage", {})
        if not isinstance(raw_coverage, Mapping):
            raise ConvergenceLedgerError(
                "audit_result 'capture_coverage' must be an object"
            )
        coverage = _validate_capture_coverage(raw_coverage)

        summary: dict[str, Any] = {
            "digest": digest, "idempotent": False,
            "opened": [], "fixed": [], "reopened": [], "unobserved": [],
            "excluded": [], "watch": [], "invalidated": [],
            "blocked_unstable": [], "blocked_unknown_cell": [],
        }

        with self._locked() as handle:
            state = self._fold(handle)
            if digest in state["ingested_digests"]:
                summary["idempotent"] = True
                return summary

            for verdict in verdicts:
                if verdict["key"] not in state["keys"]:
                    raise ConvergenceLedgerError(
                        f"{verdict['verdict']} verdict cites unknown key "
                        f"{verdict['key']!r}"
                    )

            prior_active = {
                key for key, info in state["keys"].items()
                if info["status"] in _ACTIVE_STATUSES
            }
            mentioned: set[Key] = set()

            if coverage:
                self._append(
                    handle,
                    {"type": "capture_coverage", "digest": digest, "cells": coverage},
                )
                state["capture_cells"].update(coverage)

            def _reopen(key: Key, info: dict[str, Any], reason: str) -> None:
                record = {
                    "type": "finding_reopened", "key": list(key),
                    "reason": reason, "digest": digest,
                }
                self._append(handle, record)
                info["status"] = "open"
                info["history"].append(record)
                if reason == _REPAIR_CLAIM_FAILED_REASON:
                    info["unsuccessful_repair_claims"] += 1
                summary["reopened"].append(list(key))

            for verdict in verdicts:
                key = verdict["key"]
                kind = verdict["verdict"]
                if kind != "unobserved":
                    mentioned.add(key)
                info = state["keys"][key]
                current_status = info["status"]

                if kind == "observed_fixed":
                    cell = verdict["capture_cell"]
                    cell_status = coverage.get(cell, state["capture_cells"].get(cell))
                    if cell_status is None:
                        summary["blocked_unknown_cell"].append(list(key))
                        continue
                    if cell_status == "unstable":
                        summary["blocked_unstable"].append(list(key))
                        continue
                    if current_status in _ACTIVE_STATUSES:
                        record = {
                            "type": "finding_fixed", "key": list(key),
                            "capture_cell": cell, "assertion": verdict["assertion"],
                            "digest": digest,
                        }
                        self._append(handle, record)
                        info["status"] = "fixed"
                        info["history"].append(record)
                        summary["fixed"].append(list(key))
                elif kind == "reopened":
                    if current_status == "fix_claimed":
                        reason = (
                            _REBASE_REOBSERVATION_REASON
                            if info.get("rebase_demoted")
                            else _REPAIR_CLAIM_FAILED_REASON
                        )
                        _reopen(key, info, reason)
                    elif current_status == "fixed":
                        _reopen(key, info, _REGRESSION_REOPENED_REASON)
                    # else: already open/excluded/amended/invalidated -- a
                    # redundant reopened verdict is a no-op.
                elif kind == "invalidated":
                    if current_status in _ACTIVE_STATUSES:
                        record = {
                            "type": "finding_invalidated", "key": list(key),
                            "digest": digest,
                        }
                        self._append(handle, record)
                        info["status"] = "invalidated"
                        info["history"].append(record)
                        summary["invalidated"].append(list(key))
                # 'unobserved' verdicts change no ledger state; the key is
                # kept out of `mentioned` so it folds into this audit's
                # unobserved set exactly like an omitted key.

            unobserved = sorted(prior_active - mentioned)
            summary["unobserved"] = [list(key) for key in unobserved]

            for finding in findings:
                key = (finding["file"], finding["subject"])
                info = state["keys"].get(key)
                if info is None:
                    record = {
                        "type": "finding_opened", "key": list(key), "digest": digest,
                        **{
                            field: value for field, value in finding.items()
                            if field not in ("file", "subject")
                        },
                        "file": finding["file"], "subject": finding["subject"],
                    }
                    self._append(handle, record)
                    state["keys"][key] = {
                        "status": "open", "history": [record],
                        "unsuccessful_repair_claims": 0, "finding": finding,
                        "rebase_demoted": False,
                    }
                    summary["opened"].append(list(key))
                elif info["status"] == "fixed":
                    _reopen(key, info, _FINDING_REEMITTED_REASON)
                elif info["status"] == "fix_claimed":
                    reason = (
                        _REBASE_REOBSERVATION_REASON
                        if info.get("rebase_demoted")
                        else _REPAIR_CLAIM_FAILED_REASON
                    )
                    _reopen(key, info, reason)
                # else: an already-open/excluded/amended/invalidated key
                # re-emitted with no status change is a no-op at the ledger
                # layer.

            for entry in confirmed:
                key = entry["key"]
                if entry["machine_checkable"]:
                    record = {
                        "type": "confirmed_good", "key": list(key),
                        "status": "excluded", "assertion": entry["assertion"],
                        "reason": entry["reason"], "digest": digest,
                    }
                    self._append(handle, record)
                    info = state["keys"].get(key)
                    if info is None:
                        state["keys"][key] = {
                            "status": "excluded", "history": [record],
                            "unsuccessful_repair_claims": 0, "finding": None,
                            "rebase_demoted": False,
                        }
                    else:
                        info["status"] = "excluded"
                        info["history"].append(record)
                    summary["excluded"].append(list(key))
                else:
                    record = {
                        "type": "confirmed_good", "key": list(key),
                        "status": "watch", "assertion": None,
                        "reason": entry["reason"], "digest": digest,
                    }
                    self._append(handle, record)
                    summary["watch"].append(list(key))

            self._append(
                handle,
                {
                    "type": "audit_ingested", "digest": digest,
                    "unobserved": summary["unobserved"],
                },
            )

        return summary

    # -- direct campaign-level records ---------------------------------

    def record_fix_claimed(
        self,
        key: Sequence[str],
        *,
        source: str = "graph_success",
        round_id: str | None = None,
    ) -> dict[str, Any]:
        validated_key = _validate_key(key, owner="record_fix_claimed")
        record = {
            "type": "finding_fix_claimed", "key": list(validated_key),
            "source": source, "round_id": round_id,
        }
        with self._locked() as handle:
            state = self._fold(handle)
            info = state["keys"].get(validated_key)
            if info is None:
                raise ConvergenceLedgerError(
                    f"cannot claim a fix for unknown key {validated_key!r}"
                )
            if info["status"] in _CLOSED_STATUSES:
                raise ConvergenceLedgerError(
                    f"cannot claim a fix for key {validated_key!r}; it is "
                    f"already {info['status']!r}"
                )
            self._append(handle, record)
        return record

    def record_ruling(
        self,
        key: Sequence[str],
        *,
        disposition: str,
        statement: str,
        actor: str = "operator",
    ) -> dict[str, Any]:
        validated_key = _validate_key(key, owner="record_ruling")
        if disposition not in RULING_DISPOSITIONS:
            raise ConvergenceLedgerError(
                f"ruling disposition must be one of {sorted(RULING_DISPOSITIONS)}"
            )
        if not isinstance(statement, str) or not statement.strip():
            raise ConvergenceLedgerError("a ruling requires a non-empty statement")
        record = {
            "type": "finding_ruled", "key": list(validated_key),
            "disposition": disposition, "statement": statement, "actor": actor,
        }
        with self._locked() as handle:
            state = self._fold(handle)
            if validated_key not in state["keys"]:
                raise ConvergenceLedgerError(
                    f"cannot rule an unknown key {validated_key!r}"
                )
            self._append(handle, record)
        return record

    def record_base_rebase(self, *, note: str | None = None) -> dict[str, Any]:
        """Append the one stall-exempt event that demotes every ``fixed``
        key to ``fix_claimed`` (``base_rebase`` — the base commit moved, so
        every prior fix needs re-observation, but this is not a repair
        failure and never counts toward stall)."""

        with self._locked() as handle:
            state = self._fold(handle)
            affected = sorted(
                key for key, info in state["keys"].items()
                if info["status"] == "fixed"
            )
            record = {
                "type": "finding_reopened", "reason": _BASE_REBASE_REASON,
                "affected_keys": [list(key) for key in affected], "note": note,
            }
            self._append(handle, record)
        return record

    def record_target_amendment(
        self,
        *,
        digest: str,
        invalidation_scope: Sequence[Sequence[str]] | None,
    ) -> dict[str, Any]:
        if not isinstance(digest, str) or not digest.strip():
            raise ConvergenceLedgerError(
                "target amendment requires a non-empty digest"
            )
        scope = None
        if invalidation_scope is not None:
            scope = [
                list(_validate_key(item, owner="target_amended invalidation_scope"))
                for item in invalidation_scope
            ]
        record = {
            "type": "target_amended", "digest": digest, "invalidation_scope": scope,
        }
        with self._locked() as handle:
            self._append(handle, record)
        return record

    # -- derived views ---------------------------------------------------

    def open_set(self) -> frozenset[Key]:
        state = self._read_state()
        return frozenset(
            key for key, info in state["keys"].items()
            if info["status"] in _ACTIVE_STATUSES
        )

    def exclusion_set(self) -> frozenset[Key]:
        state = self._read_state()
        return frozenset(
            key for key, info in state["keys"].items()
            if info["status"] == "excluded"
        )

    def key_status(self, key: Sequence[str]) -> str | None:
        validated_key = _validate_key(key, owner="key_status")
        state = self._read_state()
        info = state["keys"].get(validated_key)
        return info["status"] if info else None

    def stalled_keys(self) -> frozenset[Key]:
        state = self._read_state()
        stalled: set[Key] = set()
        for key, info in state["keys"].items():
            if info["unsuccessful_repair_claims"] >= 2:
                stalled.add(key)
                continue
            pattern = [
                event["type"] for event in info["history"]
                if event["type"] in ("finding_fixed", "finding_reopened")
                and event.get("reason") not in (
                    _BASE_REBASE_REASON, _REBASE_REOBSERVATION_REASON,
                )
            ]
            for start in range(0, max(len(pattern) - 3, 0) + 1):
                if pattern[start:start + 4] == [
                    "finding_fixed", "finding_reopened",
                    "finding_fixed", "finding_reopened",
                ]:
                    stalled.add(key)
                    break
        return frozenset(stalled)

    def is_stalled(self) -> bool:
        return bool(self.stalled_keys())

    def is_blocked(self) -> bool:
        """``True`` after a ``target_amended`` record with no stated
        ``invalidation_scope``, until a later ``target_amended`` record
        states one."""

        return bool(self._read_state()["blocked"])

    def coverage_state(self) -> dict[str, str]:
        state = self._read_state()
        return dict(state["capture_cells"])

    def amendment_ratio(self) -> float:
        state = self._read_state()
        amended = sum(
            1 for info in state["keys"].values() if info["status"] == "amended"
        )
        closed = sum(
            1 for info in state["keys"].values()
            if info["status"] in _CLOSED_STATUSES
        )
        if closed == 0:
            return 0.0
        return amended / closed

    def success(self) -> bool:
        """``True`` only if the latest ingested audit left no key
        unobserved. This is the ledger's contribution to the larger
        termination predicate (``bounds-termination``); coverage, recall,
        and amendment-ratio acknowledgment are assembled by the driver."""

        state = self._read_state()
        if state["last_audit_unobserved"] is None:
            return False
        return len(state["last_audit_unobserved"]) == 0

    def records(self) -> tuple[dict[str, Any], ...]:
        state = self._read_state()
        return tuple(state["journal"])

    def key_lineage(self) -> dict[Key, tuple[dict[str, Any], ...]]:
        """Public read-only per-key lineage (em-history): every record
        touching each finding key, grouped by key, in journal order. Each
        returned record is a copy of the journal record annotated with an
        ``"ordinal"`` field -- its index into :meth:`records`, i.e. the
        when-learned order this ledger replays by.

        Adds no record kind and appends nothing; this only regroups the
        same records :meth:`records` already exposes.
        """

        grouped: dict[Key, list[dict[str, Any]]] = {}
        for ordinal, record in enumerate(self.records()):
            for key in _record_keys(record):
                grouped.setdefault(key, []).append({**record, "ordinal": ordinal})
        return {key: tuple(items) for key, items in grouped.items()}

    def _read_state(self) -> dict[str, Any]:
        with self._locked(shared=True) as handle:
            return self._fold(handle)

    # -- journal mechanics (RetryBudgetLedger / JoinConflictResolutionStore
    # durability pattern) --------------------------------------------------

    def _locked(self, shared: bool = False) -> "_Lock":
        directory = self.path.parent
        directory_was_missing = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if directory_was_missing:
            _fsync_directory(directory.parent)
        journal_was_missing = not self.path.exists()
        handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        return _Lock(handle, journal_was_missing=journal_was_missing)

    def _fold(self, handle: "_Lock") -> dict[str, Any]:
        state: dict[str, Any] = {
            "campaign": None,
            "ingested_digests": set(),
            "keys": {},
            "capture_cells": {},
            "journal": [],
            "last_audit_unobserved": None,
            "blocked": False,
        }
        handle.seek(0)
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("protocol") != self.protocol:
                    raise ValueError("unrecognized record")
                record_type = event.get("type")
                if record_type not in self.RECORD_TYPES:
                    raise ValueError("unknown record type")
                record = {
                    field: value for field, value in event.items()
                    if field != "protocol"
                }
                state["journal"].append(record)
                self._fold_one(state, record_type, record)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ConvergenceLedgerError(
                    "convergence campaign ledger journal is corrupt at line "
                    f"{line_number}; operator intervention required"
                ) from exc
        return state

    @staticmethod
    def _fold_one(
        state: dict[str, Any], record_type: str, record: Mapping[str, Any],
    ) -> None:
        keys = state["keys"]
        if record_type == "campaign_opened":
            state["campaign"] = record
        elif record_type == "finding_opened":
            key = (record["file"], record["subject"])
            keys[key] = {
                "status": "open", "history": [record],
                "unsuccessful_repair_claims": 0, "finding": record,
                "rebase_demoted": False,
            }
        elif record_type == "finding_fix_claimed":
            key = tuple(record["key"])
            info = keys.setdefault(key, {
                "status": "open", "history": [],
                "unsuccessful_repair_claims": 0, "finding": None,
                "rebase_demoted": False,
            })
            info["status"] = "fix_claimed"
            info["history"].append(record)
            info["rebase_demoted"] = False
        elif record_type == "finding_fixed":
            key = tuple(record["key"])
            info = keys[key]
            info["status"] = "fixed"
            info["history"].append(record)
        elif record_type == "finding_invalidated":
            key = tuple(record["key"])
            info = keys[key]
            info["status"] = "invalidated"
            info["history"].append(record)
        elif record_type == "finding_reopened":
            if record.get("reason") == _BASE_REBASE_REASON:
                for raw_key in record.get("affected_keys", []):
                    key = tuple(raw_key)
                    info = keys[key]
                    info["status"] = "fix_claimed"
                    info["history"].append(record)
                    info["rebase_demoted"] = True
            else:
                key = tuple(record["key"])
                info = keys[key]
                info["status"] = "open"
                info["history"].append(record)
                if record.get("reason") == _REPAIR_CLAIM_FAILED_REASON:
                    info["unsuccessful_repair_claims"] += 1
        elif record_type == "finding_ruled":
            key = tuple(record["key"])
            info = keys[key]
            disposition = record["disposition"]
            if disposition == "waive":
                info["status"] = "excluded"
            elif disposition == "amend_criterion":
                info["status"] = "amended"
            info["history"].append(record)
        elif record_type == "confirmed_good":
            key = tuple(record["key"])
            if record["status"] == "excluded":
                info = keys.setdefault(key, {
                    "status": "excluded", "history": [],
                    "unsuccessful_repair_claims": 0, "finding": None,
                    "rebase_demoted": False,
                })
                info["status"] = "excluded"
                info["history"].append(record)
        elif record_type == "capture_coverage":
            state["capture_cells"].update(record.get("cells", {}))
        elif record_type == "target_amended":
            state["blocked"] = record.get("invalidation_scope") is None
        elif record_type == "audit_ingested":
            state["ingested_digests"].add(record["digest"])
            state["last_audit_unobserved"] = [
                tuple(item) for item in record.get("unobserved", [])
            ]

    def _append(self, handle: "_Lock", event: Mapping[str, Any]) -> None:
        payload = {"protocol": self.protocol, **event}
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if handle.journal_was_missing:
            _fsync_directory(self.path.parent)
            handle.journal_was_missing = False


def _record_keys(record: Mapping[str, Any]) -> tuple[Key, ...]:
    """The finding key(s) one journal record pertains to, if any.

    Every per-key record kind carries a ``"key"`` field except the one
    ``base_rebase`` variant of ``finding_reopened``, which carries
    ``"affected_keys"`` instead (CC-01); campaign-level records
    (``campaign_opened``, ``capture_coverage``, ``target_amended``,
    ``audit_ingested``) carry neither and touch no key.
    """

    raw_key = record.get("key")
    if raw_key is not None:
        return (tuple(raw_key),)
    affected = record.get("affected_keys")
    if affected:
        return tuple(tuple(item) for item in affected)
    return ()


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class _Lock:
    def __init__(self, handle: Any, *, journal_was_missing: bool) -> None:
        self.handle = handle
        self.journal_was_missing = journal_was_missing

    def __getattr__(self, name: str) -> Any:
        return getattr(self.handle, name)

    def __iter__(self):
        return iter(self.handle)

    def __enter__(self) -> "_Lock":
        return self

    def __exit__(self, *_exc: Any) -> None:
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


# -- ingest-time validation (contracts-finding, contracts-verdicts) --------


def _validate_key(raw: Any, *, owner: str) -> Key:
    if (
        not isinstance(raw, (list, tuple))
        or len(raw) != 2
        or not all(isinstance(part, str) and part.strip() for part in raw)
    ):
        raise ConvergenceLedgerError(f"{owner} key must be a [file, subject] pair")
    return (raw[0], raw[1])


def _validate_finding(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ConvergenceLedgerError("finding must be an object")
    file = item.get("file")
    if not isinstance(file, str) or not file.strip():
        raise ConvergenceLedgerError("finding is missing a non-empty 'file'")
    subject = item.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ConvergenceLedgerError("finding is missing a non-empty 'subject'")
    required_paths = item.get("required_paths")
    if (
        not isinstance(required_paths, list) or not required_paths
        or not all(isinstance(path, str) and path.strip() for path in required_paths)
    ):
        raise ConvergenceLedgerError(
            "finding is missing non-empty 'required_paths'"
        )
    if file not in required_paths:
        raise ConvergenceLedgerError(
            "finding 'file' must be a member of 'required_paths'"
        )
    confidence = item.get("confidence")
    if confidence is not None and confidence not in ("C", "S", "C+S"):
        raise ConvergenceLedgerError("finding 'confidence' must be C, S, or C+S")
    supersedes_key = item.get("supersedes_key")
    if supersedes_key is not None:
        supersedes_key = list(
            _validate_key(supersedes_key, owner="finding supersedes_key")
        )
    for text_field in ("id", "statement", "category"):
        value = item.get(text_field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConvergenceLedgerError(
                f"finding {text_field!r} must be a non-empty string when present"
            )
    severity = item.get("severity")
    if severity is not None and severity not in FINDING_SEVERITIES:
        raise ConvergenceLedgerError(
            f"finding 'severity' must be one of {sorted(FINDING_SEVERITIES)}"
        )
    requires_disposition = item.get("requires_disposition", False)
    if not isinstance(requires_disposition, bool):
        raise ConvergenceLedgerError(
            "finding 'requires_disposition' must be a boolean"
        )
    evidence_refs = item.get("evidence_refs")
    if evidence_refs is not None and (
        not isinstance(evidence_refs, list)
        or not all(isinstance(ref, str) and ref.strip() for ref in evidence_refs)
    ):
        raise ConvergenceLedgerError(
            "finding 'evidence_refs' must be a list of non-empty strings"
        )
    source_finding_ids = item.get("source_finding_ids")
    if source_finding_ids is not None and (
        not isinstance(source_finding_ids, list)
        or not all(isinstance(sid, str) and sid.strip() for sid in source_finding_ids)
    ):
        raise ConvergenceLedgerError(
            "finding 'source_finding_ids' must be a list of non-empty strings"
        )
    return {
        "file": file,
        "subject": subject,
        "required_paths": list(required_paths),
        "confidence": confidence,
        "supersedes_key": supersedes_key,
        "id": item.get("id"),
        "statement": item.get("statement"),
        "category": item.get("category"),
        "severity": severity,
        "requires_disposition": requires_disposition,
        "evidence_refs": list(evidence_refs or []),
        "source_finding_ids": list(source_finding_ids or []),
    }


def _validate_verdict(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ConvergenceLedgerError("verdict must be an object")
    kind = item.get("verdict")
    if kind not in VERDICT_KINDS:
        raise ConvergenceLedgerError(f"verdict has an invalid kind: {kind!r}")
    key = _validate_key(item.get("key"), owner="verdict")
    result: dict[str, Any] = {"key": key, "verdict": kind}
    if kind == "observed_fixed":
        cell = item.get("capture_cell")
        if not isinstance(cell, str) or not cell.strip():
            raise ConvergenceLedgerError(
                "an observed_fixed verdict must cite a non-empty capture_cell"
            )
        assertion = item.get("assertion")
        if not isinstance(assertion, str) or not assertion.strip():
            raise ConvergenceLedgerError(
                "an observed_fixed verdict must cite the assertion evaluated"
            )
        result["capture_cell"] = cell
        result["assertion"] = assertion
    return result


def _validate_confirmed_good(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ConvergenceLedgerError("confirmed_good entry must be an object")
    key = _validate_key(item.get("key"), owner="confirmed_good")
    assertion = item.get("assertion")
    machine_checkable = (
        isinstance(assertion, Mapping)
        and assertion.get("kind") in _CONFIRMED_GOOD_ASSERTION_KINDS
        and isinstance(assertion.get("referent"), str)
        and bool(assertion.get("referent").strip())
    )
    reason = item.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ConvergenceLedgerError("confirmed_good 'reason' must be a string")
    return {
        "key": key,
        "assertion": dict(assertion) if machine_checkable else None,
        "machine_checkable": machine_checkable,
        "reason": reason,
    }


def _validate_capture_coverage(raw: Mapping[str, Any]) -> dict[str, str]:
    coverage: dict[str, str] = {}
    for cell_id, status in raw.items():
        if not isinstance(cell_id, str) or not cell_id.strip():
            raise ConvergenceLedgerError(
                "capture_coverage cell id must be a non-empty string"
            )
        if status not in CAPTURE_CELL_STATUSES:
            raise ConvergenceLedgerError(
                f"capture_coverage status for {cell_id!r} must be one of "
                f"{sorted(CAPTURE_CELL_STATUSES)}"
            )
        coverage[cell_id] = status
    return coverage
