"""Decision registry: load ADR headers and schema-1.1 JSON decision records
into one `Decision` shape, and answer "what governs this path" queries.

Two decision-record shapes are read here, both conforming to
`schemas/decision.schema.json`: ADR markdown header blocks under
`docs/decisions/`, and standalone JSON records. `controller_kernel`'s
decision-record shape (`{"id", "question", "choice", ...}`, no
`schema_version`) is a different, smaller contract and is never ingested —
files lacking `schema_version` and `decision_id` are silently skipped as
JSON candidates rather than treated as malformed decision records.

Core layer: imports core and the standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_HEADER_FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s*(.*)$")
_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_ADR_FILENAME_RE = re.compile(r"^\d{4}-.+\.md$")

_JSON_DECISION_ID_KEY = "decision_id"
_JSON_SCHEMA_VERSION_KEY = "schema_version"
_KNOWN_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})


class DecisionRegistryError(ValueError):
    """Raised when a decision record cannot be parsed as declared."""


@dataclass(frozen=True)
class Decision:
    """One normalized decision, sourced from an ADR header or a JSON record."""

    id: str
    status: str
    supersedes: tuple[str, ...]
    concerns_paths: tuple[str, ...]
    valid_from_commit: str | None
    source_path: str


@dataclass(frozen=True)
class Inconsistency:
    """An explicit, unresolved status/supersedes contradiction.

    Decision `superseded_id` carries `status: accepted` while decision
    `superseding_id` lists it in `supersedes`. Neither side is silently
    trusted when this is present.
    """

    superseded_id: str
    superseding_id: str


@dataclass(frozen=True)
class DecisionQueryResult:
    """Result of `active_decisions_for_paths`: the active decisions and any
    status/supersedes contradictions found among the loaded decisions."""

    active: tuple[Decision, ...]
    inconsistencies: tuple[Inconsistency, ...]


def _parse_header_block(text: str) -> dict[str, str]:
    """Parse `Key: value` lines, folding non-`Key:` continuation lines into
    the previous field's value (tolerant of wrapped multi-line values)."""

    heading_match = _HEADING_RE.search(text)
    header_text = text[: heading_match.start()] if heading_match else text
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in header_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _HEADER_FIELD_RE.match(line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            fields[key] = value
            current_key = key
        elif current_key is not None:
            fields[current_key] = f"{fields[current_key]} {line}".strip()
        # Lines before the first recognized field (e.g. the "# NNNN — Title"
        # heading) are preamble, not a continuation, and are discarded.
    return fields


def _split_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _decision_from_adr(path: Path) -> Decision:
    text = path.read_text(encoding="utf-8")
    fields = _parse_header_block(text)
    decision_id = path.stem
    return Decision(
        id=decision_id,
        status=fields.get("Status", "").strip(),
        supersedes=_split_list(fields.get("Supersedes")),
        concerns_paths=_split_list(fields.get("Concerns-paths")),
        valid_from_commit=(fields.get("Valid-from-commit") or "").strip() or None,
        source_path=str(path),
    )


def _decision_from_json_record(path: Path, record: dict[str, object]) -> Decision | None:
    schema_version = record.get(_JSON_SCHEMA_VERSION_KEY)
    decision_id = record.get(_JSON_DECISION_ID_KEY)
    status = record.get("status")
    if schema_version not in _KNOWN_SCHEMA_VERSIONS:
        return None
    if not isinstance(decision_id, str) or not decision_id:
        return None
    if not isinstance(status, str) or not status:
        return None
    supersedes = record.get("supersedes") or []
    concerns_paths = record.get("concerns_paths") or []
    valid_from_commit = record.get("valid_from_commit")
    if not isinstance(supersedes, list) or not all(isinstance(v, str) for v in supersedes):
        raise DecisionRegistryError(f"{path}: supersedes must be a list of strings")
    if not isinstance(concerns_paths, list) or not all(
        isinstance(v, str) for v in concerns_paths
    ):
        raise DecisionRegistryError(f"{path}: concerns_paths must be a list of strings")
    if valid_from_commit is not None and not isinstance(valid_from_commit, str):
        raise DecisionRegistryError(f"{path}: valid_from_commit must be a string")
    return Decision(
        id=decision_id,
        status=status,
        supersedes=tuple(supersedes),
        concerns_paths=tuple(concerns_paths),
        valid_from_commit=valid_from_commit,
        source_path=str(path),
    )


def _covers_by_prefix(concerns_path: str, queried_path: str) -> bool:
    concerns_parts = [p for p in concerns_path.split("/") if p]
    queried_parts = [p for p in queried_path.split("/") if p]
    if len(concerns_parts) > len(queried_parts):
        return False
    return queried_parts[: len(concerns_parts)] == concerns_parts


class DecisionRegistry:
    """A loaded, queryable set of `Decision`s. Construct via `load_decisions`."""

    def __init__(self, decisions: tuple[Decision, ...]) -> None:
        self.decisions = decisions

    def active_decisions_for_paths(self, paths: tuple[str, ...]) -> DecisionQueryResult:
        """Accepted decisions covering any of `paths` by directory-prefix
        intersection against `concerns_paths`, excluding decisions named in
        another loaded decision's `supersedes` — except where doing so would
        silently resolve a contradiction (a decision named in `supersedes`
        that itself still carries `status: accepted`): that case is surfaced
        as an `Inconsistency` instead, and the named decision is excluded
        from both the active set and any silently-superseded set.
        """

        by_id = {decision.id: decision for decision in self.decisions}
        superseded_by: dict[str, list[str]] = {}
        for decision in self.decisions:
            for superseded_id in decision.supersedes:
                superseded_by.setdefault(superseded_id, []).append(decision.id)

        inconsistencies: list[Inconsistency] = []
        contradictory_ids: set[str] = set()
        for superseded_id, superseding_ids in superseded_by.items():
            superseded_decision = by_id.get(superseded_id)
            if superseded_decision is None:
                continue
            if superseded_decision.status == "accepted":
                for superseding_id in superseding_ids:
                    inconsistencies.append(
                        Inconsistency(
                            superseded_id=superseded_id,
                            superseding_id=superseding_id,
                        )
                    )
                contradictory_ids.add(superseded_id)

        excluded_ids = set(superseded_by) - contradictory_ids

        active: list[Decision] = []
        for decision in self.decisions:
            if decision.status != "accepted":
                continue
            if decision.id in excluded_ids or decision.id in contradictory_ids:
                continue
            if any(
                _covers_by_prefix(concerns_path, path)
                for concerns_path in decision.concerns_paths
                for path in paths
            ):
                active.append(decision)

        return DecisionQueryResult(
            active=tuple(active), inconsistencies=tuple(inconsistencies)
        )


def load_decisions(directory: str | Path) -> DecisionRegistry:
    """Load every ADR header block and schema-1.1-conforming JSON decision
    record directly inside `directory` (non-recursive) into a `DecisionRegistry`.

    ADR ids are synthesized from filenames (the stem, e.g.
    `0006-parallel-plangraph-contract`), so two ADRs numbered identically
    but named differently load as distinct decisions. JSON records conforming
    to `decision.schema.json` (declaring `schema_version` in {"1.0", "1.1"}
    and a non-empty `decision_id`/`status`) use their own `decision_id`.
    Any other JSON file — including a `controller_kernel`-shaped decision
    record, which has no `schema_version` field — is skipped, not ingested.
    """

    root = Path(directory)
    decisions: list[Decision] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if path.suffix == ".md" and _ADR_FILENAME_RE.match(path.name):
            decisions.append(_decision_from_adr(path))
        elif path.suffix == ".json":
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            decision = _decision_from_json_record(path, record)
            if decision is not None:
                decisions.append(decision)
    return DecisionRegistry(tuple(decisions))
