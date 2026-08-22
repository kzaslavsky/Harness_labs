"""Improvement program (SI-04): the graphrun-layer composition that turns a
qualifying ``blocker-pattern/1`` record into an operator-facing
``improvement-proposal/1`` draft.

This is the only place the engineering-memory join is legal (``harness_labs.
plangraph.finding_history`` is a plangraph module observability may not
import): every pattern a drafted proposal touches is annotated with the
prior-ruled lineage :func:`~harness_labs.plangraph.finding_history.
fold_campaigns` recalls for its ``target_surface`` paths, and with the
governing decisions :func:`~harness_labs.core.decision_registry.
load_decisions` recalls for the same paths. A draft that touches a governed
path without citing the governing decision, or that cannot fill the
Complexity-admission triple or at least one executable success-criterion
assertion, is refused rather than emitted -- an unfalsifiable proposal must
never reach the operator.

Drafting judgment is model work, but it is injected: :func:`draft_proposal`
takes a caller-supplied :data:`JudgmentCallable`, so every test in
``tests/test_improvement_program.py`` runs deterministically with no live
model, in the style of the inspector-injection pattern in the convergence
tests. The judgment callable proposes; only :func:`apply_ruling` -- fed a
human-authored actor and statement -- can move a proposal to ``accepted``.
Rulings are never machine-authored.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Mapping, Sequence

from harness_labs.core.decision_registry import DecisionRegistry, Inconsistency
from harness_labs.plangraph.finding_history import FindingHistory

PROTOCOL = "improvement-proposal/1"

#: Paths that force ``accuracy_risk: gate_relaxation`` regardless of the
#: ``kind`` a draft assigns them (SI-04: "Proposals targeting AGENTS.md,
#: docs/architecture/harness-contract.md, or any accuracy gate carry
#: accuracy_risk: gate_relaxation").
GATE_FORCING_PATHS = frozenset(
    {"AGENTS.md", "docs/architecture/harness-contract.md"}
)

RULING_DISPOSITIONS = frozenset({"accept", "reject", "waive"})

#: Mechanized proxy for "names the superseding mechanism" (burden-admission/1
#: clause 2): the ruling statement must use one of these marker phrases
#: followed by non-empty text naming the mechanism.
_SUPERSEDE_MARKERS = ("superseded by", "supersedes", "superseding mechanism")

_RULING_STATUS_BY_DISPOSITION = {
    "accept": "accepted",
    "reject": "rejected",
    "waive": "rejected",
}


class ImprovementProgramError(ValueError):
    """Base error for this module."""


class ProposalRefused(ImprovementProgramError):
    """The drafter declined to emit a proposal; ``.reason`` names why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RulingError(ImprovementProgramError):
    """A ruling failed human-authorship or gate-relaxation validation."""


# -- draft input shape (the injectable judgment callable's return type) -----


@dataclass(frozen=True)
class TargetSurfaceDraft:
    path: str
    kind: str


@dataclass(frozen=True)
class SuccessCriterionDraft:
    file: str
    subject: str
    required_paths: tuple[str, ...]
    statement: str
    assertion: Mapping[str, object]


@dataclass(frozen=True)
class ProposalDraft:
    """One model-authored draft, prior to the EM joins and refusal checks.

    The injected judgment callable returns this, or ``None`` to decline
    outright (e.g. the pattern's generalizability verdict does not warrant a
    proposal).
    """

    question: str
    choice: str
    alternatives: tuple[str, ...]
    rationale: str
    evidence: tuple[str, ...]
    consequences: tuple[str, ...]
    reversible: bool
    demonstrated_failure: str | None
    production_consumer: str | None
    end_to_end_assertion: str | None
    target_surface: tuple[TargetSurfaceDraft, ...]
    accuracy_risk: str
    success_criteria: tuple[SuccessCriterionDraft, ...]
    rollback: str


JudgmentCallable = Callable[[Mapping[str, object]], "ProposalDraft | None"]


# -- engineering-memory join outputs -----------------------------------------


@dataclass(frozen=True)
class RecurrenceEntry:
    """One prior campaign's ruling on a key touching a proposal's paths.

    ``source``/``key``/``ref`` mirror ``blocker-pattern.schema.json``'s
    ``recurrence`` entry shape (see :meth:`to_schema_dict`); ``campaign_label``,
    ``disposition``, and ``statement`` are the richer lineage the ruling
    packet surfaces to the operator (AC-SI04-1).
    """

    source: str
    key: str
    ref: str
    campaign_label: str
    disposition: str
    statement: str

    def to_schema_dict(self) -> dict[str, str]:
        return {"source": self.source, "key": self.key, "ref": self.ref}


@dataclass(frozen=True)
class RulingPacket:
    """Everything the operator needs to rule on a proposal: pattern
    evidence, cost aggregate, recurrence lineage, governing decisions, and
    any unresolved registry inconsistencies -- never resolved silently."""

    pattern_id: str
    cost_aggregate: Mapping[str, object]
    recurrence: tuple[RecurrenceEntry, ...]
    governing_decisions: Mapping[str, tuple[str, ...]]
    registry_inconsistencies: tuple[dict[str, str], ...]
    candidate_dispositions: tuple[str, ...]


@dataclass(frozen=True)
class Proposal:
    """A drafted ``improvement-proposal/1`` record plus its ruling packet."""

    proposal_id: str
    pattern_ids: tuple[str, ...]
    question: str
    choice: str
    alternatives: tuple[str, ...]
    rationale: str
    evidence: tuple[str, ...]
    consequences: tuple[str, ...]
    reversible: bool
    status: str
    demonstrated_failure: str
    production_consumer: str
    end_to_end_assertion: str
    target_surface: tuple[dict[str, object], ...]
    accuracy_risk: str
    success_criteria: tuple[dict[str, object], ...]
    rollback: str
    ruling: dict[str, object] | None
    ruling_packet: RulingPacket

    def to_dict(self) -> dict[str, object]:
        """Project to the ``improvement-proposal/1`` wire shape (the ruling
        packet is operator tooling, not part of the committed schema)."""

        return {
            "protocol": PROTOCOL,
            "proposal_id": self.proposal_id,
            "pattern_ids": list(self.pattern_ids),
            "question": self.question,
            "choice": self.choice,
            "alternatives": list(self.alternatives),
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "consequences": list(self.consequences),
            "reversible": self.reversible,
            "status": self.status,
            "demonstrated_failure": self.demonstrated_failure,
            "production_consumer": self.production_consumer,
            "end_to_end_assertion": self.end_to_end_assertion,
            "target_surface": [dict(entry) for entry in self.target_surface],
            "accuracy_risk": self.accuracy_risk,
            "success_criteria": [dict(entry) for entry in self.success_criteria],
            "rollback": self.rollback,
            "ruling": dict(self.ruling) if self.ruling else None,
        }


# -- engineering-memory joins -------------------------------------------------


def join_recurrence(
    paths: Sequence[str], *, history: FindingHistory
) -> tuple[RecurrenceEntry, ...]:
    """Annotate ``paths`` with prior ruled keys via ``for_paths`` path
    containment: one :class:`RecurrenceEntry` per ruling folded onto a key
    whose recorded ``required_paths`` intersects ``paths``, newest-campaign
    lineage first (as :meth:`FindingHistory.for_paths` already orders it)."""

    entries: list[RecurrenceEntry] = []
    for lineage_entry in history.for_paths(paths):
        file_, subject = lineage_entry.key
        key_label = f"{file_}::{subject}"
        for ruling in lineage_entry.rulings:
            entries.append(
                RecurrenceEntry(
                    source="finding_history",
                    key=key_label,
                    ref=lineage_entry.base_commit or lineage_entry.campaign_label,
                    campaign_label=lineage_entry.campaign_label,
                    disposition=ruling.disposition,
                    statement=ruling.statement,
                )
            )
    return tuple(entries)


def annotate_pattern_with_recurrence(
    pattern: Mapping[str, object], paths: Sequence[str], *, history: FindingHistory
) -> dict[str, object]:
    """Return a copy of ``pattern`` with its ``recurrence`` field (``blocker-
    pattern.schema.json``) populated from :func:`join_recurrence` over
    ``paths``, projected to the schema's ``{source, key, ref}`` shape.

    Callable directly against any candidate pattern -- independent of
    :func:`draft_proposal` and its refusal gates -- so a pattern that never
    yields an emitted proposal is still annotated with its prior-ruled
    lineage (AC-SI04-1)."""

    recurrence = join_recurrence(paths, history=history)
    return {**pattern, "recurrence": [entry.to_schema_dict() for entry in recurrence]}


def join_governing_decisions(
    paths: Sequence[str], *, registry: DecisionRegistry
) -> tuple[dict[str, tuple[str, ...]], tuple[Inconsistency, ...]]:
    """Per-path governing decision ids, plus every registry
    :class:`Inconsistency` found across the loaded decision set (surfaced
    unconditionally -- the registry computes these over its whole loaded set,
    not scoped to ``paths``, and this join never drops or resolves one)."""

    per_path: dict[str, tuple[str, ...]] = {}
    for path in paths:
        result = registry.active_decisions_for_paths((path,))
        per_path[path] = tuple(sorted(decision.id for decision in result.active))
    inconsistencies = registry.active_decisions_for_paths(tuple(paths)).inconsistencies
    return per_path, inconsistencies


# -- bounded, injectable drafting --------------------------------------------


def _complexity_admission_complete(draft: ProposalDraft) -> bool:
    return all(
        isinstance(value, str) and value.strip()
        for value in (
            draft.demonstrated_failure,
            draft.production_consumer,
            draft.end_to_end_assertion,
        )
    )


def _is_executable_assertion(assertion: Mapping[str, object]) -> bool:
    """Mirrors ``improvement-proposal.schema.json``'s executable-assertion
    branch (``$defs.success_criterion.assertion.oneOf[0]``): key presence
    alone is not enough -- ``argv`` must be a non-empty list of non-empty
    strings and ``timeout_seconds`` a number strictly greater than zero, or
    the assertion cannot actually execute."""

    argv = assertion.get("argv")
    timeout_seconds = assertion.get("timeout_seconds")
    return (
        isinstance(argv, (list, tuple))
        and len(argv) >= 1
        and all(isinstance(item, str) and item.strip() for item in argv)
        and isinstance(timeout_seconds, (int, float))
        and not isinstance(timeout_seconds, bool)
        and timeout_seconds > 0
    )


def _has_executable_success_criterion(draft: ProposalDraft) -> bool:
    return any(
        _is_executable_assertion(criterion.assertion)
        for criterion in draft.success_criteria
    )


def _citation_text(draft: ProposalDraft) -> str:
    return " ".join((draft.rationale, *draft.evidence))


def _uncited_governed_paths(
    draft: ProposalDraft, governing_by_path: Mapping[str, tuple[str, ...]]
) -> list[tuple[str, tuple[str, ...]]]:
    citation_text = _citation_text(draft)
    uncited: list[tuple[str, tuple[str, ...]]] = []
    for surface in draft.target_surface:
        decisions = governing_by_path.get(surface.path, ())
        if decisions and not any(
            decision_id in citation_text for decision_id in decisions
        ):
            uncited.append((surface.path, decisions))
    return uncited


def _gate_paths(draft: ProposalDraft) -> tuple[str, ...]:
    return tuple(
        surface.path
        for surface in draft.target_surface
        if surface.kind == "gate" or surface.path in GATE_FORCING_PATHS
    )


def _candidate_dispositions(pattern: Mapping[str, object]) -> tuple[str, ...]:
    generalizability = pattern.get("generalizability") or {}
    verdict = generalizability.get("verdict") if isinstance(generalizability, Mapping) else None
    if verdict in {"one_off", "superseded_by_existing_mechanism"}:
        return ("reject", "waive")
    return ("accept", "waive")


def draft_proposal(
    pattern: Mapping[str, object],
    *,
    judgment: JudgmentCallable,
    finding_history: FindingHistory,
    decision_registry: DecisionRegistry,
    proposal_id: str,
) -> Proposal:
    """Draft one ``improvement-proposal/1`` for ``pattern``, joining
    engineering memory and refusing rather than emitting an unfalsifiable or
    uncited-governed-path proposal.

    Raises :class:`ProposalRefused` (never returns a partial ``Proposal``)
    when:

    - ``judgment(pattern)`` declines outright (returns ``None``);
    - the draft has no ``target_surface`` entries;
    - the Complexity-admission triple (``demonstrated_failure``,
      ``production_consumer``, ``end_to_end_assertion``) cannot be filled;
    - no ``success_criteria`` entry carries an executable (``argv`` +
      ``timeout_seconds``) assertion;
    - a target_surface path is governed by an active decision the draft's
      ``rationale``/``evidence`` does not cite by id;
    - a target_surface path forces ``accuracy_risk: gate_relaxation``
      (``AGENTS.md``, ``docs/architecture/harness-contract.md``, or any
      ``kind: gate`` entry) but the draft did not set it.
    """

    draft = judgment(pattern)
    if draft is None:
        raise ProposalRefused(
            "judgment declined to draft a proposal for this pattern"
        )
    if not draft.target_surface:
        raise ProposalRefused("draft has no target_surface entries")
    if not _complexity_admission_complete(draft):
        raise ProposalRefused(
            "Complexity-admission triple (demonstrated_failure, "
            "production_consumer, end_to_end_assertion) cannot be filled"
        )
    if not draft.success_criteria or not _has_executable_success_criterion(draft):
        raise ProposalRefused(
            "no executable success-criterion assertion (argv + "
            "timeout_seconds) can be filled"
        )

    paths = tuple(surface.path for surface in draft.target_surface)
    governing_by_path, inconsistencies = join_governing_decisions(
        paths, registry=decision_registry
    )

    uncited = _uncited_governed_paths(draft, governing_by_path)
    if uncited:
        detail = ", ".join(
            f"{path} (governed by {list(decisions)})" for path, decisions in uncited
        )
        raise ProposalRefused(
            f"proposal touches governed path(s) without citing the "
            f"governing decision(s): {detail}"
        )

    if _gate_paths(draft) and draft.accuracy_risk != "gate_relaxation":
        raise ProposalRefused(
            f"proposal touches gate-forcing path(s) {_gate_paths(draft)!r} "
            "but accuracy_risk is not 'gate_relaxation'"
        )

    recurrence = join_recurrence(paths, history=finding_history)

    target_surface_dicts = tuple(
        {
            "path": surface.path,
            "kind": surface.kind,
            "governing_decisions": list(governing_by_path.get(surface.path, ())),
        }
        for surface in draft.target_surface
    )
    success_criteria_dicts = tuple(
        {
            "file": criterion.file,
            "subject": criterion.subject,
            "required_paths": list(criterion.required_paths),
            "statement": criterion.statement,
            "assertion": dict(criterion.assertion),
        }
        for criterion in draft.success_criteria
    )

    ruling_packet = RulingPacket(
        pattern_id=str(pattern.get("pattern_id")),
        cost_aggregate=pattern.get("cost_aggregate", {}),
        recurrence=recurrence,
        governing_decisions=governing_by_path,
        registry_inconsistencies=tuple(
            {
                "superseded_id": inconsistency.superseded_id,
                "superseding_id": inconsistency.superseding_id,
            }
            for inconsistency in inconsistencies
        ),
        candidate_dispositions=_candidate_dispositions(pattern),
    )

    return Proposal(
        proposal_id=proposal_id,
        pattern_ids=(str(pattern.get("pattern_id")),),
        question=draft.question,
        choice=draft.choice,
        alternatives=draft.alternatives,
        rationale=draft.rationale,
        evidence=draft.evidence,
        consequences=draft.consequences,
        reversible=draft.reversible,
        status="proposed",
        demonstrated_failure=draft.demonstrated_failure,
        production_consumer=draft.production_consumer,
        end_to_end_assertion=draft.end_to_end_assertion,
        target_surface=target_surface_dicts,
        accuracy_risk=draft.accuracy_risk,
        success_criteria=success_criteria_dicts,
        rollback=draft.rollback,
        ruling=None,
        ruling_packet=ruling_packet,
    )


# -- human-only ruling ---------------------------------------------------


def _names_superseding_mechanism(statement: str) -> bool:
    lowered = statement.lower()
    for marker in _SUPERSEDE_MARKERS:
        index = lowered.find(marker)
        if index == -1:
            continue
        remainder = statement[index + len(marker):].strip(" :-")
        if remainder:
            return True
    return False


def validate_gate_relaxation_ruling(
    target_surface: Sequence[Mapping[str, object]], statement: str
) -> None:
    """Reject a gate_relaxation ruling statement that does not name every
    relaxed gate and a superseding mechanism (burden-admission/1 clause 2,
    mechanized: no model judgment call here)."""

    gate_paths = [
        entry["path"]
        for entry in target_surface
        if entry.get("kind") == "gate" or entry.get("path") in GATE_FORCING_PATHS
    ]
    if not gate_paths:
        raise RulingError(
            "gate_relaxation ruling has no gate-forcing target_surface path "
            "to name as the relaxed gate"
        )
    missing_gates = [path for path in gate_paths if path not in statement]
    if missing_gates:
        raise RulingError(
            "gate_relaxation ruling statement does not name the relaxed "
            f"gate(s): {missing_gates!r}"
        )
    if not _names_superseding_mechanism(statement):
        raise RulingError(
            "gate_relaxation ruling statement does not name a superseding "
            "mechanism"
        )


def apply_ruling(
    proposal: Proposal,
    *,
    disposition: str,
    actor: str,
    statement: str,
    ruled_at: str,
) -> Proposal:
    """Fold a human ruling onto ``proposal``, returning the updated record.

    Validates the ruling is human-authored (non-empty ``actor`` and
    ``statement``) before any status change, and additionally rejects an
    ``accept`` ruling on a ``gate_relaxation`` proposal whose ``statement``
    does not name the relaxed gate and the superseding mechanism. Never
    mutates ``proposal`` in place.
    """

    if disposition not in RULING_DISPOSITIONS:
        raise RulingError(
            f"ruling disposition must be one of {sorted(RULING_DISPOSITIONS)}, "
            f"got {disposition!r}"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise RulingError("ruling actor must be a non-empty, human-authored string")
    if not isinstance(statement, str) or not statement.strip():
        raise RulingError(
            "ruling statement must be a non-empty, human-authored string"
        )
    if not isinstance(ruled_at, str) or not ruled_at.strip():
        raise RulingError("ruling ruled_at must be a non-empty string")

    if disposition == "accept" and proposal.accuracy_risk == "gate_relaxation":
        validate_gate_relaxation_ruling(proposal.target_surface, statement)

    ruling = {
        "disposition": disposition,
        "actor": actor,
        "statement": statement,
        "ruled_at": ruled_at,
    }
    return replace(
        proposal,
        status=_RULING_STATUS_BY_DISPOSITION[disposition],
        ruling=ruling,
    )
