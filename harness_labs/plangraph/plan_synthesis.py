"""Plan synthesis (DTR-LK-SYN, DTR-F6): the ledger's open findings -> a
decomposition JSON the campaign driver's round contract accepts.

:func:`plan_synthesis` turns :meth:`ConvergenceLedger.open_findings` into a
``plan-graph-plan/1`` payload: one repair run per connected component of
open findings' ``required_paths`` (ownership derives from ``required_paths``
alone -- two findings share a run iff they share a required path, directly
or through a chain of other findings, and never by any other attribute, so
dependency-unordered repair runs can never hold overlapping grants), plus
one join-and-regression run that ``depends_on`` every repair run and so is
the unique sink :func:`scripts.run_convergence_campaign.join_regression_node_id`
resolves. Every synthesized criterion carries a trailing
``OBSERVABLE:{"kind": ..., "referent": ...}`` annotation
(:func:`harness_labs.plangraph.decomposition_conformance.parse_observable`
recognizes it), so a synthesized plan never trips ``S5_MISSING_OBSERVABLE``.
The raw payload is returned only after round-tripping through
``plan_graph_contract.canonical_plan_graph_payload`` -- the same call that
enforces the contract's closed top-level key set, so synthesis cannot invent
a field the contract does not already know.

:func:`plan_synthesis` takes only a ``ConvergenceLedger`` and calls exactly
one accessor on it, :meth:`ConvergenceLedger.open_findings`; it never folds
the journal itself (``dtr-lk``: "synthesis never re-folds the journal").
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from harness_labs.plangraph.decomposition_conformance import (
    OBSERVABLE_KINDS,
    parse_observable,
)
from harness_labs.plangraph.plan_graph_contract import (
    PLAN_GRAPH_PROTOCOL,
    PlanGraphContractError,
    canonical_plan_graph_payload,
)

DEFAULT_JOIN_RUN_ID = "join-regression"
DEFAULT_JOIN_OBJECTIVE = (
    "Run the round's full regression verification across every repair "
    "grant and record the result."
)
DEFAULT_JOIN_ALLOWED_PATH = "docs/development/regression-report.md"
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 900.0
DEFAULT_JOIN_VERIFICATION_TIMEOUT_SECONDS = 3600.0
DEFAULT_OBSERVABLE_KIND = "file"


class PlanSynthesisError(ValueError):
    """Raised when ``plan_synthesis`` cannot build a valid decomposition."""


class _OpenFindingsSource(Protocol):
    """The one accessor ``plan_synthesis`` needs from a ledger-shaped
    object -- see the module docstring: never anything wider than
    ``open_findings()``."""

    def open_findings(self) -> Sequence[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class PlanSynthesisResult:
    """A synthesized decomposition, paired with the exact finding ownership
    it computed -- so a caller feeding both straight into
    ``scripts.run_convergence_campaign.validate_round_grants`` checks
    synthesis against itself, never a second, possibly-diverging copy."""

    decomposition: dict[str, Any]
    findings_by_run: dict[str, list[dict[str, Any]]]
    join_run_id: str


def plan_synthesis(
    ledger: _OpenFindingsSource,
    *,
    plan_path: str,
    plan_section_id: str,
    plan_section_heading: str,
    join_run_id: str = DEFAULT_JOIN_RUN_ID,
    join_objective: str = DEFAULT_JOIN_OBJECTIVE,
    join_allowed_path: str = DEFAULT_JOIN_ALLOWED_PATH,
    verification_timeout_seconds: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
    join_verification_timeout_seconds: float = DEFAULT_JOIN_VERIFICATION_TIMEOUT_SECONDS,
    observable_kind: str = DEFAULT_OBSERVABLE_KIND,
    functionality_tests: Sequence[Mapping[str, Any]] = (),
    referenced_artifacts: Sequence[str] = (),
    repository: Path | None = None,
    base_commit: str | None = None,
    verification_argv_builder: Callable[[str], Sequence[str]] | None = None,
) -> PlanSynthesisResult:
    """Synthesize one round's decomposition from ``ledger.open_findings()``.

    ``plan_path``/``plan_section_id``/``plan_section_heading`` name the
    committed plan document and the one section every synthesized run cites
    (the campaign's plan document is domain-specific and known only to the
    caller; synthesis cannot discover it). ``observable_kind`` is the one
    :data:`~harness_labs.plangraph.decomposition_conformance.OBSERVABLE_KINDS`
    every synthesized criterion declares; each repair run's observable
    referent is the lexicographically-first path in its own ``allowed_paths``
    (always a member of its own grants, so a ``"file"`` kind is always
    reachable), and the join run's referent is ``join_allowed_path``.

    ``repository``/``base_commit`` -- when both are given -- resolve every
    synthesized ``path_intents`` action against what actually exists at
    ``base_commit`` (``"modify"`` when the path is already there, ``"create"``
    when it is not), so the choice ``plan_approval._validate_intents`` checks
    at approve time is never a static guess. Omitting them falls back to the
    previous static guess (``"modify"`` for repair grants, ``"create"`` for
    the join report), which a caller with no repository access cannot avoid.

    ``verification_argv_builder`` -- when given -- replaces the default
    verification command (a referent-existence check -- real, but minimal:
    it proves the referent is present, not that the round's regression
    suite passes) for every synthesized run; it is called once per run with
    that run's own observable referent (repair runs: the referent named in
    ``_build_repair_run``; the join run: ``join_allowed_path``) and must
    return an argv that keeps the referent reachable from it
    (``decomposition_conformance`` S6), which is the caller's responsibility
    to preserve, exactly as the default builder does.

    Raises :class:`PlanSynthesisError` when there are no open findings to
    plan, when ``observable_kind`` is not recognized, when ``join_run_id``
    collides with a synthesized repair run id, or when exactly one of
    ``repository``/``base_commit`` is given.
    """

    if observable_kind not in OBSERVABLE_KINDS:
        raise PlanSynthesisError(
            f"observable_kind must be one of {sorted(OBSERVABLE_KINDS)}"
        )
    if (repository is None) != (base_commit is None):
        raise PlanSynthesisError(
            "repository and base_commit must be given together, or not at all"
        )

    findings = tuple(ledger.open_findings())
    if not findings:
        raise PlanSynthesisError(
            "plan_synthesis requires at least one open finding"
        )

    path_exists: Callable[[str], bool] | None = None
    if repository is not None and base_commit is not None:
        path_exists = lambda path: _path_exists_at_commit(  # noqa: E731
            repository, base_commit, path
        )
    build_argv = verification_argv_builder or _default_verification_argv

    groups = _group_by_required_paths(findings)

    acceptance_criteria: dict[str, str] = {}
    findings_by_run: dict[str, list[dict[str, Any]]] = {}
    repair_runs: list[dict[str, Any]] = []
    repair_run_ids: list[str] = []
    for position, group in enumerate(groups, start=1):
        run_id = f"repair-{position}"
        run, criterion_id, criterion_text = _build_repair_run(
            run_id=run_id,
            findings=group,
            plan_section_id=plan_section_id,
            verification_timeout_seconds=verification_timeout_seconds,
            observable_kind=observable_kind,
            path_exists=path_exists,
            build_argv=build_argv,
        )
        repair_runs.append(run)
        repair_run_ids.append(run_id)
        acceptance_criteria[criterion_id] = criterion_text
        findings_by_run[run_id] = list(group)

    if join_run_id in repair_run_ids:
        raise PlanSynthesisError(
            f"join_run_id {join_run_id!r} collides with a synthesized "
            "repair run id"
        )

    join_run, join_criterion_id, join_criterion_text = _build_join_run(
        join_run_id=join_run_id,
        repair_run_ids=repair_run_ids,
        plan_section_id=plan_section_id,
        join_allowed_path=join_allowed_path,
        join_objective=join_objective,
        verification_timeout_seconds=join_verification_timeout_seconds,
        observable_kind=observable_kind,
        path_exists=path_exists,
        build_argv=build_argv,
    )
    acceptance_criteria[join_criterion_id] = join_criterion_text

    for criterion_id, text in acceptance_criteria.items():
        if parse_observable(text) is None:
            raise PlanSynthesisError(
                f"synthesized criterion {criterion_id!r} failed to "
                "round-trip through parse_observable"
            )

    raw_payload = {
        "protocol": PLAN_GRAPH_PROTOCOL,
        "plan": plan_path,
        "plan_sections": {plan_section_id: plan_section_heading},
        "acceptance_criteria": acceptance_criteria,
        "runs": [*repair_runs, join_run],
        "functionality_tests": [dict(item) for item in functionality_tests],
        "referenced_artifacts": list(referenced_artifacts),
    }
    try:
        decomposition = canonical_plan_graph_payload(raw_payload)
    except PlanGraphContractError as exc:
        raise PlanSynthesisError(str(exc)) from exc

    return PlanSynthesisResult(
        decomposition=decomposition,
        findings_by_run=findings_by_run,
        join_run_id=join_run_id,
    )


# ---------------------------------------------------------------------------
# Ownership: connected components of required_paths, and nothing else
# ---------------------------------------------------------------------------


def _group_by_required_paths(
    findings: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Union-find components over ``required_paths``: two findings land in
    the same group iff they share a required path, directly or through a
    chain of other findings -- ownership derives from ``required_paths``
    alone, never from category, severity, or any other attribute. Groups are
    returned in the order their lowest-index member first appears, so the
    result is deterministic whenever ``findings`` is (as
    ``ConvergenceLedger.open_findings()`` guarantees)."""

    parent = list(range(len(findings)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[max(root_first, root_second)] = min(root_first, root_second)

    first_owner: dict[str, int] = {}
    for index, finding in enumerate(findings):
        for path in finding.get("required_paths") or ():
            owner = first_owner.get(path)
            if owner is None:
                first_owner[path] = index
            else:
                union(owner, index)

    groups: dict[int, list[int]] = {}
    for index in range(len(findings)):
        groups.setdefault(find(index), []).append(index)
    return [
        [findings[member] for member in groups[root]]
        for root in sorted(groups)
    ]


# ---------------------------------------------------------------------------
# Run/criterion construction
# ---------------------------------------------------------------------------


def _observable_annotation(text: str, *, kind: str, referent: str) -> str:
    observable = json.dumps({"kind": kind, "referent": referent}, sort_keys=True)
    return f"{text.rstrip('.')}. OBSERVABLE:{observable}"


def _default_verification_argv(referent: str) -> list[str]:
    # A real, if minimal, check -- not a comment inside a command that
    # always exits 0: the referent must actually exist on disk, so the
    # default gate can fail (unlike a bare ``pass``) without requiring any
    # product-specific knowledge synthesis has no way to have. It also
    # carries the observable referent verbatim, so a ``"file"``-kind
    # observable is always reachable from ``verification_argv``
    # (``decomposition_conformance`` S6) regardless of which kind the caller
    # selected. ``python3`` (not ``sys.executable``) so the recorded gate
    # evidence resolves via PATH -- the same convention every committed
    # decomposition in this repository uses -- instead of pinning approval
    # to this host's own interpreter file. A caller with a real regression
    # command to run replaces this entirely via ``verification_argv_builder``.
    script = f"import pathlib, sys; sys.exit(0 if pathlib.Path({referent!r}).exists() else 1)"
    return ["python3", "-c", script]


def _path_exists_at_commit(repository: Path, commit: str, path: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _intent_action(
    path_exists: Callable[[str], bool] | None, path: str, *, default: str
) -> str:
    if path_exists is None:
        return default
    return "modify" if path_exists(path) else "create"


def _build_repair_run(
    *,
    run_id: str,
    findings: Sequence[Mapping[str, Any]],
    plan_section_id: str,
    verification_timeout_seconds: float,
    observable_kind: str,
    path_exists: Callable[[str], bool] | None,
    build_argv: Callable[[str], Sequence[str]],
) -> tuple[dict[str, Any], str, str]:
    allowed_paths = sorted(
        {
            str(path)
            for finding in findings
            for path in finding.get("required_paths") or ()
        }
    )
    if not allowed_paths:
        raise PlanSynthesisError(
            f"{run_id!r} owns no required_paths to grant"
        )
    referent = allowed_paths[0]
    owned = "; ".join(
        f"{finding['file']}:{finding['subject']}" for finding in findings
    )
    objective = f"Repair finding(s) {owned}."
    criterion_id = f"{run_id}-criterion"
    criterion_text = _observable_annotation(
        f"every finding owned by {run_id!r} is observed fixed",
        kind=observable_kind,
        referent=referent,
    )
    run = {
        "id": run_id,
        "objective": objective,
        "plan_sections": [plan_section_id],
        "criteria": [criterion_id],
        "depends_on": [],
        "allowed_paths": allowed_paths,
        "path_intents": [
            {
                "path": path,
                "action": _intent_action(path_exists, path, default="modify"),
            }
            for path in allowed_paths
        ],
        "verification_argv": list(build_argv(referent)),
        "verification_timeout_seconds": verification_timeout_seconds,
        "verification_required_paths": [],
    }
    return run, criterion_id, criterion_text


def _build_join_run(
    *,
    join_run_id: str,
    repair_run_ids: Sequence[str],
    plan_section_id: str,
    join_allowed_path: str,
    join_objective: str,
    verification_timeout_seconds: float,
    observable_kind: str,
    path_exists: Callable[[str], bool] | None,
    build_argv: Callable[[str], Sequence[str]],
) -> tuple[dict[str, Any], str, str]:
    criterion_id = f"{join_run_id}-criterion"
    criterion_text = _observable_annotation(
        "the round's full regression verification passes across every "
        "repair grant",
        kind=observable_kind,
        referent=join_allowed_path,
    )
    run = {
        "id": join_run_id,
        "objective": join_objective,
        "plan_sections": [plan_section_id],
        "criteria": [criterion_id],
        "depends_on": list(repair_run_ids),
        "allowed_paths": [join_allowed_path],
        "path_intents": [
            {
                "path": join_allowed_path,
                "action": _intent_action(path_exists, join_allowed_path, default="create"),
            }
        ],
        "verification_argv": list(build_argv(join_allowed_path)),
        "verification_timeout_seconds": verification_timeout_seconds,
        "verification_required_paths": [],
    }
    return run, criterion_id, criterion_text


__all__ = [
    "DEFAULT_JOIN_ALLOWED_PATH",
    "DEFAULT_JOIN_OBJECTIVE",
    "DEFAULT_JOIN_RUN_ID",
    "DEFAULT_JOIN_VERIFICATION_TIMEOUT_SECONDS",
    "DEFAULT_OBSERVABLE_KIND",
    "DEFAULT_VERIFICATION_TIMEOUT_SECONDS",
    "PlanSynthesisError",
    "PlanSynthesisResult",
    "plan_synthesis",
]
