"""Deterministic pattern index: blocker-observation/1 -> blocker-pattern/1.

Groups observations by exact ``signature`` + ``classification`` -- no ML
similarity, by design (SI-03). Assigns each cluster a lifecycle ``status``
and computes its ``support`` and ``cost_aggregate`` fields, then separately
tracks anti-thrash proposal state (per-surface uniqueness, cooldown, open
cap, rejected/regressed-history bar) so a downstream drafter (SI-04) and
campaign loop (SI-05) cannot thrash the same target surface. A ``closed``
(accepted) proposal that later recurs in production is recorded via
``regress_proposal`` -- there is no open entry left to close at that
point -- and a ``regressed`` entry bars re-proposal exactly like an
explicit ``rejected`` disposition.

**Status-gate ruling (operator disposition on AC-SI03-1, as corrected by
the first real audit over this campaign's own journals).** The plan text
governs verbatim: "``candidate`` at >=3 observations across >=2 distinct
run *lineages*"; "Single-run findings stay ``observed`` forever." The
ruling's intent is that recurrence requires an *independent* observation
-- not a retry of the same incident. Counting raw ``run_id`` values
defeats exactly that intent under PlanGraph: a node that retries across
graph attempts gets a *fresh* ``run_id`` every attempt
(``...-attempt-3-SI-06``, ``...-attempt-4-SI-06``,
``...-attempt-5-SI-06`` are three run ids for ONE logical node retrying
ONE defect), so a single defect could promote itself to ``candidate`` on
its own retry storm.

``_incident_lineage_key`` therefore folds retries of one logical node
across graph attempts of one logical graph into a single lineage, and
``_status_from_support`` gates on ``distinct_lineage_count >= 2`` (plus
``observation_count >= 3``). ``distinct_run_count`` stays the raw
``run_id`` count -- an honest reporting statistic, never a gate -- and is
always ``>= distinct_lineage_count``. A pattern seen only inside one
logical node's retry chain stays ``observed`` no matter how many attempts
(or how many fresh run ids) pile up.

**Incident-lineage identity and its fallback.** The observation schema is
closed (``blocker-observation.schema.json``) and carries exactly
``run_id``, ``run_kind``, ``node_id`` and ``attempt_id`` of the
correlation data a run descriptor holds -- descriptors themselves
(``parent_correlation.plan_graph_id`` / ``plan_node_id``,
``logical_graph_id`` / ``graph_attempt_id``) are not reachable from this
layer, so the identity is derived at cluster time from what observations
do carry, in this order:

1. ``run_id`` matching the harness's graph-attempt convention
   ``<logical-graph>-attempt-<n>[-<node>]`` yields the logical graph id
   (everything before ``-attempt-<n>``) and, when a node suffix is
   present, the plan node id. ``node_id`` -- populated from event
   correlation when the miner has it -- takes precedence over the run-id
   suffix for the node component. Lineage is then
   ``("node", logical_graph, node)``, or ``("graph", logical_graph)`` for
   an observation mined from the graph-level run itself (retries of one
   logical graph are likewise retries, not independent observations).
2. Otherwise -- no attempt marker, i.e. no correlation to recover --
   lineage falls back to ``("run", run_id, node_id or "")``. This folds
   only attempts *within* one ``run_id`` (which are by construction the
   same run) and never merges two different ``run_id`` values, so an
   unparseable naming scheme can only *under*-count support, never
   fabricate a fold between genuinely unrelated runs.

The fold is likewise conservative in the parseable case: two run ids
collapse only when they differ solely in their graph-attempt ordinal,
which is precisely the harness's own retry naming.

``is_proposable``'s stricter gate (``>= 2`` distinct graph attempts *or*
task suites, additionally required on top of ``candidate`` status) keeps
using ``distinct_lineage_count`` / ``distinct_task_suite_count`` exactly
as SI-03 describes -- and it is the folded lineage count that now feeds
it, so "distinct graph attempts" means attempts that are not retries of
the same incident. See ``is_proposable``.

**Known schema tension, not owned by this node.** ``blocker-pattern
.schema.json`` (SI-01, out of this node's grant) requires
``generalizability.verdict`` to be a non-null member of a closed enum, but
the plan states the verdict "starts null and is filled by the bounded
model step in SI-04" (plan section SI-03). Records built here therefore
carry ``generalizability.verdict: None`` and are not asserted to validate
against ``blocker-pattern.schema.json`` until SI-04 fills the verdict --
neither the schema nor the plan text may be amended from this node, and
AC-SI03-2's schema-conformance requirement is scoped to
``scripts/dev/check_import_boundaries.py``, not the artifact checker.

**Layer placement**: this module imports only ``harness_labs.core``,
``harness_labs.observability``, and the standard library -- never
``harness_labs.plangraph`` -- so ``scripts/dev/check_import_boundaries.py``
(and ``tests/test_import_boundaries.py``) stay green (AC-SI03-2). Emitting
records to ``logs/improvement/patterns/`` and folding partial mining
batches across many audit runs is SI-05's CLI-wiring job, mirroring how
SI-02's ``run_forensics.mine`` stays a pure library with "thin CLI wiring
later" -- this module's functions are pure over whatever observation set
and ledger state the caller supplies.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


PROTOCOL = "blocker-pattern/1"

#: Pattern lifecycle status values this module's clustering step can itself
#: assign. The remaining schema-level statuses (``proposed``, ``addressed``,
#: ``superseded``, ``rejected``) are set by later layers (SI-04 drafting,
#: SI-05 campaign close, human ruling) and are never touched here.
STATUS_OBSERVED = "observed"
STATUS_CANDIDATE = "candidate"

CANDIDATE_MIN_OBSERVATIONS = 3
#: Plan SI-03: "``candidate`` at >=3 observations across >=2 distinct run
#: lineages" -- lineages, not raw run ids (see the module docstring's
#: status-gate ruling and ``_incident_lineage_key``).
CANDIDATE_MIN_DISTINCT_LINEAGES = 2
PROPOSABLE_MIN_ATTEMPTS_OR_SUITES = 2

#: The harness's PlanGraph run-id convention: a logical graph id, the
#: graph attempt ordinal, and (for a node's own feature run) the plan node
#: id -- e.g. ``self-improvement-agent-r2-attempt-4-SI-06``. Retrying a
#: node mints a fresh run id that differs only in ``<n>``.
_RUN_ATTEMPT_PATTERN = re.compile(r"^(?P<graph>.+?)-attempt-(?P<attempt>\d+)(?:-(?P<node>.+))?$")

#: Anti-thrash defaults (plan: "cooldown (>=14 days or >=N new runs)").
DEFAULT_COOLDOWN_DAYS = 14
DEFAULT_COOLDOWN_RUNS = 5
DEFAULT_MAX_OPEN_PROPOSALS = 5

_OPEN = "open"
_REJECTED = "rejected"
_CLOSED = "closed"
_REGRESSED = "regressed"
_LEDGER_STATUSES = frozenset({_OPEN, _REJECTED, _CLOSED, _REGRESSED})


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------


def _signature_key(observation: Mapping[str, Any]) -> tuple[str, str]:
    return (str(observation["signature"]), str(observation["classification"]))


def _pattern_id(signature: str, classification: str) -> str:
    digest = hashlib.sha256(f"{signature}\x00{classification}".encode("utf-8")).hexdigest()
    return f"pattern-{digest[:16]}"


def _observation_ref(observation: Mapping[str, Any]) -> dict[str, str]:
    payload = "\x00".join(
        [
            str(observation["run_id"]),
            str(observation["attempt_id"]),
            str(observation["phase"]),
            str(observation["signature"]),
            str(observation.get("first_event_sequence", "")),
        ]
    ).encode("utf-8")
    return {
        "run_id": str(observation["run_id"]),
        "attempt_id": str(observation["attempt_id"]),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _incident_lineage_key(observation: Mapping[str, Any]) -> tuple[str, ...]:
    """The incident-lineage identity of one observation.

    Two observations share a lineage when they are retries of the *same*
    logical incident: the same plan node of the same logical graph
    (whatever graph attempt each retry happened to be minted under), or
    the same logical graph for observations mined from a graph-level run.
    Independent observations -- a different node, a different logical
    graph, an unrelated run -- always get different keys.

    Derivation and fallback are documented in the module docstring; the
    invariant that matters is one-directional: the fallback may split a
    genuine lineage in two (under-counting support, which can only make
    the ``candidate`` gate harder to cross) but never merges observations
    from genuinely unrelated runs.
    """

    run_id = str(observation["run_id"])
    node_id = observation.get("node_id")
    node_id = str(node_id) if isinstance(node_id, str) and node_id else None

    match = _RUN_ATTEMPT_PATTERN.match(run_id)
    if match is not None:
        logical_graph = match.group("graph")
        node = node_id or match.group("node")
        if node:
            return ("node", logical_graph, node)
        return ("graph", logical_graph)

    # No recoverable correlation: keep the run itself as the lineage, so
    # attempts recorded inside one run fold (they are one run by
    # construction) while distinct run ids never merge.
    return ("run", run_id, node_id or "")


def _support(observations: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "observation_count": len(observations),
        "distinct_run_count": len({str(o["run_id"]) for o in observations}),
        "distinct_lineage_count": len({_incident_lineage_key(o) for o in observations}),
        "distinct_task_suite_count": len({str(o["run_kind"]) for o in observations}),
    }


def _status_from_support(support: Mapping[str, int]) -> str:
    if (
        support["observation_count"] >= CANDIDATE_MIN_OBSERVATIONS
        and support["distinct_lineage_count"] >= CANDIDATE_MIN_DISTINCT_LINEAGES
    ):
        return STATUS_CANDIDATE
    return STATUS_OBSERVED


def is_proposable(pattern: Mapping[str, Any]) -> bool:
    """Additionally-required, stricter gate on top of ``candidate`` status.

    The ``candidate`` gate is itself ``distinct_lineage_count >= 2``, so
    the "attempts" branch of this OR is by construction satisfied once a
    pattern is ``candidate`` -- but that is now a *meaningful* two, not
    two retries of one incident: the lineage identity folds a node's
    retries across graph attempts into one lineage
    (``_incident_lineage_key``). ``distinct_task_suite_count >= 2`` is the
    branch that adds marginal selectivity for this repo's current mining
    fields (``run_kind`` is the sole per-observation task-suite signal
    SI-02 captures); it is kept in the OR verbatim per the plan text.

    The invariant this gate must preserve, per the SI-03 ruling: a
    single-run -- and now a single-incident -- finding is never
    actionable. A pattern whose observations all come from one logical
    node's retry chain has ``distinct_lineage_count == 1``, never reaches
    ``candidate``, and is therefore never proposable, however many
    attempts and fresh run ids it accumulates.
    """

    if pattern.get("status") != STATUS_CANDIDATE:
        return False
    support = pattern["support"]
    return (
        support["distinct_lineage_count"] >= PROPOSABLE_MIN_ATTEMPTS_OR_SUITES
        or support["distinct_task_suite_count"] >= PROPOSABLE_MIN_ATTEMPTS_OR_SUITES
    )


def _cost_stat(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"median": 0.0, "tail": 0.0}
    ordered = sorted(float(value) for value in values)
    return {"median": float(statistics.median(ordered)), "tail": float(ordered[-1])}


def _cost_aggregate(observations: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    wall_clock_ms: list[float] = []
    tokens: list[float] = []
    diff_churn_lines: list[float] = []
    for observation in observations:
        cost = observation.get("resolution_cost") or {}
        wall_clock_ms.append(cost.get("wall_clock_ms", 0))
        diff_churn_lines.append(cost.get("diff_churn_lines", 0))
        if cost.get("tokens") is not None:
            tokens.append(cost["tokens"])
    return {
        "wall_clock_ms": _cost_stat(wall_clock_ms),
        "tokens": _cost_stat(tokens),
        "diff_churn_lines": _cost_stat(diff_churn_lines),
    }


def cluster_observations(
    observations: Iterable[Mapping[str, Any]], *, now: str
) -> list[dict[str, Any]]:
    """Deterministically cluster ``observations`` into ``blocker-pattern/1``
    records.

    ``now`` is the caller-supplied clustering timestamp (ISO-8601), used
    verbatim for ``first_seen_at``/``last_seen_at`` -- observations
    themselves carry no wall-clock field (SI-02 only records
    ``first_event_sequence``), and this function must stay deterministic
    given a fixed input, so it never reads the system clock itself. Callers
    that fold successive mining batches over time are responsible for
    accumulating the full observation set (or otherwise tracking each
    pattern's true first-seen moment) before calling in; this function
    always recomputes clusters from the complete set it is given.

    Output is sorted by ``pattern_id`` for byte-stable results.
    """

    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for observation in observations:
        groups.setdefault(_signature_key(observation), []).append(observation)

    patterns: list[dict[str, Any]] = []
    for (signature, classification), group in groups.items():
        ordered = sorted(
            group,
            key=lambda o: (
                str(o["run_id"]),
                int(o.get("first_event_sequence", 0)),
                str(o["attempt_id"]),
            ),
        )
        support = _support(ordered)
        status = _status_from_support(support)
        refs = sorted(
            (_observation_ref(o) for o in ordered),
            key=lambda ref: (ref["run_id"], ref["attempt_id"], ref["sha256"]),
        )
        fixes_employed = sorted({str(o["resolution"]) for o in ordered if o.get("resolution")})
        patterns.append(
            {
                "protocol": PROTOCOL,
                "pattern_id": _pattern_id(signature, classification),
                "signature": signature,
                "classification": classification,
                "status": status,
                "support": support,
                "first_seen_at": now,
                "last_seen_at": now,
                "observations": refs,
                "cost_aggregate": _cost_aggregate(ordered),
                "fixes_employed": fixes_employed,
                "generalizability": {
                    "verdict": None,
                    "rubric_id": "burden-admission/1",
                    "rationale": "",
                    "counterexamples": [],
                },
                "recurrence": [],
            }
        )

    patterns.sort(key=lambda pattern: pattern["pattern_id"])
    return patterns


# --------------------------------------------------------------------------
# Anti-thrash proposal ledger
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AntiThrashDecision:
    """The outcome of ``evaluate_anti_thrash``: whether a proposal may open,
    and every rule that would block it (not just the first), so a caller
    can report a complete reason list rather than one blocker at a time."""

    allowed: bool
    reasons: tuple[str, ...]


def _parse_timestamp(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _entries_for_surface(
    ledger: Sequence[Mapping[str, Any]], target_surface: str
) -> list[Mapping[str, Any]]:
    return [entry for entry in ledger if entry["target_surface"] == target_surface]


def _entries_for_pattern(
    ledger: Sequence[Mapping[str, Any]], pattern_id: str
) -> list[Mapping[str, Any]]:
    return [entry for entry in ledger if entry["pattern_id"] == pattern_id]


def evaluate_anti_thrash(
    ledger: Sequence[Mapping[str, Any]],
    *,
    target_surface: str,
    pattern_id: str,
    observation_count: int,
    now: str,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    cooldown_runs: int = DEFAULT_COOLDOWN_RUNS,
    new_runs_since_last_close: int = 0,
    max_open_proposals: int = DEFAULT_MAX_OPEN_PROPOSALS,
) -> AntiThrashDecision:
    """Whether a fresh proposal for ``pattern_id`` targeting
    ``target_surface`` may open, given the accumulated ``ledger`` of prior
    proposal entries.

    Every one of SI-03's four anti-thrash rules is checked independently
    and every violated rule is reported:

    - **Per-surface uniqueness**: an already-``open`` entry on the same
      ``target_surface`` blocks a new one, regardless of pattern.
    - **Cooldown**: the most recently closed (``closed`` or ``rejected``)
      entry on this surface must be at least ``cooldown_days`` old *or*
      ``new_runs_since_last_close`` must reach ``cooldown_runs`` -- either
      satisfies the cooldown (plan: ">=14 days or >=N new runs").
    - **Hard cap**: the ledger's total ``open`` count must stay below
      ``max_open_proposals``.
    - **Rejected-history bar**: the most recent ``rejected`` *or*
      ``regressed`` entry for this exact ``pattern_id`` (any surface)
      requires ``observation_count`` to have grown past what it was at
      close time; the same evidence cannot be re-litigated. A
      ``regressed`` entry (see ``regress_proposal``) bars re-proposal
      exactly like an explicit rejection.
    """

    reasons: list[str] = []

    if any(entry["status"] == _OPEN for entry in _entries_for_surface(ledger, target_surface)):
        reasons.append("surface_already_open")

    closed_on_surface = [
        entry
        for entry in _entries_for_surface(ledger, target_surface)
        if entry["status"] in (_CLOSED, _REJECTED)
    ]
    if closed_on_surface:
        latest = max(closed_on_surface, key=lambda entry: _parse_timestamp(entry["closed_at"]))
        elapsed = _parse_timestamp(now) - _parse_timestamp(latest["closed_at"])
        days_cleared = elapsed >= timedelta(days=cooldown_days)
        runs_cleared = new_runs_since_last_close >= cooldown_runs
        if not (days_cleared or runs_cleared):
            reasons.append("cooldown_active")

    open_count = sum(1 for entry in ledger if entry["status"] == _OPEN)
    if open_count >= max_open_proposals:
        reasons.append("open_proposal_cap_reached")

    barred_for_pattern = [
        entry
        for entry in _entries_for_pattern(ledger, pattern_id)
        if entry["status"] in (_REJECTED, _REGRESSED)
    ]
    if barred_for_pattern:
        latest_bar = max(barred_for_pattern, key=lambda entry: _parse_timestamp(entry["closed_at"]))
        if observation_count <= latest_bar["observation_count_at_close"]:
            reasons.append("rejected_without_new_observations")

    return AntiThrashDecision(allowed=not reasons, reasons=tuple(reasons))


def open_proposal(
    ledger: Sequence[Mapping[str, Any]],
    *,
    target_surface: str,
    pattern_id: str,
    observation_count: int,
    now: str,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    cooldown_runs: int = DEFAULT_COOLDOWN_RUNS,
    new_runs_since_last_close: int = 0,
    max_open_proposals: int = DEFAULT_MAX_OPEN_PROPOSALS,
) -> list[dict[str, Any]]:
    """Append a new ``open`` ledger entry, refusing (rather than silently
    admitting) any proposal ``evaluate_anti_thrash`` would block."""

    decision = evaluate_anti_thrash(
        ledger,
        target_surface=target_surface,
        pattern_id=pattern_id,
        observation_count=observation_count,
        now=now,
        cooldown_days=cooldown_days,
        cooldown_runs=cooldown_runs,
        new_runs_since_last_close=new_runs_since_last_close,
        max_open_proposals=max_open_proposals,
    )
    if not decision.allowed:
        raise ValueError(f"anti-thrash gate refused proposal: {', '.join(decision.reasons)}")

    entry = {
        "target_surface": target_surface,
        "pattern_id": pattern_id,
        "status": _OPEN,
        "opened_at": now,
        "closed_at": None,
        "observation_count_at_open": observation_count,
        "observation_count_at_close": None,
    }
    return [dict(item) for item in ledger] + [entry]


def close_proposal(
    ledger: Sequence[Mapping[str, Any]],
    *,
    target_surface: str,
    pattern_id: str,
    disposition: str,
    observation_count: int,
    now: str,
) -> list[dict[str, Any]]:
    """Close the open entry matching ``target_surface``/``pattern_id``.

    ``disposition`` is either ``"rejected"`` (feeds the rejected-history
    bar) or ``"closed"`` (any other terminal disposition -- accepted or
    waived -- which does not bar re-proposal on its own). A ``"closed"``
    entry that later recurs in production is recorded with
    ``regress_proposal`` instead of being re-closed here."""

    if disposition not in (_REJECTED, _CLOSED):
        raise ValueError(f"unknown proposal disposition: {disposition!r}")

    updated: list[dict[str, Any]] = []
    closed_one = False
    for entry in ledger:
        if (
            not closed_one
            and entry["status"] == _OPEN
            and entry["target_surface"] == target_surface
            and entry["pattern_id"] == pattern_id
        ):
            updated.append(
                {
                    **dict(entry),
                    "status": disposition,
                    "closed_at": now,
                    "observation_count_at_close": observation_count,
                }
            )
            closed_one = True
        else:
            updated.append(dict(entry))

    if not closed_one:
        raise ValueError(
            f"no open proposal for target_surface={target_surface!r} pattern_id={pattern_id!r}"
        )
    return updated


def regress_proposal(
    ledger: Sequence[Mapping[str, Any]],
    *,
    target_surface: str,
    pattern_id: str,
    observation_count: int,
    now: str,
) -> list[dict[str, Any]]:
    """Record that a previously ``closed`` (accepted) proposal recurred in
    production.

    Plan section SI-03 names a "rejected/regressed history bar", and plan
    SI-05 (lines 279-281) requires a post-close recurrence to bar
    re-proposal without new evidence -- but at that moment there is no
    ``open`` entry left to close. This appends a ``regressed`` entry
    instead, so ``evaluate_anti_thrash``'s rejected-history bar fires for
    this ``pattern_id`` exactly as it would for an explicit rejection.
    """

    closed_for_pattern_and_surface = [
        entry
        for entry in _entries_for_surface(ledger, target_surface)
        if entry["pattern_id"] == pattern_id and entry["status"] == _CLOSED
    ]
    if not closed_for_pattern_and_surface:
        raise ValueError(
            f"no closed proposal for target_surface={target_surface!r} pattern_id={pattern_id!r}"
        )
    latest_closed = max(
        closed_for_pattern_and_surface, key=lambda entry: _parse_timestamp(entry["closed_at"])
    )

    entry = {
        "target_surface": target_surface,
        "pattern_id": pattern_id,
        "status": _REGRESSED,
        "opened_at": latest_closed["closed_at"],
        "closed_at": now,
        "observation_count_at_open": latest_closed["observation_count_at_close"],
        "observation_count_at_close": observation_count,
    }
    return [dict(item) for item in ledger] + [entry]
