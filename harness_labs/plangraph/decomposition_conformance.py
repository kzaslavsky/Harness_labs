"""The S1-S10 decomposition-sizing rules as an admission-time analyzer.

``docs/development/convergence-campaign-plan.md`` (``sizing-s1-s10``) defines
ten node-sizing criteria a *generated* decomposition should satisfy before an
operator reviews it. This module turns those ten criteria into one analyzer
with three enforcement grades:

* **block** (S1, S2, S4, S5, S6, S7) -- ``plan_approval.py`` refuses
  admission outright when one of these is both in scope and unoverridden.
* **warn** (S8, S10) -- surfaced through ``plan_approval.py``'s existing
  high-severity acknowledgment gate (``_require_acknowledged_high_warnings``)
  by construction: an enforced, unoverridden warn-grade finding is appended
  to ``gate-evidence.json``'s ``warnings`` array with ``severity: "high"``,
  the exact shape ``warning_identity``/the ack gate already understand.
* **proposal** (S3, S9) -- never blocks and never requires acknowledgment.
  Each is a data object describing an edit an operator (or an injected
  refinement judge -- see :func:`conformance_judge`) can apply by
  re-committing a revised decomposition. This module never mutates a
  decomposition in place.

Enforcement scope
------------------

Blocking and warn-grade enforcement apply only when the decomposition is
*conformance-aware*: :func:`is_conformance_aware` returns ``True`` exactly
when at least one entry of the payload's own ``acceptance_criteria`` mapping
carries a machine-readable observable annotation (see below). A
hand-authored legacy decomposition that never adopted the annotation stays
un-enforced -- the analysis still runs and the report is still emitted, but
nothing raises and nothing requires acknowledgment. ``analyze_decomposition``
also accepts an explicit ``enforce`` override so a caller (a future campaign
driver, for instance) can force enforcement on regardless of what the
payload declares. Analysis and report emission are unconditional: no input,
including ``enforce=False`` or an override list, can suppress either one.

The observable annotation
--------------------------

``PlanRun.criteria`` is a list of criterion IDs; the criterion *text* lives
in the plan's top-level ``acceptance_criteria`` mapping, and
``plan_graph_contract.canonical_plan_graph_payload`` accepts no top-level or
per-run keys beyond its fixed set -- there is nowhere else to attach
structured per-criterion data without breaking that contract. So S5/S6's
"machine-readable observable declaration" is carried *inside* the criterion
text itself, as a trailing ``OBSERVABLE:{...}`` JSON object recognized by
:func:`parse_observable`, e.g.::

    "the button renders blue. OBSERVABLE:{\"kind\": \"file\", \"referent\": \"app/index.html\"}"

A criterion whose text carries no such annotation, or whose annotation does
not parse into ``{"kind": <one of file|test_id|selector|command>, "referent":
<non-empty string>}``, is treated as carrying none (S5).

Overrides
---------

An override is a ``{"scope": "node"|"criterion", "target": <id>, "kind":
<one of the S*_ constants>, "reason": <non-empty string>}`` record. It
suppresses exactly the findings whose ``kind`` matches and whose subject (a
run id for ``scope="node"``, a criterion id for ``scope="criterion"``)
matches ``target`` -- nothing wider. There is no wildcard scope, target, or
kind, so no single override can disable the analyzer, or even one whole
S-rule, wholesale; every suppression is recorded, with its reason, in the
emitted report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from harness_labs.plangraph.plan_graph import PlanGraphPlan, PlanRun
from harness_labs.plangraph.plan_graph_contract import (
    declares_intent,
    path_is_allowed,
    unintended_grants,
)


CONFORMANCE_PROTOCOL = "decomposition-conformance-report/1"

GRADE_BLOCK = "block"
GRADE_WARN = "warn"
GRADE_PROPOSAL = "proposal"
GRADES = frozenset({GRADE_BLOCK, GRADE_WARN, GRADE_PROPOSAL})

#: The observable kinds S5/S6 accept -- a closed set, per the plan.
OBSERVABLE_KINDS = frozenset({"file", "test_id", "selector", "command"})

S1_WRITABLE_PATH_OVERLAP = "conformance-s1-writable-path-overlap"
S2_DIRECTORY_GRANT = "conformance-s2-directory-grant"
S3_SERIALIZATION_PROPOSAL = "conformance-s3-serialization-proposal"
S4_GRANT_OUTSIDE_OBJECTIVE = "conformance-s4-grant-outside-objective"
S5_MISSING_OBSERVABLE = "conformance-s5-missing-observable"
S6_UNREACHABLE_OBSERVABLE = "conformance-s6-unreachable-observable"
S7_EXIT_CHECK_OUTSIDE_GRANTS = "conformance-s7-exit-check-outside-grants"
S8_GATE_LARGER_THAN_CRITERIA = "conformance-s8-gate-larger-than-criteria"
S9_FAN_IN_JOIN_PROPOSAL = "conformance-s9-fan-in-join-proposal"
S10_NODE_COUNT = "conformance-s10-node-count"

_BLOCK_KINDS = frozenset(
    {
        S1_WRITABLE_PATH_OVERLAP,
        S2_DIRECTORY_GRANT,
        S4_GRANT_OUTSIDE_OBJECTIVE,
        S5_MISSING_OBSERVABLE,
        S6_UNREACHABLE_OBSERVABLE,
        S7_EXIT_CHECK_OUTSIDE_GRANTS,
    }
)
_WARN_KINDS = frozenset({S8_GATE_LARGER_THAN_CRITERIA, S10_NODE_COUNT})
_PROPOSAL_KINDS = frozenset({S3_SERIALIZATION_PROPOSAL, S9_FAN_IN_JOIN_PROPOSAL})
ALL_KINDS = _BLOCK_KINDS | _WARN_KINDS | _PROPOSAL_KINDS

MAX_FAN_IN = 3
MAX_ROUND_NODES = 8

_OVERRIDE_SCOPES = frozenset({"node", "criterion"})
_OBSERVABLE_PATTERN = re.compile(r"OBSERVABLE:(\{.*\})\s*$", re.DOTALL)


class DecompositionConformanceError(ValueError):
    """Raised when conformance inputs (overrides, a stored report) are unusable."""


# ---------------------------------------------------------------------------
# Observable annotations and conformance-awareness
# ---------------------------------------------------------------------------


def parse_observable(text: object) -> dict[str, str] | None:
    """Extract one ``{kind, referent}`` observable from criterion text, or ``None``.

    Absence and malformation are both treated as "no declaration" -- S5 does
    not distinguish a criterion that forgot the annotation from one that
    wrote it wrong, since neither leaves a machine-readable object behind.
    """

    if not isinstance(text, str):
        return None
    match = _OBSERVABLE_PATTERN.search(text)
    if match is None:
        return None
    try:
        candidate = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(candidate, Mapping):
        return None
    kind = candidate.get("kind")
    referent = candidate.get("referent")
    if kind not in OBSERVABLE_KINDS or not isinstance(referent, str) or not referent.strip():
        return None
    return {"kind": kind, "referent": referent}


def is_conformance_aware(canonical: Mapping[str, object]) -> bool:
    """Whether any criterion in the payload declares a machine-readable observable.

    This is the sole, deterministic input to enforcement scope (absent an
    explicit ``enforce`` override): a decomposition that never adopted the
    annotation convention is legacy, not generated, and stays un-enforced.
    """

    criteria = canonical.get("acceptance_criteria")
    if not isinstance(criteria, Mapping):
        return False
    return any(parse_observable(text) is not None for text in criteria.values())


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceOverride:
    scope: str
    target: str
    kind: str
    reason: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "target": self.target,
            "kind": self.kind,
            "reason": self.reason,
        }

    def matches(self, finding: "ConformanceFinding") -> bool:
        if self.kind != finding.kind:
            return False
        if self.scope == "node":
            return self.target in finding.runs
        return finding.criterion is not None and self.target == finding.criterion


def parse_overrides(
    raw: Sequence[Mapping[str, object]]
) -> tuple[ConformanceOverride, ...]:
    """Validate and normalize caller-supplied overrides.

    Every field is required and non-blank, and ``kind`` must name a real
    S-rule constant: there is no wildcard that could disable the analyzer, or
    even one whole rule, across every node or criterion at once.
    """

    overrides: list[ConformanceOverride] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise DecompositionConformanceError(f"override {index} must be an object")
        if set(entry) != {"scope", "target", "kind", "reason"}:
            raise DecompositionConformanceError(f"override {index} has invalid fields")
        scope = entry.get("scope")
        target = entry.get("target")
        kind = entry.get("kind")
        reason = entry.get("reason")
        if scope not in _OVERRIDE_SCOPES:
            raise DecompositionConformanceError(
                f"override {index} scope must be 'node' or 'criterion'"
            )
        if not isinstance(target, str) or not target.strip():
            raise DecompositionConformanceError(f"override {index} target is required")
        if kind not in ALL_KINDS:
            raise DecompositionConformanceError(
                f"override {index} kind is not a recognized conformance finding kind"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise DecompositionConformanceError(
                f"override {index} requires a non-empty reason"
            )
        overrides.append(ConformanceOverride(scope, target, kind, reason))
    return tuple(overrides)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceFinding:
    kind: str
    grade: str
    category: str
    severity: str
    statement: str
    runs: tuple[str, ...]
    criterion: str | None
    paths: tuple[str, ...]
    proposal: Mapping[str, object] | None = None
    enforced: bool = False
    overridden: bool = False
    override_reason: str | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "id": f"{self.kind}:{','.join(self.runs)}:{self.criterion or ''}",
            "kind": self.kind,
            "grade": self.grade,
            "category": self.category,
            "severity": self.severity,
            "statement": self.statement,
            "runs": list(self.runs),
            "criterion": self.criterion,
            "paths": list(self.paths),
            "proposal": dict(self.proposal) if self.proposal is not None else None,
            "requires_disposition": self.enforced,
            "enforced": self.enforced,
            "overridden": self.overridden,
            "override_reason": self.override_reason,
        }


def _finding(
    kind: str,
    category: str,
    *,
    runs: Sequence[str],
    criterion: str | None,
    paths: Sequence[str],
    statement: str,
    proposal: Mapping[str, object] | None = None,
) -> ConformanceFinding:
    if kind in _BLOCK_KINDS:
        grade, severity = GRADE_BLOCK, "critical"
    elif kind in _WARN_KINDS:
        grade, severity = GRADE_WARN, "major"
    else:
        grade, severity = GRADE_PROPOSAL, "info"
    return ConformanceFinding(
        kind=kind,
        grade=grade,
        category=category,
        severity=severity,
        statement=statement,
        runs=tuple(runs),
        criterion=criterion,
        paths=tuple(paths),
        proposal=proposal,
    )


# ---------------------------------------------------------------------------
# Shared plan-shape helpers
# ---------------------------------------------------------------------------


def _looks_like_file(path: str) -> bool:
    return "." in path.rsplit("/", 1)[-1]


def _paths_overlap(first: str, second: str) -> bool:
    first = first.rstrip("/")
    second = second.rstrip("/")
    return (
        first == second
        or second.startswith(first + "/")
        or first.startswith(second + "/")
    )


class _AncestorIndex:
    """Dependency-closure lookup, computed once per :func:`analyze_decomposition` call.

    Duplicated from ``plan_approval._sibling_overlap_warnings``'s own closure
    rather than imported: ``plan_approval`` imports this module to wire the
    analyzer into admission, so the reverse import would cycle.
    """

    def __init__(self, plan: PlanGraphPlan) -> None:
        self.by_id: dict[str, PlanRun] = {run.id: run for run in plan.runs}
        self._cache: dict[str, frozenset[str]] = {}

    def ancestors(self, run_id: str) -> frozenset[str]:
        cached = self._cache.get(run_id)
        if cached is not None:
            return cached
        found: set[str] = set()
        for dependency in self.by_id[run_id].depends_on:
            found.add(dependency)
            found |= self.ancestors(dependency)
        self._cache[run_id] = frozenset(found)
        return self._cache[run_id]

    def is_sink(self, run_id: str) -> bool:
        return not any(
            run_id in run.depends_on for run in self.by_id.values() if run.id != run_id
        )

    def available_paths(self, run: PlanRun) -> set[str]:
        available = set(run.allowed_paths)
        for ancestor_id in self.ancestors(run.id):
            available |= set(self.by_id[ancestor_id].allowed_paths)
        return available


# ---------------------------------------------------------------------------
# S1 / S3: dependency-unordered writable-path overlap, and its serialization proposal
# ---------------------------------------------------------------------------


def _writable_path_overlaps(
    plan: PlanGraphPlan, index: _AncestorIndex
) -> list[tuple[str, str, list[str]]]:
    ordered = [run.id for run in plan.runs]
    overlaps: list[tuple[str, str, list[str]]] = []
    for position, first_id in enumerate(ordered):
        for second_id in ordered[position + 1 :]:
            if first_id in index.ancestors(second_id) or second_id in index.ancestors(first_id):
                continue
            first_run, second_run = index.by_id[first_id], index.by_id[second_id]
            shared = {
                max(path_a, path_b, key=len)
                for path_a in first_run.allowed_paths
                for path_b in second_run.allowed_paths
                if _paths_overlap(path_a, path_b)
            }
            if shared:
                overlaps.append((first_id, second_id, sorted(shared)))
    return overlaps


def _s1_and_s3_findings(
    plan: PlanGraphPlan, index: _AncestorIndex
) -> list[ConformanceFinding]:
    findings: list[ConformanceFinding] = []
    for first_id, second_id, paths in _writable_path_overlaps(plan, index):
        findings.append(
            _finding(
                S1_WRITABLE_PATH_OVERLAP,
                "S1",
                runs=(first_id, second_id),
                criterion=None,
                paths=paths,
                statement=(
                    f"{first_id!r} and {second_id!r} are dependency-unordered and "
                    f"share writable path(s) {', '.join(paths)}; a controller join "
                    "of both cannot be guaranteed disjoint"
                ),
            )
        )
        findings.append(
            _finding(
                S3_SERIALIZATION_PROPOSAL,
                "S3",
                runs=(first_id, second_id),
                criterion=None,
                paths=paths,
                statement=(
                    f"propose ordering {second_id!r} after {first_id!r} via "
                    "depends_on to resolve the S1 overlap by construction "
                    "rather than by prose discipline"
                ),
                proposal={
                    "repair": "serialize",
                    "dependency": first_id,
                    "dependent": second_id,
                    "paths": paths,
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# S2: directory grants
# ---------------------------------------------------------------------------


def _s2_findings(plan: PlanGraphPlan) -> list[ConformanceFinding]:
    findings: list[ConformanceFinding] = []
    for run in plan.runs:
        declared_creates = {
            intent.path for intent in run.path_intents if intent.action == "create"
        }
        directory_grants = sorted(
            path
            for path in run.allowed_paths
            if not _looks_like_file(path) and path not in declared_creates
        )
        if directory_grants:
            findings.append(
                _finding(
                    S2_DIRECTORY_GRANT,
                    "S2",
                    runs=(run.id,),
                    criterion=None,
                    paths=directory_grants,
                    statement=(
                        f"{run.id!r} holds directory grant(s) "
                        f"{', '.join(directory_grants)} that are neither "
                        "file-shaped nor an explicitly declared create"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# S4: grants outside the objective's declared intents
# ---------------------------------------------------------------------------


def _s4_findings(plan: PlanGraphPlan) -> list[ConformanceFinding]:
    """Elevate ``unintended_grants`` to a block for a positively-disclaiming run.

    ``plan_approval._unclaimed_grant_warnings`` treats an empty
    ``path_intents`` as an absence of evidence, not evidence of absence, and
    stays silent. This rule reuses that same predicate
    (``declares_intent``/``unintended_grants`` in ``plan_graph_contract``)
    but only fires once a run *has* declared some intent and that declared
    intent still fails to cover a grant it holds -- an unambiguous signal
    the grant sits outside what the run's own objective claimed.
    """

    findings: list[ConformanceFinding] = []
    for run in plan.runs:
        intent_paths = [intent.path for intent in run.path_intents]
        if not declares_intent(intent_paths):
            continue
        uncovered = unintended_grants(intent_paths, run.allowed_paths)
        if uncovered:
            findings.append(
                _finding(
                    S4_GRANT_OUTSIDE_OBJECTIVE,
                    "S4",
                    runs=(run.id,),
                    criterion=None,
                    paths=sorted(uncovered),
                    statement=(
                        f"{run.id!r} declares path intents that do not cover "
                        f"grant(s) {', '.join(sorted(uncovered))}; the objective "
                        "disclaims what those grants would let it write"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# S5 / S6: observable declarations
# ---------------------------------------------------------------------------


def _run_argv_tokens(run: PlanRun) -> list[str]:
    tokens = list(run.verification_argv)
    for gate in run.verification_gates:
        tokens.extend(gate.argv)
    return tokens


def _reachable_from_argv(referent: str, tokens: Sequence[str]) -> bool:
    return any(referent == token or referent in token for token in tokens)


def _s5_and_s6_findings(
    plan: PlanGraphPlan, canonical: Mapping[str, object]
) -> list[ConformanceFinding]:
    criteria_text = canonical.get("acceptance_criteria")
    criteria_text = criteria_text if isinstance(criteria_text, Mapping) else {}
    findings: list[ConformanceFinding] = []
    for run in plan.runs:
        tokens = _run_argv_tokens(run)
        for criterion_id in run.criteria:
            text = criteria_text.get(criterion_id, "")
            observable = parse_observable(text)
            if observable is None:
                findings.append(
                    _finding(
                        S5_MISSING_OBSERVABLE,
                        "S5",
                        runs=(run.id,),
                        criterion=criterion_id,
                        paths=(),
                        statement=(
                            f"criterion {criterion_id!r} owned by {run.id!r} carries "
                            "no machine-readable {kind, referent} observable"
                        ),
                    )
                )
                continue
            kind, referent = observable["kind"], observable["referent"]
            reasons: list[str] = []
            if kind == "file" and not path_is_allowed(referent, run.allowed_paths):
                reasons.append("outside the node's grants")
            if not _reachable_from_argv(referent, tokens):
                reasons.append("not reachable from verification_argv")
            if reasons:
                findings.append(
                    _finding(
                        S6_UNREACHABLE_OBSERVABLE,
                        "S6",
                        runs=(run.id,),
                        criterion=criterion_id,
                        paths=(referent,),
                        statement=(
                            f"criterion {criterion_id!r} owned by {run.id!r} declares "
                            f"observable referent {referent!r} that is "
                            f"{' and '.join(reasons)}"
                        ),
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# S7: exit checks satisfiable within the node's own (and inherited) grants
# ---------------------------------------------------------------------------


def _s7_findings(plan: PlanGraphPlan, index: _AncestorIndex) -> list[ConformanceFinding]:
    findings: list[ConformanceFinding] = []
    for run in plan.runs:
        available = index.available_paths(run)
        for required in run.verification_required_paths:
            if required.availability != "created_by":
                continue
            producer = required.producer_run_id
            if producer != run.id and producer not in index.ancestors(run.id):
                findings.append(
                    _finding(
                        S7_EXIT_CHECK_OUTSIDE_GRANTS,
                        "S7",
                        runs=(run.id,),
                        criterion=None,
                        paths=(required.path,),
                        statement=(
                            f"{run.id!r}'s exit check requires "
                            f"{required.path!r} from producer {producer!r}, which "
                            "is not an ancestor and so is not merged in by "
                            "execution time"
                        ),
                    )
                )
                continue
            if not path_is_allowed(required.path, available):
                findings.append(
                    _finding(
                        S7_EXIT_CHECK_OUTSIDE_GRANTS,
                        "S7",
                        runs=(run.id,),
                        criterion=None,
                        paths=(required.path,),
                        statement=(
                            f"{run.id!r}'s exit check requires {required.path!r}, "
                            "which lies outside its own grants and its "
                            "inherited-region merge obligations"
                        ),
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# S8: verification gate no larger than the criteria set
# ---------------------------------------------------------------------------


def _s8_findings(plan: PlanGraphPlan) -> list[ConformanceFinding]:
    findings: list[ConformanceFinding] = []
    for run in plan.runs:
        if run.verification_gates and len(run.verification_gates) > len(run.criteria):
            findings.append(
                _finding(
                    S8_GATE_LARGER_THAN_CRITERIA,
                    "S8",
                    runs=(run.id,),
                    criterion=None,
                    paths=(),
                    statement=(
                        f"{run.id!r} carries {len(run.verification_gates)} "
                        f"verification gate(s) against {len(run.criteria)} "
                        "criterion/criteria; a gate that pins a repo-wide "
                        "invariant beyond its criteria should become a "
                        "criterion or move downstream"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# S9: fan-in and the intermediate-join proposal
# ---------------------------------------------------------------------------


def intermediate_join_proposal(run: PlanRun) -> dict[str, object]:
    """A concrete, never-applied intermediate-join proposal for one over-fanned run.

    Groups just enough of ``run``'s dependencies into a new intermediate node
    so ``run``'s own fan-in falls to :data:`MAX_FAN_IN`; the intermediate
    node's own fan-in is exactly the grouped count, which is constructed to
    stay at or under the same limit. Pure data: applying it means an operator
    (or a tool acting on their behalf) writes a *new* decomposition carrying
    the extra node and the rewired edges, then re-commits it -- this function
    never touches ``run`` or any decomposition mapping.
    """

    dependencies = sorted(run.depends_on)
    overflow = len(dependencies) - MAX_FAN_IN + 1
    grouped = dependencies[:overflow]
    remaining = dependencies[overflow:]
    intermediate_id = f"{run.id}-join-1"
    return {
        "target_run": run.id,
        "current_fan_in": len(dependencies),
        "proposed_intermediate_node_id": intermediate_id,
        "grouped_dependencies": grouped,
        "remaining_dependencies": remaining,
        "rewired_depends_on": sorted(remaining + [intermediate_id]),
    }


def _s9_findings(plan: PlanGraphPlan, index: _AncestorIndex) -> list[ConformanceFinding]:
    findings: list[ConformanceFinding] = []
    for run in plan.runs:
        if len(run.depends_on) <= MAX_FAN_IN or index.is_sink(run.id):
            continue
        proposal = intermediate_join_proposal(run)
        findings.append(
            _finding(
                S9_FAN_IN_JOIN_PROPOSAL,
                "S9",
                runs=(run.id,),
                criterion=None,
                paths=(),
                statement=(
                    f"{run.id!r} has fan-in {len(run.depends_on)} > {MAX_FAN_IN} "
                    "and is not the plan's sink/join node; propose an "
                    "intermediate join"
                ),
                proposal=proposal,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# S10: node count per round
# ---------------------------------------------------------------------------


def _s10_findings(plan: PlanGraphPlan) -> list[ConformanceFinding]:
    if len(plan.runs) <= MAX_ROUND_NODES:
        return []
    run_ids = [run.id for run in plan.runs]
    return [
        _finding(
            S10_NODE_COUNT,
            "S10",
            runs=run_ids,
            criterion=None,
            paths=(),
            statement=(
                f"the round carries {len(run_ids)} node(s), above the "
                f"~{MAX_ROUND_NODES}-node guideline; split the round"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceReport:
    conformance_aware: bool
    enforced: bool
    findings: tuple[ConformanceFinding, ...]
    overrides_applied: tuple[ConformanceOverride, ...]

    def as_mapping(self) -> dict[str, object]:
        return {
            "protocol": CONFORMANCE_PROTOCOL,
            "conformance_aware": self.conformance_aware,
            "enforced": self.enforced,
            "findings": [item.as_mapping() for item in self.findings],
            "proposals": [
                item.as_mapping() for item in self.findings if item.grade == GRADE_PROPOSAL
            ],
            "overrides_applied": [item.as_mapping() for item in self.overrides_applied],
        }

    @property
    def block_violations(self) -> tuple[ConformanceFinding, ...]:
        return tuple(
            item
            for item in self.findings
            if item.grade == GRADE_BLOCK and item.enforced and not item.overridden
        )

    def warning_entries(self) -> list[dict[str, object]]:
        """Warn-grade findings shaped for ``plan_approval``'s ``warnings`` array.

        Only enforced, unoverridden entries are returned, and always at
        ``severity: "high"`` -- the one severity
        ``_require_acknowledged_high_warnings`` treats as ack-required. This
        is how S8/S10 ride the existing acknowledgment gate rather than the
        analyzer inventing a second one.
        """

        return [
            {
                "kind": item.kind,
                "severity": "high",
                "runs": list(item.runs),
                "paths": list(item.paths),
                "note": item.statement,
            }
            for item in self.findings
            if item.grade == GRADE_WARN and item.enforced and not item.overridden
        ]


def analyze_decomposition(
    plan: PlanGraphPlan,
    canonical: Mapping[str, object],
    *,
    enforce: bool | None = None,
    overrides: Sequence[Mapping[str, object]] = (),
) -> ConformanceReport:
    """Run every S1-S10 check and resolve enforcement/overrides.

    Analysis always runs and every finding is always present in the
    returned report; ``enforce``/``overrides`` only control each finding's
    ``enforced``/``overridden`` flags, never whether it appears at all.
    """

    conformance_aware = is_conformance_aware(canonical)
    enforcement_active = conformance_aware if enforce is None else bool(enforce)
    parsed_overrides = parse_overrides(overrides)

    index = _AncestorIndex(plan)
    raw_findings: list[ConformanceFinding] = [
        *_s1_and_s3_findings(plan, index),
        *_s2_findings(plan),
        *_s4_findings(plan),
        *_s5_and_s6_findings(plan, canonical),
        *_s7_findings(plan, index),
        *_s8_findings(plan),
        *_s9_findings(plan, index),
        *_s10_findings(plan),
    ]
    raw_findings.sort(key=lambda item: (item.kind, item.runs, item.criterion or "", item.paths))

    used_overrides: set[int] = set()
    resolved: list[ConformanceFinding] = []
    for item in raw_findings:
        override = None
        for offset, candidate in enumerate(parsed_overrides):
            if candidate.matches(item):
                override = candidate
                used_overrides.add(offset)
                break
        overridden = override is not None
        enforced = (
            enforcement_active
            and item.grade in (GRADE_BLOCK, GRADE_WARN)
            and not overridden
        )
        resolved.append(
            replace(
                item,
                enforced=enforced,
                overridden=overridden,
                override_reason=override.reason if override is not None else None,
            )
        )

    applied = tuple(
        override for offset, override in enumerate(parsed_overrides) if offset in used_overrides
    )
    return ConformanceReport(
        conformance_aware=conformance_aware,
        enforced=enforcement_active,
        findings=tuple(resolved),
        overrides_applied=applied,
    )


# ---------------------------------------------------------------------------
# Gate-evidence shape validation
# ---------------------------------------------------------------------------


def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise DecompositionConformanceError(f"{label} has invalid fields")


_FINDING_KEYS = {
    "id", "kind", "grade", "category", "severity", "statement", "runs",
    "criterion", "paths", "proposal", "requires_disposition", "enforced",
    "overridden", "override_reason",
}
_OVERRIDE_APPLIED_KEYS = {"scope", "target", "kind", "reason"}


def validate_conformance_report(value: object) -> None:
    """Shape-check one embedded conformance report -- called from
    ``plan_approval._validate_gate_evidence`` so a hand-edited
    ``gate-evidence.json`` cannot carry a malformed or absent report."""

    if not isinstance(value, Mapping):
        raise DecompositionConformanceError("conformance report must be an object")
    _require_exact_keys(
        value,
        {"protocol", "conformance_aware", "enforced", "findings", "proposals", "overrides_applied"},
        "conformance report",
    )
    if value.get("protocol") != CONFORMANCE_PROTOCOL:
        raise DecompositionConformanceError("unsupported conformance report protocol")
    for field_name in ("conformance_aware", "enforced"):
        if not isinstance(value.get(field_name), bool):
            raise DecompositionConformanceError(f"conformance report {field_name} must be a boolean")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise DecompositionConformanceError("conformance report findings must be an array")
    for index, item in enumerate(findings):
        _validate_finding_shape(item, index)
    proposals = value.get("proposals")
    if not isinstance(proposals, list):
        raise DecompositionConformanceError("conformance report proposals must be an array")
    for index, item in enumerate(proposals):
        _validate_finding_shape(item, index)
    overrides_applied = value.get("overrides_applied")
    if not isinstance(overrides_applied, list):
        raise DecompositionConformanceError("conformance report overrides_applied must be an array")
    for index, item in enumerate(overrides_applied):
        _require_exact_keys(item, _OVERRIDE_APPLIED_KEYS, f"applied override {index}")
        for field_name in _OVERRIDE_APPLIED_KEYS:
            if not isinstance(item.get(field_name), str) or not item[field_name]:
                raise DecompositionConformanceError(f"applied override {index} {field_name} is invalid")


def _validate_finding_shape(item: object, index: int) -> None:
    label = f"conformance finding {index}"
    _require_exact_keys(item, _FINDING_KEYS, label)
    if item.get("kind") not in ALL_KINDS:
        raise DecompositionConformanceError(f"{label} kind is not recognized")
    if item.get("grade") not in GRADES:
        raise DecompositionConformanceError(f"{label} grade is invalid")
    if not isinstance(item.get("statement"), str) or not item["statement"].strip():
        raise DecompositionConformanceError(f"{label} statement is required")
    if not isinstance(item.get("runs"), list) or not all(
        isinstance(value, str) for value in item["runs"]
    ):
        raise DecompositionConformanceError(f"{label} runs must be an array of strings")
    criterion = item.get("criterion")
    if criterion is not None and not isinstance(criterion, str):
        raise DecompositionConformanceError(f"{label} criterion must be a string or null")
    if not isinstance(item.get("paths"), list) or not all(
        isinstance(value, str) for value in item["paths"]
    ):
        raise DecompositionConformanceError(f"{label} paths must be an array of strings")
    proposal = item.get("proposal")
    if proposal is not None and not isinstance(proposal, Mapping):
        raise DecompositionConformanceError(f"{label} proposal must be an object or null")
    for field_name in ("requires_disposition", "enforced", "overridden"):
        if not isinstance(item.get(field_name), bool):
            raise DecompositionConformanceError(f"{label} {field_name} must be a boolean")
    override_reason = item.get("override_reason")
    if override_reason is not None and not isinstance(override_reason, str):
        raise DecompositionConformanceError(f"{label} override_reason must be a string or null")


# ---------------------------------------------------------------------------
# S3 refinement-judge adapter
# ---------------------------------------------------------------------------

JUDGMENT_PROTOCOL = "plan-refinement-judgment/1"


def conformance_judge():
    """A ``plan_refinement.RefinementJudge``-compatible callable implementing S3.

    ``plan_refinement.refine_decomposition`` already applies its own
    intent-aware narrowing first and only calls the injected judge for
    dependency-unordered overlaps that survive it -- genuine contention where
    both runs declared intent on the shared path. That is exactly S1's
    remaining case once the mechanical repair is exhausted, so this adapter
    always answers with S3's rule: order the runs with ``depends_on`` rather
    than leave the collision to prose discipline. It is deliberately a plain
    callable (no import of ``plan_refinement``, which would cycle back
    through ``plan_approval``) so it plugs into ``judge=`` without this
    module depending on the refinement loop's internals.
    """

    def _judge(request: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(request, Mapping) or request.get("protocol") != JUDGMENT_PROTOCOL:
            raise DecompositionConformanceError(
                "conformance judge received an unrecognized refinement request"
            )
        return {
            "repair": "serialize",
            "reason": (
                "S3: shared writers are serialized by depends_on, never left "
                "to prose discipline; ordering the plan-earlier run first "
                "resolves the overlap by construction"
            ),
        }

    return _judge


__all__ = [
    "ALL_KINDS",
    "CONFORMANCE_PROTOCOL",
    "ConformanceFinding",
    "ConformanceOverride",
    "ConformanceReport",
    "DecompositionConformanceError",
    "GRADE_BLOCK",
    "GRADE_PROPOSAL",
    "GRADE_WARN",
    "JUDGMENT_PROTOCOL",
    "MAX_FAN_IN",
    "MAX_ROUND_NODES",
    "OBSERVABLE_KINDS",
    "S1_WRITABLE_PATH_OVERLAP",
    "S2_DIRECTORY_GRANT",
    "S3_SERIALIZATION_PROPOSAL",
    "S4_GRANT_OUTSIDE_OBJECTIVE",
    "S5_MISSING_OBSERVABLE",
    "S6_UNREACHABLE_OBSERVABLE",
    "S7_EXIT_CHECK_OUTSIDE_GRANTS",
    "S8_GATE_LARGER_THAN_CRITERIA",
    "S9_FAN_IN_JOIN_PROPOSAL",
    "S10_NODE_COUNT",
    "analyze_decomposition",
    "conformance_judge",
    "intermediate_join_proposal",
    "is_conformance_aware",
    "parse_observable",
    "parse_overrides",
    "validate_conformance_report",
]
