"""Close the loop on admission-time plan defects before an operator approves.

``prepare_approval`` already computes the strongest static predictor of a
controller-join conflict -- ``_sibling_overlap_warnings`` -- and writes it
into ``gate-evidence.json``. Across two real campaigns the plan carrying 17
HIGH overlap warnings produced 36 join-conflict escalations and stalled,
while the plan carrying none produced none and completed. The detection was
correct and nobody consumed it.

This module is the consumer: prepare -> warnings -> revise the decomposition
-> re-prepare -> repeat, until the plan is clean, only judgment calls remain,
or the loop stops making progress. The human still approves the final plan --
that is what confirms intent -- but they approve a decomposition whose
mechanical defects have already been repaired, with a report showing every
edit and why it was made.

Two deliberate contracts:

* The judge is an injected callable defaulting to ``None``. Rewriting an
  operator's decomposition is not something to do without a decision-maker in
  the loop, so ``None`` means *report only*: the loop runs the analysis, marks
  every finding with the repair it would apply and whether that repair is
  mechanical or a judgment call, and returns the decomposition untouched.
  Tests therefore never need a live model.
* Only the git-independent half of preparation runs here. Blob freezing and
  host-executable evidence do not vary with the decomposition's *content*, and
  a revised decomposition is not committed yet; ``prepare_approval`` runs the
  full check once, after the operator commits the refined plan.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from harness_labs.plangraph.plan_approval import (
    _git,
    _git_artifact,
    _load_json_bytes,
    _relative_repository_path,
    _sibling_overlap_warnings,
    warning_identity,
)
from harness_labs.plangraph.plan_graph import (
    PlanGraphError,
    plan_from_mapping,
    validate_plan_graph_plan,
)
from harness_labs.plangraph.plan_graph_contract import (
    PlanGraphContractError,
    canonical_plan_graph_payload,
    load_repository_id,
    path_is_allowed,
    plan_graph_identity,
)
from pathlib import Path


REFINEMENT_PROTOCOL = "plan-refinement-report/1"
JUDGMENT_PROTOCOL = "plan-refinement-judgment/1"

#: Repairs a judge may return. ``serialize`` is the mechanical one and is also
#: what the loop applies on its own; the other two are the judgment calls.
JUDGE_REPAIRS = ("serialize", "narrow_grant", "defer")


class PlanRefinementError(ValueError):
    """Raised when a refinement request or a judge decision is unusable."""


#: A judge receives one ``plan-refinement-judgment/1`` request and returns one
#: decision mapping: ``{"repair": ..., "reason": <non-empty string>, ...}``.
RefinementJudge = Callable[[Mapping[str, object]], Mapping[str, object]]


@dataclass(frozen=True)
class Repair:
    """One decomposition edit, with who decided it and why."""

    kind: str
    decided_by: str
    reason: str
    runs: tuple[str, ...]
    detail: Mapping[str, object]
    finding: Mapping[str, object]

    def as_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "runs": list(self.runs),
            "detail": dict(self.detail),
            "finding": dict(self.finding),
        }


@dataclass(frozen=True)
class RefinementRound:
    index: int
    plan_graph_digest: str
    high_warnings: int
    info_warnings: int
    applied: tuple[Repair, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "index": self.index,
            "plan_graph_digest": self.plan_graph_digest,
            "high_warnings": self.high_warnings,
            "info_warnings": self.info_warnings,
            "applied": [repair.as_mapping() for repair in self.applied],
        }


@dataclass(frozen=True)
class RefinementOutcome:
    """The complete record of what the loop changed, and what it did not."""

    status: str
    reason: str
    decomposition: Mapping[str, object]
    original_decomposition: Mapping[str, object]
    rounds: tuple[RefinementRound, ...]
    applied: tuple[Repair, ...]
    deferred: tuple[Mapping[str, object], ...]
    proposals: tuple[Mapping[str, object], ...]
    initial_plan_graph_digest: str
    final_plan_graph_digest: str
    initial_warnings: Mapping[str, int]
    final_warnings: Mapping[str, int]
    open_warnings: tuple[Mapping[str, object], ...]

    @property
    def revised(self) -> bool:
        return self.initial_plan_graph_digest != self.final_plan_graph_digest

    def as_mapping(self) -> dict[str, object]:
        return {
            "protocol": REFINEMENT_PROTOCOL,
            "status": self.status,
            "reason": self.reason,
            "revised": self.revised,
            "initial_plan_graph_digest": self.initial_plan_graph_digest,
            "final_plan_graph_digest": self.final_plan_graph_digest,
            "initial_warnings": dict(self.initial_warnings),
            "final_warnings": dict(self.final_warnings),
            "rounds": [entry.as_mapping() for entry in self.rounds],
            "applied": [repair.as_mapping() for repair in self.applied],
            "deferred": [dict(item) for item in self.deferred],
            "proposals": [dict(item) for item in self.proposals],
            "open_warnings": [dict(item) for item in self.open_warnings],
            "decomposition_diff": decomposition_diff(
                self.original_decomposition, self.decomposition
            ),
        }


# ---------------------------------------------------------------------------
# No-progress guard
# ---------------------------------------------------------------------------


@dataclass
class NoProgressGuard:
    """Stop revising once consecutive rounds stop differing.

    Modeled on ``scripts/plan_graph_autoresume.NoProgressGuard``: the
    signature deliberately excludes the round index, which changes by
    construction, and carries the findings an operator would actually read to
    decide whether anything moved. An unbounded revise loop over an LLM judge
    is a money fire; this is the brake.
    """

    threshold: int = 2
    signature: tuple[object, ...] | None = None
    repeats: int = 0

    def observe(self, signature: tuple[object, ...]) -> bool:
        if self.threshold < 1:
            raise PlanRefinementError("no-progress threshold must be positive")
        self.repeats = self.repeats + 1 if signature == self.signature else 1
        self.signature = signature
        return self.repeats >= self.threshold


# ---------------------------------------------------------------------------
# Analysis (the git-independent half of preparation, re-run per revision)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    canonical: dict[str, object]
    digest: str
    warnings: tuple[dict[str, object], ...]

    @property
    def high(self) -> tuple[dict[str, object], ...]:
        return tuple(item for item in self.warnings if item["severity"] == "high")

    @property
    def info(self) -> tuple[dict[str, object], ...]:
        return tuple(item for item in self.warnings if item["severity"] != "high")


def _prepare(
    decomposition: Mapping[str, object],
    *,
    base_commit: str,
    repository_id: str,
    plan_sha256: str,
) -> _Prepared:
    try:
        canonical = canonical_plan_graph_payload(decomposition)
        plan = plan_from_mapping(
            canonical,
            base_commit=base_commit,
            repository_id=repository_id,
            plan_sha256=plan_sha256,
        )
        validate_plan_graph_plan(plan)
        digest = plan_graph_identity(
            repository_id=repository_id,
            base_commit=base_commit,
            plan_sha256=plan_sha256,
            decomposition=canonical,
        )
    except (PlanGraphContractError, PlanGraphError) as exc:
        raise PlanRefinementError(str(exc)) from exc
    return _Prepared(canonical, digest, tuple(_sibling_overlap_warnings(plan)))


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------


def _run_index(canonical: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {str(run["id"]): run for run in canonical["runs"]}  # type: ignore[index]


def _reaches(runs: Mapping[str, Mapping[str, object]], start: str, target: str) -> bool:
    """True when ``target`` is already an ancestor of ``start``."""

    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for dependency in runs[current].get("depends_on") or ():
            dependency = str(dependency)
            if dependency == target:
                return True
            if dependency not in seen and dependency in runs:
                seen.add(dependency)
                pending.append(dependency)
    return False


def _serialize(
    canonical: dict[str, object], first: str, second: str
) -> dict[str, object] | None:
    """Order ``second`` after ``first``; return the edit, or ``None`` on cycle.

    The direction rule is plan order: the decomposition's run sequence is what
    the operator authored as the intended progression, so the earlier run
    becomes the dependency. The reverse is tried when plan order would close a
    cycle -- which can happen only against edges this same loop already
    inserted, since the pair was dependency-unordered when it was flagged.
    """

    runs = _run_index(canonical)
    if first not in runs or second not in runs:
        raise PlanRefinementError(f"unknown run in serialize repair: {first}, {second}")
    for dependency, dependent in ((first, second), (second, first)):
        if _reaches(runs, dependency, dependent):
            continue  # would close a cycle
        edges = list(runs[dependent].get("depends_on") or ())
        if dependency in edges:
            return None
        runs[dependent]["depends_on"] = sorted(edges + [dependency])
        return {"edge": {"dependency": dependency, "dependent": dependent}}
    return None


def _narrow_grant(
    canonical: dict[str, object],
    run_id: str,
    drop_paths: Sequence[str],
    add_paths: Sequence[str],
) -> dict[str, object]:
    runs = _run_index(canonical)
    run = runs.get(run_id)
    if run is None:
        raise PlanRefinementError(f"unknown run in narrow_grant repair: {run_id}")
    allowed = [str(value) for value in run["allowed_paths"]]  # type: ignore[index]
    unknown = [path for path in drop_paths if path not in allowed]
    if unknown:
        raise PlanRefinementError(
            f"run {run_id!r} does not hold the dropped grants: {sorted(unknown)}"
        )
    narrowed = [path for path in allowed if path not in set(drop_paths)]
    for path in add_paths:
        if path not in narrowed:
            narrowed.append(path)
    if not narrowed:
        raise PlanRefinementError(f"run {run_id!r} would be left with no allowed_paths")
    # A grant may never be narrowed out from under a declared intent: the run
    # would then be unable to do the work it was assigned.
    orphaned = [
        str(intent["path"])
        for intent in run["path_intents"]  # type: ignore[index]
        if not path_is_allowed(str(intent["path"]), narrowed)
    ]
    if orphaned:
        raise PlanRefinementError(
            f"run {run_id!r} declares path intents outside the narrowed grant: "
            f"{sorted(orphaned)}"
        )
    run["allowed_paths"] = narrowed
    return {"run": run_id, "dropped": sorted(drop_paths), "added": sorted(add_paths)}


# ---------------------------------------------------------------------------
# The judge boundary
# ---------------------------------------------------------------------------


def judgment_request(
    finding: Mapping[str, object], canonical: Mapping[str, object]
) -> dict[str, object]:
    """The context a judge needs, and nothing else."""

    runs = _run_index(canonical)
    order = [str(run["id"]) for run in canonical["runs"]]  # type: ignore[index]
    subjects = {}
    for run_id in finding["runs"]:  # type: ignore[index]
        run = runs[str(run_id)]
        subjects[str(run_id)] = {
            "objective": run["objective"],
            "allowed_paths": list(run["allowed_paths"]),  # type: ignore[arg-type]
            "path_intents": [dict(intent) for intent in run["path_intents"]],  # type: ignore[union-attr]
            "depends_on": list(run.get("depends_on") or ()),
            "criteria": list(run.get("criteria") or ()),
            "plan_order": order.index(str(run_id)),
        }
    return {
        "protocol": JUDGMENT_PROTOCOL,
        "finding": dict(finding),
        "runs": subjects,
        "repairs": list(JUDGE_REPAIRS),
        "question": (
            "These dependency-unordered runs hold overlapping write grants; "
            "their edits will collide in the controller join. Decide which run "
            "should own the contested path (narrow_grant), or order them "
            "(serialize), or defer the call to the operator (defer). Every "
            "decision requires a reason."
        ),
    }


def _decide(
    judge: RefinementJudge,
    finding: Mapping[str, object],
    canonical: Mapping[str, object],
) -> dict[str, object]:
    decision = judge(judgment_request(finding, canonical))
    if not isinstance(decision, Mapping):
        raise PlanRefinementError("judge decision must be a mapping")
    repair = decision.get("repair")
    reason = decision.get("reason")
    if repair not in JUDGE_REPAIRS:
        raise PlanRefinementError(f"unsupported judge repair: {repair!r}")
    # Modeled on RetryBudgetLedger.extend: a decision without a stated reason
    # is not a decision anybody can review later.
    if not isinstance(reason, str) or not reason.strip():
        raise PlanRefinementError("judge decision requires a non-empty reason")
    return dict(decision)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    judge: RefinementJudge | None
    guard: NoProgressGuard
    applied: list[Repair] = field(default_factory=list)
    deferred: dict[str, dict[str, object]] = field(default_factory=dict)


def refine_decomposition(
    decomposition: Mapping[str, object],
    *,
    base_commit: str,
    repository_id: str,
    plan_sha256: str,
    judge: RefinementJudge | None = None,
    max_rounds: int = 8,
    no_progress_threshold: int = 2,
) -> RefinementOutcome:
    """Revise ``decomposition`` until its HIGH overlap warnings are resolved.

    With no ``judge`` the decomposition is returned untouched and every
    finding is reported as a proposal -- see the module docstring.
    """

    if max_rounds < 1:
        raise PlanRefinementError("max_rounds must be positive")
    original = _prepare(
        decomposition,
        base_commit=base_commit,
        repository_id=repository_id,
        plan_sha256=plan_sha256,
    )
    counts = {"high": len(original.high), "info": len(original.info)}
    if judge is None:
        return _report_only(original, counts)

    session = _Session(judge, NoProgressGuard(no_progress_threshold))
    working = copy.deepcopy(original.canonical)
    prepared = original
    rounds: list[RefinementRound] = []
    status = reason = ""
    for index in range(1, max_rounds + 1):
        prepared = _prepare(
            working,
            base_commit=base_commit,
            repository_id=repository_id,
            plan_sha256=plan_sha256,
        )
        working = copy.deepcopy(prepared.canonical)
        actionable = [
            finding
            for finding in prepared.high
            if warning_identity(finding) not in session.deferred
        ]
        if not prepared.high:
            status, reason = "clean", "no high-severity overlap warnings remain"
            rounds.append(_round(index, prepared, ()))
            break
        if not actionable:
            status, reason = (
                "judgment_only",
                f"{len(session.deferred)} finding(s) left for the operator to dispose",
            )
            rounds.append(_round(index, prepared, ()))
            break
        signature = tuple(
            sorted(warning_identity(finding) for finding in prepared.high)
        )
        if session.guard.observe(signature):
            status, reason = (
                "no_progress",
                f"{session.guard.repeats} consecutive rounds left the same "
                f"{len(prepared.high)} high-severity finding(s); operator review required",
            )
            rounds.append(_round(index, prepared, ()))
            break
        applied = _apply_round(working, actionable, session)
        rounds.append(_round(index, prepared, applied))
    else:
        status, reason = (
            "no_progress",
            f"round ceiling of {max_rounds} reached with "
            f"{len(prepared.high)} high-severity finding(s) outstanding",
        )

    final = _prepare(
        working,
        base_commit=base_commit,
        repository_id=repository_id,
        plan_sha256=plan_sha256,
    )
    return RefinementOutcome(
        status=status,
        reason=reason,
        decomposition=final.canonical,
        original_decomposition=original.canonical,
        rounds=tuple(rounds),
        applied=tuple(session.applied),
        deferred=tuple(session.deferred.values()),
        proposals=(),
        initial_plan_graph_digest=original.digest,
        final_plan_graph_digest=final.digest,
        initial_warnings=counts,
        final_warnings={"high": len(final.high), "info": len(final.info)},
        open_warnings=final.high,
    )


def _round(
    index: int, prepared: _Prepared, applied: Sequence[Repair]
) -> RefinementRound:
    return RefinementRound(
        index=index,
        plan_graph_digest=prepared.digest,
        high_warnings=len(prepared.high),
        info_warnings=len(prepared.info),
        applied=tuple(applied),
    )


def _apply_round(
    working: dict[str, object],
    actionable: Sequence[Mapping[str, object]],
    session: _Session,
) -> list[Repair]:
    """Apply one round of repairs, judge first, mechanics as the fallback."""

    applied: list[Repair] = []
    for finding in actionable:
        first, second = (str(value) for value in finding["runs"])  # type: ignore[index]
        assert session.judge is not None
        decision = _decide(session.judge, finding, working)
        repair: Repair | None = None
        if decision["repair"] == "defer":
            session.deferred[warning_identity(finding)] = {
                **dict(finding),
                "decided_by": "judge",
                "disposition": "operator",
                "reason": decision["reason"],
            }
            continue
        if decision["repair"] == "narrow_grant":
            detail = _narrow_grant(
                working,
                str(decision.get("run")),
                [str(value) for value in decision.get("drop_paths") or ()],
                [str(value) for value in decision.get("add_paths") or ()],
            )
            repair = Repair(
                "narrow_grant", "judge", str(decision["reason"]),
                (str(decision.get("run")),), detail, dict(finding),
            )
        else:
            edit = _serialize(
                working,
                str(decision.get("first", first)),
                str(decision.get("second", second)),
            )
            if edit is not None:
                repair = Repair(
                    "serialize", "judge", str(decision["reason"]),
                    (first, second), edit, dict(finding),
                )
        if repair is None:
            # The judge chose an ordering the graph cannot carry. Fall back to
            # the mechanical repair in plan order; if that also cycles, the
            # pair is beyond automation and goes to the operator.
            edit = _serialize(working, first, second)
            if edit is None:
                session.deferred[warning_identity(finding)] = {
                    **dict(finding),
                    "decided_by": "deterministic",
                    "disposition": "operator",
                    "reason": (
                        "ordering these runs in either direction would close a "
                        "dependency cycle; the overlap needs a decomposition change"
                    ),
                }
                continue
            repair = Repair(
                "serialize", "deterministic",
                "plan-order serialization of dependency-unordered runs sharing "
                "a declared file grant",
                (first, second), edit, dict(finding),
            )
        applied.append(repair)
        session.applied.append(repair)
    return applied


def _report_only(prepared: _Prepared, counts: Mapping[str, int]) -> RefinementOutcome:
    proposals = []
    for finding in prepared.warnings:
        high = finding["severity"] == "high"
        proposals.append(
            {
                **dict(finding),
                "warning_sha256": warning_identity(finding),
                "proposed_repair": "serialize" if high else "narrow_grant",
                "repair_class": "deterministic" if high else "judgment",
                "note": (
                    "plan-order depends_on edge removes the overlap mechanically"
                    if high
                    else "narrowing a directory grant needs the node's intent, "
                    "not just the repository listing"
                ),
            }
        )
    return RefinementOutcome(
        status="report_only",
        reason="no judge was injected; the decomposition was left untouched",
        decomposition=prepared.canonical,
        original_decomposition=prepared.canonical,
        rounds=(_round(0, prepared, ()),),
        applied=(),
        deferred=(),
        proposals=tuple(proposals),
        initial_plan_graph_digest=prepared.digest,
        final_plan_graph_digest=prepared.digest,
        initial_warnings=dict(counts),
        final_warnings=dict(counts),
        open_warnings=prepared.high,
    )


# ---------------------------------------------------------------------------
# Diff and repository entry point
# ---------------------------------------------------------------------------


def decomposition_diff(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, object]:
    """What changed against what the operator originally wrote."""

    old = _run_index(before)
    new = _run_index(after)
    changes: dict[str, object] = {}
    for run_id, run in new.items():
        previous = old.get(run_id)
        if previous is None:
            changes[run_id] = {"added_run": True}
            continue
        entry: dict[str, object] = {}
        for field_name in ("depends_on", "allowed_paths"):
            was = list(previous.get(field_name) or ())
            now = list(run.get(field_name) or ())
            if was != now:
                entry[field_name] = {
                    "before": was,
                    "after": now,
                    "added": [value for value in now if value not in was],
                    "removed": [value for value in was if value not in now],
                }
        if entry:
            changes[run_id] = entry
    return changes


def refine_repository_decomposition(
    *,
    repository: Path,
    decomposition_path: Path,
    judge: RefinementJudge | None = None,
    max_rounds: int = 8,
    no_progress_threshold: int = 2,
) -> RefinementOutcome:
    """Refine a committed decomposition using its own approval-bound metadata.

    The metadata is derived exactly as ``prepare_approval`` derives it, so the
    per-round PlanGraph digest is the identity the approval subject will carry
    once the refined decomposition is committed.
    """

    repository = repository.resolve()
    base_commit = _git(repository, "rev-parse", "HEAD")
    relative = _relative_repository_path(
        repository, decomposition_path.resolve(), "decomposition"
    )
    _, raw = _git_artifact(repository, base_commit, relative)
    decomposition = _load_json_bytes(raw, "decomposition")
    _, identity_raw = _git_artifact(
        repository, base_commit, ".harness/repository.json"
    )
    try:
        repository_id = load_repository_id(
            _load_json_bytes(identity_raw, "repository identity")
        )
        canonical = canonical_plan_graph_payload(decomposition)
    except PlanGraphContractError as exc:
        raise PlanRefinementError(str(exc)) from exc
    plan_record, _ = _git_artifact(repository, base_commit, str(canonical["plan"]))
    return refine_decomposition(
        canonical,
        base_commit=base_commit,
        repository_id=repository_id,
        plan_sha256=str(plan_record["sha256"]),
        judge=judge,
        max_rounds=max_rounds,
        no_progress_threshold=no_progress_threshold,
    )


__all__ = [
    "JUDGE_REPAIRS",
    "JUDGMENT_PROTOCOL",
    "NoProgressGuard",
    "PlanRefinementError",
    "REFINEMENT_PROTOCOL",
    "RefinementJudge",
    "RefinementOutcome",
    "RefinementRound",
    "Repair",
    "decomposition_diff",
    "judgment_request",
    "refine_decomposition",
    "refine_repository_decomposition",
    "warning_identity",
]
