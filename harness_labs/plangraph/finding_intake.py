"""Finding intake (DTR-FI): free-text statements -> contract-valid findings.

Turns an operator's free-text statement into a finding envelope that
round-trips :meth:`ConvergenceLedger.ingest_audit` unchanged, or -- when the
statement's ownership is ambiguous -- an :class:`IntakeQuestion` naming the
candidates. Ambiguity is never resolved by guessing (``dtr-risks``): every
backtick-quoted term in the statement is resolved, and a statement whose
terms match zero files, more than one disjoint file for a single term, or
disjoint files across different terms always produces a question, never a
picked winner.

``draft_finding`` roots a statement's ``required_paths`` by searching the
working tree for the ``def``/``class`` that owns the backtick-quoted term
(or the literal path it names); the owning file becomes both ``file`` and
the sole member of ``required_paths``. Sealing (:func:`seal_findings`) never
folds through :meth:`ConvergenceLedger.ingest_audit` -- it only builds the
sealed audit-artifact envelope and hands it to
:meth:`CampaignArtifactStore.seal` for the next round's real measure/ingest
path to consume.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness_labs.core.controller_results import FINDING_SEVERITIES
from harness_labs.plangraph.convergence_campaign import (
    ArtifactRecord,
    CampaignArtifactStore,
)

_SEARCH_EXTENSIONS = (".py",)
_EXCLUDED_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist",
    ".pytest_cache", ".mypy_cache",
})
_QUOTED_TERM_RE = re.compile(r"`([^`]+)`")
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class FindingIntakeError(Exception):
    """Raised when a batch cannot be fully drafted, or sealing is misused."""


@dataclass(frozen=True)
class IntakeQuestion:
    """Ambiguity the intake pipeline refuses to resolve by guessing."""

    statement: str
    reason: str
    candidates: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class DraftFinding:
    """A contract-valid finding, fields matching the ledger's twelve-field
    ``_validate_finding`` envelope exactly."""

    file: str
    subject: str
    required_paths: tuple[str, ...]
    confidence: str
    supersedes_key: tuple[str, str] | None
    id: str
    statement: str
    category: str
    severity: str | None
    requires_disposition: bool
    evidence_refs: tuple[str, ...] = ()
    source_finding_ids: tuple[str, ...] = field(default_factory=tuple)

    def to_envelope(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "subject": self.subject,
            "required_paths": list(self.required_paths),
            "confidence": self.confidence,
            "supersedes_key": list(self.supersedes_key) if self.supersedes_key else None,
            "id": self.id,
            "statement": self.statement,
            "category": self.category,
            "severity": self.severity,
            "requires_disposition": self.requires_disposition,
            "evidence_refs": list(self.evidence_refs),
            "source_finding_ids": list(self.source_finding_ids),
        }


def draft_finding(
    statement: str,
    *,
    repo_root: Path,
    target: str,
    evidence_refs: Sequence[str] = (),
    severity: str | None = "major",
    supersedes_key: Sequence[str] | None = None,
    source_finding_ids: Sequence[str] = (),
    requires_disposition: bool | None = None,
) -> DraftFinding | IntakeQuestion:
    """Root-cause ``statement`` against ``repo_root`` and draft a finding.

    ``target`` is the subsystem/product label under audit; it is recorded
    verbatim as the finding's ``category``. Ambiguity -- zero or more than
    one disjoint owning file, whether from a single backtick-quoted term or
    from multiple terms in the same statement resolving to different files
    -- is returned as an :class:`IntakeQuestion`, never guessed.

    ``requires_disposition`` defaults to ``not bool(evidence_refs)`` (an
    evidence-less report is a judgment call pending operator disposition),
    but the two are independent: pass ``requires_disposition`` explicitly to
    mark a judgment call that happens to carry capture evidence, or an
    evidence-less statement that is already a confirmed observation.
    """

    if not isinstance(statement, str) or not statement.strip():
        raise FindingIntakeError("statement must be a non-empty string")
    if not isinstance(target, str) or not target.strip():
        raise FindingIntakeError("target must be a non-empty string")
    if severity is not None and severity not in FINDING_SEVERITIES:
        raise FindingIntakeError(
            f"severity must be one of {sorted(FINDING_SEVERITIES)} or None"
        )
    evidence_refs = tuple(evidence_refs)
    source_finding_ids = tuple(source_finding_ids)
    for label, values in (
        ("evidence_refs", evidence_refs),
        ("source_finding_ids", source_finding_ids),
    ):
        if not all(isinstance(v, str) and v.strip() for v in values):
            raise FindingIntakeError(f"{label} entries must be non-empty strings")
    if supersedes_key is not None:
        supersedes_key = tuple(supersedes_key)
        if len(supersedes_key) != 2 or not all(
            isinstance(part, str) and part.strip() for part in supersedes_key
        ):
            raise FindingIntakeError(
                "supersedes_key must be a [file, subject] pair of non-empty strings"
            )

    repo_root = Path(repo_root)
    queries = _extract_queries(statement)
    if not queries:
        return IntakeQuestion(
            statement=statement,
            reason="no backtick-quoted term to search the working tree for",
        )

    matched: list[tuple[str, str, bool]] = []
    for term in queries:
        candidates, via_path = _resolve_candidates(repo_root, term)
        if len(candidates) > 1:
            return IntakeQuestion(
                statement=statement,
                reason=(
                    f"`{term}` matches {len(candidates)} disjoint files in "
                    "the working tree"
                ),
                candidates=tuple((path,) for path in candidates),
            )
        if candidates:
            matched.append((term, candidates[0], via_path))

    if not matched:
        return IntakeQuestion(
            statement=statement,
            reason=f"no file in the working tree matches any of {queries}",
        )

    distinct_files = sorted({owning_file for _, owning_file, _ in matched})
    if len(distinct_files) > 1:
        return IntakeQuestion(
            statement=statement,
            reason=(
                f"backtick-quoted terms {[term for term, _, _ in matched]} "
                f"resolve to {len(distinct_files)} disjoint files in the "
                "working tree"
            ),
            candidates=tuple((path,) for path in distinct_files),
        )

    query, owning_file, via_path = matched[0]
    subject = query if not via_path else _slugify(statement.replace(f"`{query}`", ""))
    has_evidence = bool(evidence_refs)
    disposition = (not has_evidence) if requires_disposition is None else requires_disposition

    return DraftFinding(
        file=owning_file,
        subject=subject,
        required_paths=(owning_file,),
        confidence="C+S" if has_evidence else "S",
        supersedes_key=supersedes_key,
        id=_derive_id(statement, target, owning_file, subject),
        statement=statement,
        category=target,
        severity=severity,
        requires_disposition=disposition,
        evidence_refs=evidence_refs,
        source_finding_ids=source_finding_ids,
    )


def draft_findings_batch(
    statements: Sequence[str],
    *,
    repo_root: Path,
    target: str,
    evidence_refs_by_index: Mapping[int, Sequence[str]] | None = None,
    source_finding_ids_by_index: Mapping[int, Sequence[str]] | None = None,
    requires_disposition_by_index: Mapping[int, bool] | None = None,
) -> tuple[DraftFinding, ...]:
    """Transcribe a seed-audit's statements into findings, all-or-nothing.

    A single ambiguous statement raises :class:`FindingIntakeError` naming
    it, rather than sealing a partial batch (the ambiguous statement would
    otherwise be silently dropped and the round would look complete). The
    same applies when two statements derive the same ``(file, subject)``
    key: silently re-ingesting it would be a ledger no-op
    (``ConvergenceLedger`` treats an already-open key re-emitted with no
    status change as a no-op), so the second statement's text would never
    be journaled anywhere -- the batch is rejected instead of dropping it.
    """

    if not statements:
        raise FindingIntakeError("batch must contain at least one statement")

    evidence_by_index = evidence_refs_by_index or {}
    source_ids_by_index = source_finding_ids_by_index or {}
    disposition_by_index = requires_disposition_by_index or {}
    drafted: list[DraftFinding] = []
    seen_keys: dict[tuple[str, str], int] = {}
    for index, statement in enumerate(statements):
        result = draft_finding(
            statement,
            repo_root=repo_root,
            target=target,
            evidence_refs=evidence_by_index.get(index, ()),
            source_finding_ids=source_ids_by_index.get(index, ()),
            requires_disposition=disposition_by_index.get(index),
        )
        if isinstance(result, IntakeQuestion):
            raise FindingIntakeError(
                f"batch statement {index} is ambiguous: {result.reason} "
                f"({statement!r})"
            )
        key = (result.file, result.subject)
        if key in seen_keys:
            raise FindingIntakeError(
                f"batch statement {index} derives the same key {key!r} as "
                f"batch statement {seen_keys[key]}; merge the two operator "
                f"statements before intake instead of silently dropping one "
                f"({statement!r})"
            )
        seen_keys[key] = index
        drafted.append(result)
    return tuple(drafted)


def seal_findings(
    findings: Sequence[DraftFinding],
    store: CampaignArtifactStore,
    *,
    digest: str | None = None,
) -> ArtifactRecord:
    """Build the sealed audit-artifact envelope and seal it via ``store``.

    Never calls ``ConvergenceLedger.ingest_audit``: the envelope is carried
    for the next round's real measure/ingest path, not folded now. The
    envelope digest defaults to a content hash of the findings themselves,
    so a byte-identical rerun produces byte-identical envelope bytes and
    ``store.seal`` (content-addressed) is a true no-op.
    """

    if not findings:
        raise FindingIntakeError("seal_findings requires at least one finding")

    payload = [finding.to_envelope() for finding in findings]
    envelope_digest = digest or _compute_digest(payload)
    envelope = {
        "digest": envelope_digest,
        "findings": payload,
        "verdicts": [],
        "confirmed_good": [],
        "capture_coverage": {},
    }
    canonical_bytes = (
        json.dumps(envelope, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", suffix=".json", delete=False
        ) as handle:
            handle.write(canonical_bytes)
            temp_path = Path(handle.name)
        return store.seal(temp_path, media_type="application/json", retention="campaign")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _extract_queries(statement: str) -> list[str]:
    seen: list[str] = []
    for match in _QUOTED_TERM_RE.finditer(statement):
        term = match.group(1).strip()
        if term and term not in seen:
            seen.append(term)
    return seen


def _resolve_candidates(repo_root: Path, query: str) -> tuple[tuple[str, ...], bool]:
    """Returns ``(candidate_files, resolved_via_explicit_path)``."""

    looks_like_path = "/" in query or query.endswith(_SEARCH_EXTENSIONS)
    if looks_like_path and (repo_root / query).is_file():
        return (query,), True

    symbol = query.rsplit(".", 1)[-1] if "." in query else query
    if not _SYMBOL_RE.match(symbol):
        return (), False

    definition_re = re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}\b", re.MULTILINE
    )
    matches = [
        path for path in _iter_source_files(repo_root)
        if definition_re.search(_read_text(repo_root / path))
    ]
    if matches:
        return tuple(sorted(matches)), False

    word_re = re.compile(rf"\b{re.escape(symbol)}\b")
    matches = [
        path for path in _iter_source_files(repo_root)
        if word_re.search(_read_text(repo_root / path))
    ]
    return tuple(sorted(matches)), False


def _iter_source_files(repo_root: Path) -> list[str]:
    results: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix not in _SEARCH_EXTENSIONS:
            continue
        relative = path.relative_to(repo_root)
        if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts):
            continue
        results.append(relative.as_posix())
    return results


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _slugify(text: str, limit: int = 8) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "-".join(words[:limit]) or "statement"


def _derive_id(statement: str, target: str, file: str, subject: str) -> str:
    digest = hashlib.sha256(
        "\x00".join((statement, target, file, subject)).encode("utf-8")
    ).hexdigest()
    return f"fi-{digest[:16]}"


def _compute_digest(findings_payload: list[dict[str, Any]]) -> str:
    canonical = json.dumps(findings_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
