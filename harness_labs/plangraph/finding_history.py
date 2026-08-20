"""Repo-scoped finding history (EM-2 / em-history).

Folds caller-declared ``(journal_path, campaign_label, repository_id)``
entries through :mod:`harness_labs.plangraph.convergence_ledger`'s public
read surface only — its :meth:`~ConvergenceLedger.key_lineage` accessor,
:meth:`~ConvergenceLedger.key_status`, :meth:`~ConvergenceLedger.records`,
and its named ``RECORD_KIND_*`` constants. This module holds no
ledger-parsing logic of its own and no record-kind string literal.

Campaigns record neither a ``repository_id`` nor a campaign id in their
journal (the checkpoint, not the journal, carries ``campaign_id``), so both
are declared per entry by the caller, who alone knows them. The journal
ordinal is the when-learned order; the ``campaign_opened`` record's
``base_commit`` is the when-true-in-repo anchor. That pair is the entire
bitemporal commitment of this module.

Folding never writes: a missing journal path raises before any
:class:`~harness_labs.plangraph.convergence_ledger.ConvergenceLedger` is
constructed (the ledger's own ``open`` would create a missing journal file).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from harness_labs.plangraph.convergence_ledger import (
    ConvergenceLedger,
    RECORD_KIND_CAMPAIGN_OPENED,
    RECORD_KIND_FINDING_RULED,
)

Key = tuple[str, str]

PROTOCOL = "finding-history/1"


class FindingHistoryError(ValueError):
    """Raised on a caller-declared entry inconsistency, e.g. entries that
    disagree on ``repository_id``."""


@dataclass(frozen=True)
class Ruling:
    """One ``finding_ruled`` record folded onto a key's lineage."""

    disposition: str
    statement: str


@dataclass(frozen=True)
class KeyHistoryEntry:
    """One folded campaign's contribution to one finding key's lineage."""

    key: Key
    campaign_label: str
    repository_id: str
    base_commit: str | None
    status: str | None
    rulings: tuple[Ruling, ...]
    required_paths: tuple[str, ...]
    ordinals: tuple[int, ...]


@dataclass(frozen=True)
class FindingHistory:
    """The folded lineage of every finding key across the declared
    campaign journals, scoped to one agreed-upon ``repository_id``."""

    repository_id: str | None
    _entries: tuple[KeyHistoryEntry, ...]

    def for_key(self, file: str, subject: str) -> tuple[KeyHistoryEntry, ...]:
        """Exact-key lineage, newest declared campaign entry first."""

        key = (file, subject)
        matches = [entry for entry in self._entries if entry.key == key]
        return tuple(reversed(matches))

    def for_paths(self, paths: Sequence[str]) -> tuple[KeyHistoryEntry, ...]:
        """Findings whose recorded ``required_paths`` intersect ``paths``
        by exact match or directory-prefix containment, newest declared
        campaign entry first.

        Keys with no recorded ``required_paths`` (never opened via a
        ``finding_opened`` record — e.g. a bare ``confirmed_good`` watch
        entry) are excluded, never raised on: they remain reachable only
        through :meth:`for_key`.
        """

        matches = [
            entry
            for entry in self._entries
            if entry.required_paths
            and any(
                _path_matches(required_path, query)
                for required_path in entry.required_paths
                for query in paths
            )
        ]
        return tuple(reversed(matches))


def fold_campaigns(
    entries: Iterable[tuple[str | Path, str, str]],
) -> FindingHistory:
    """Fold caller-declared ``(journal_path, campaign_label,
    repository_id)`` entries into one :class:`FindingHistory`.

    Every declared journal path is checked to exist, and every declared
    ``repository_id`` is checked to agree, before any
    :class:`ConvergenceLedger` is constructed for any entry (``em-history``:
    folding never writes, and the ledger's own ``open`` would create a
    missing journal). A journal an opened ledger rejects surfaces its
    :class:`ConvergenceLedgerError` unchanged.
    """

    declared: list[tuple[Path, str, str]] = []
    repository_id: str | None = None
    for journal_path, campaign_label, entry_repository_id in entries:
        path = Path(journal_path)
        if not path.exists():
            raise FindingHistoryError(
                f"finding history journal path does not exist: {path}"
            )
        if repository_id is None:
            repository_id = entry_repository_id
        elif entry_repository_id != repository_id:
            raise FindingHistoryError(
                "fold_campaigns entries disagree on repository_id: "
                f"{repository_id!r} != {entry_repository_id!r}"
            )
        declared.append((path, campaign_label, entry_repository_id))

    folded: list[KeyHistoryEntry] = []
    for path, campaign_label, entry_repository_id in declared:
        ledger = ConvergenceLedger(path)
        base_commit = _base_commit(ledger)
        lineage = ledger.key_lineage()
        for key, records in lineage.items():
            folded.append(
                _fold_key_entry(
                    key=key,
                    records=records,
                    status=ledger.key_status(key),
                    campaign_label=campaign_label,
                    repository_id=entry_repository_id,
                    base_commit=base_commit,
                )
            )

    return FindingHistory(repository_id=repository_id, _entries=tuple(folded))


def _fold_key_entry(
    *,
    key: Key,
    records: Sequence[Mapping[str, object]],
    status: str | None,
    campaign_label: str,
    repository_id: str,
    base_commit: str | None,
) -> KeyHistoryEntry:
    rulings = tuple(
        Ruling(disposition=str(record["disposition"]), statement=str(record["statement"]))
        for record in records
        if record.get("type") == RECORD_KIND_FINDING_RULED
    )
    required_paths: tuple[str, ...] = ()
    for record in records:
        raw_paths = record.get("required_paths")
        if raw_paths:
            required_paths = tuple(raw_paths)  # type: ignore[arg-type]
            break
    ordinals = tuple(record["ordinal"] for record in records)  # type: ignore[misc]
    return KeyHistoryEntry(
        key=key,
        campaign_label=campaign_label,
        repository_id=repository_id,
        base_commit=base_commit,
        status=status,
        rulings=rulings,
        required_paths=required_paths,
        ordinals=ordinals,
    )


def _base_commit(ledger: ConvergenceLedger) -> str | None:
    for record in ledger.records():
        if record.get("type") == RECORD_KIND_CAMPAIGN_OPENED:
            base_commit = record.get("base_commit")
            return str(base_commit) if base_commit is not None else None
    return None


def _path_matches(required_path: str, query: str) -> bool:
    """Exact match or directory-prefix containment, guarded against bare
    string-prefix false positives (``pkg/sub`` must not match
    ``pkg/subx/mod.py``) by comparing path components, not characters."""

    required_parts = tuple(part for part in required_path.split("/") if part)
    query_parts = tuple(part for part in query.split("/") if part)
    if required_parts == query_parts:
        return True
    return (
        len(required_parts) > len(query_parts)
        and required_parts[: len(query_parts)] == query_parts
    )
