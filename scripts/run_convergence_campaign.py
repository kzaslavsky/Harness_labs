#!/usr/bin/env python3
"""Convergence campaign driver (CC-04, ``driver-steps``).

A sequencer for the campaign's ``measure -> ingest -> rule -> plan -> approve
-> run -> close`` step machine (``docs/development/convergence-campaign-plan.md``,
``driver-steps``). It delegates node launching, approval, and resume to
machinery that already exists elsewhere in the repository rather than
reimplementing any of it:

* **approve** delegates to ``harness_labs.plangraph.plan_approval``
  (``prepare_approval`` / ``issue_receipt`` / ``warning_identity``) for every
  admission gate and receipt; this module only adds the two checks the plan
  text and warning-acknowledgment posture the driver itself is responsible
  for (byte-identity preconditions, an acknowledgment gate stricter than
  ``issue_receipt``'s own high-severity-only gate) and never writes
  ``operator-approval.json`` -- it halts for the human-written file.
* **run** delegates to ``scripts/run_plan_graph.py`` (a subprocess, the
  repository's one PlanGraph execution entry point) for every node dispatch,
  budget, and finalize decision.
* **resume** delegates to ``scripts/plan_graph_autoresume.py``
  (``find_predecessor`` / ``reconcile_frontier``) to reconstruct every resume
  argument from ``escalation.json`` -- this module adds no second reading of
  that artifact.
* **ledger/checkpoint** state is CC-01 (``harness_labs.plangraph.convergence_ledger``)
  and CC-02 (``harness_labs.plangraph.convergence_campaign``); this module
  only sequences calls into them and keeps campaign-scoped bookkeeping
  (round number, repair-round budget, the join-and-regression node id, the
  prior round's repair grants) in the checkpoint's ``state`` mapping.

Per-round attempt relaunch (quiescence, frontier reconciliation, no-progress
bounding) is likewise not reimplemented here: it is
``scripts/plan_graph_autoresume.py``'s job (``build-order-cc-04``); this
module owns only campaign-round sequencing and the ledger/checkpoint
interactions around it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.core.controller_results import FINDING_SEVERITIES  # noqa: E402
from harness_labs.plangraph.convergence_campaign import (  # noqa: E402
    CONFIG_AMENDMENT_RATIO_THRESHOLD_KEY,
    CONFIG_RECALL_REPORT_DIGEST_KEY,
    CONFIG_RECALL_THRESHOLD_KEY,
    CONFIG_SANITIZER_KEY,
    CampaignArtifactStore,
    CampaignCheckpoint,
    CampaignCheckpointSequenceError,
    CampaignCheckpointStaleError,
    CampaignCheckpointStore,
    ConvergenceCampaignError,
    pin_target,
)
from harness_labs.plangraph.convergence_ledger import (  # noqa: E402
    RECORD_KIND_FINDING_OPENED,
    ConvergenceLedger,
    ConvergenceLedgerError,
)
from harness_labs.plangraph.finding_history import (  # noqa: E402
    FindingHistoryError,
    fold_campaigns,
)
from harness_labs.plangraph.plan_approval import (  # noqa: E402
    REPOSITORY_IDENTITY_PATH,
    SIBLING_OVERLAP_WARNING,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_graph import PlanGraph  # noqa: E402
from harness_labs.plangraph.plan_graph_contract import (  # noqa: E402
    load_repository_id,
    path_is_allowed,
)
from harness_labs.plangraph.plan_refinement import (  # noqa: E402
    RefinementOutcome,
    refine_repository_decomposition,
)
from harness_labs.plangraph.plan_synthesis import (  # noqa: E402
    PlanSynthesisError,
    plan_synthesis,
)

from scripts.plan_graph_autoresume import (  # noqa: E402
    _RESUMABLE_ATTEMPT_STATUSES,
    AutoresumeDriver,
    AutoresumeError,
    find_predecessor,
    reconcile_frontier,
)


PROTOCOL = "convergence-campaign-driver/1"
DEFAULT_MAX_REPAIR_ROUNDS = 3
FINDINGS_OWNERS_PATHS_RELATIVE_PATH = "findings-owners-paths.json"
#: Protocol tag for the recurrence-annotation artifact ``ingest`` seals
#: through the existing :class:`CampaignArtifactStore` (EM-D2, ``em-history``
#: production consumer) -- never a new ``state-ledger`` record kind, and
#: never written to the ledger's own journal.
RECURRENCE_ANNOTATION_PROTOCOL = "convergence-campaign-recurrence-annotation/1"

#: The base ``run_plan_graph.py run`` invocation ``resume_directive_from_escalation``
#: appends its directive flags to when no campaign-specific launcher is given
#: (``AutoresumeDriver.resume_argv`` -- see the module docstring: resume is
#: delegated wholesale, not reimplemented).
DEFAULT_RESUME_COMMAND: tuple[str, ...] = (
    sys.executable, str(Path(__file__).resolve().parent / "run_plan_graph.py"), "run",
)

#: Every checkpoint ``lifecycle`` value the driver may save, one per step of
#: the measure/ingest/rule/plan/approve/run/close machine (plus the two
#: campaign-terminal states) -- ``AC-CC04-7``.
LIFECYCLES = (
    "opened",
    "measuring", "measured",
    "ingesting", "ingested",
    "ruling", "ruled",
    "planning", "planned",
    "approving", "approved",
    "running", "run_succeeded", "run_blocked",
    "closing", "closed",
    "succeeded", "blocked",
)


class ConvergenceCampaignDriverError(ValueError):
    """Raised when the driver refuses to proceed."""


class UnacknowledgedWarningsError(ConvergenceCampaignDriverError):
    """Raised when the approval packet would carry an unacknowledged warning."""


class ByteIdentityViolation(ConvergenceCampaignDriverError):
    """Raised when a criterion quote or an objective fails its byte-identity check."""


class StallEscalation(ConvergenceCampaignDriverError):
    """Raised in place of launching another round when the ledger has stalled."""


class RepairRoundBoundExceeded(ConvergenceCampaignDriverError):
    """Raised when the repair-round bound blocks another ``plan`` step."""


class PredecessorResumableError(ConvergenceCampaignDriverError):
    """Raised when ``campaign_opened`` is refused because a predecessor graph
    is still resumable (``AC-CC04-4``)."""


class SanitizerFailure(ConvergenceCampaignDriverError):
    """Raised when the configured ``pre_journal_sanitizer`` hook cannot be
    resolved or fails while running -- one of ``bounds-termination``'s named
    blocked end states."""


class TargetAmendedWithoutScopeError(ConvergenceCampaignDriverError):
    """Raised in place of a ``plan`` step while ``ConvergenceLedger.is_blocked()``
    is true -- a ``target_amended`` record with no stated
    ``invalidation_scope`` -- one of ``bounds-termination``'s named blocked
    end states."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# Campaign config and the ``pre_journal_sanitizer`` hook (``measurer-requirements``)
# ---------------------------------------------------------------------------


def campaign_config(ledger: ConvergenceLedger) -> dict[str, Any]:
    """The ``campaign_opened`` record's ``config`` mapping (sanitizer hook,
    recall and amendment-ratio thresholds), read back from the ledger --
    the one place the config is durably recorded (``pin_target``)."""

    for record in ledger.records():
        if record.get("type") == "campaign_opened":
            return dict(record.get("config") or {})
    return {}


def identity_pre_journal_sanitizer(text: str) -> str:
    """A no-op ``pre_journal_sanitizer`` hook for domains with nothing to
    redact -- a real, resolvable ``module:callable`` reference
    (``scripts.run_convergence_campaign:identity_pre_journal_sanitizer``),
    not a placeholder."""

    return text


def resolve_pre_journal_sanitizer(reference: str) -> Callable[[str], str]:
    """Resolve a ``module:callable`` reference to the hook it names."""

    module_name, _, attribute = reference.partition(":")
    if not module_name or not attribute:
        raise SanitizerFailure(
            f"pre_journal_sanitizer hook {reference!r} must be a 'module:callable' reference"
        )
    try:
        module = importlib.import_module(module_name)
        hook = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise SanitizerFailure(
            f"pre_journal_sanitizer hook {reference!r} could not be resolved: {exc}"
        ) from exc
    if not callable(hook):
        raise SanitizerFailure(f"pre_journal_sanitizer hook {reference!r} is not callable")
    return hook


def _text_hook_reference(config: Mapping[str, Any]) -> str | None:
    """Extract the ``module:callable`` text-hook reference from either the
    legacy string form or the ``{"text": ..., "binary": {...}}`` mapping
    form of ``CONFIG_SANITIZER_KEY`` (``dtr-sn``).

    A mapping with no ``text`` entry raises :class:`SanitizerFailure` --
    never the ``AttributeError`` a bare ``.partition(":")`` on a ``dict``
    would raise (AC-SN-4).
    """

    reference = config.get(CONFIG_SANITIZER_KEY)
    if reference is None or reference == "":
        return None
    if isinstance(reference, str):
        return reference
    if isinstance(reference, Mapping):
        text_reference = reference.get("text")
        if not isinstance(text_reference, str) or not text_reference.strip():
            raise SanitizerFailure(
                "pre_journal_sanitizer mapping config carries no non-empty "
                f"'text' hook entry: {reference!r}"
            )
        return text_reference
    raise SanitizerFailure(
        "pre_journal_sanitizer config must be a string or a "
        f"{{'text': ..., 'binary': {{...}}}} mapping, got {type(reference).__name__}"
    )


def sanitize_before_journaling(config: Mapping[str, Any], text: str) -> str:
    """Pass ``text`` through the configured hook before it is journaled,
    digested, or sealed (``measurer-requirements``).

    A campaign with no configured hook journals text unsanitized -- the hook
    is optional at the config layer (``build_campaign_config`` requires a
    non-empty string, but a checkpoint state built outside that helper may
    carry none). Any failure to resolve or run the hook is a
    :class:`SanitizerFailure`, one of ``bounds-termination``'s named blocked
    end states, rather than a silent pass-through. ``config``'s
    ``CONFIG_SANITIZER_KEY`` may hold either the legacy string reference or
    the mapping form -- :func:`_text_hook_reference` resolves the ``text``
    hook out of the mapping exactly as the legacy string is applied
    (``dtr-sn``, AC-SN-4).
    """

    reference = _text_hook_reference(config)
    if not reference:
        return text
    hook = resolve_pre_journal_sanitizer(reference)
    try:
        sanitized = hook(text)
    except SanitizerFailure:
        raise
    except Exception as exc:  # the hook is untrusted campaign config, not our code
        raise SanitizerFailure(
            f"pre_journal_sanitizer hook {reference!r} raised while sanitizing: {exc}"
        ) from exc
    if not isinstance(sanitized, str):
        raise SanitizerFailure(
            f"pre_journal_sanitizer hook {reference!r} must return str, got "
            f"{type(sanitized).__name__}"
        )
    return sanitized


# ---------------------------------------------------------------------------
# Approval packet rendering (AC-CC04-1, AC-CC04-8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalPacket:
    subject_path: Path
    gate_evidence_path: Path
    findings_table: tuple[Mapping[str, Any], ...]
    warnings: tuple[Mapping[str, Any], ...]
    sibling_overlap_warnings: tuple[Mapping[str, Any], ...]
    refinement_status: str = ""
    refinement_reason: str = ""
    findings_owners_paths_table_path: str | None = None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "subject": str(self.subject_path),
            "gate_evidence": str(self.gate_evidence_path),
            "findings_table": [dict(row) for row in self.findings_table],
            "warnings": [dict(item) for item in self.warnings],
            "sibling_overlap_warnings": [dict(item) for item in self.sibling_overlap_warnings],
            "refinement_status": self.refinement_status,
            "refinement_reason": self.refinement_reason,
            "findings_owners_paths_table_path": self.findings_owners_paths_table_path,
        }


def render_findings_owners_paths_table(
    findings_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], ...]:
    """One row per (owner run, owned finding), sorted for a stable rendering."""

    rows = []
    for run_id in sorted(findings_by_run):
        for finding in findings_by_run[run_id]:
            rows.append(
                {
                    "run_id": run_id,
                    "file": finding.get("file"),
                    "subject": finding.get("subject"),
                    "required_paths": sorted(finding.get("required_paths") or ()),
                }
            )
    rows.sort(key=lambda row: (row["run_id"], row["file"] or "", row["subject"] or ""))
    return tuple(rows)


def sibling_overlap_warnings_from_gate_evidence(
    gate_evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Every ``sibling-allowed-path-overlap`` warning gate-evidence.json carries."""

    return tuple(
        dict(warning)
        for warning in gate_evidence.get("warnings") or ()
        if isinstance(warning, Mapping) and warning.get("kind") == SIBLING_OVERLAP_WARNING
    )


def unacknowledged_warnings(
    gate_evidence: Mapping[str, Any],
    acknowledgements: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    """Every gate-evidence warning, of any severity, absent an acknowledgment.

    Stricter than ``plan_approval.issue_receipt``'s own gate, which only hard
    -blocks high-severity warnings: the driver's own precondition for
    proceeding past ``approve`` is that *every* warning it read from
    ``gate-evidence.json`` -- including advisory ones -- has been
    acknowledged, so nothing is silently carried past the human (``AC-CC04-1``).
    """

    acknowledged = {
        str(entry.get("warning_sha256")) for entry in acknowledgements
    }
    return tuple(
        dict(warning)
        for warning in gate_evidence.get("warnings") or ()
        if isinstance(warning, Mapping) and warning_identity(warning) not in acknowledged
    )


def check_criteria_byte_identity(
    decomposition: Mapping[str, Any],
    criteria_texts_by_run: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> tuple[str, ...]:
    """Every quoted criterion text a run's own packet material carries must be
    byte-identical to ``decomposition["acceptance_criteria"][id]``.

    When the caller carries no external packet material to cross-check (the
    common case: nothing outside the decomposition itself quotes a
    criterion), ``criteria_texts_by_run`` is derived from the decomposition's
    own ``runs[*]["criteria"]`` ids against its own
    ``acceptance_criteria`` -- so the check always runs (``AC-CC04-8``
    is never opt-out-able), degrading to a tautology only when there is
    nothing external to diverge, rather than being skipped. The shipped CLI
    exposes this cross-check material via ``approve prepare
    --criteria-texts-by-run-file``, so a caller with genuine external packet
    quotes to check is not limited to the tautological default.

    Returns ``"{run_id}:{criterion_id}"`` for each mismatch or unknown id.
    """

    acceptance_criteria = decomposition.get("acceptance_criteria") or {}
    if not criteria_texts_by_run:
        criteria_texts_by_run = {
            str(run.get("id")): [
                {"id": criterion_id, "text": acceptance_criteria.get(criterion_id)}
                for criterion_id in run.get("criteria") or ()
            ]
            for run in decomposition.get("runs") or ()
        }
    violations: list[str] = []
    for run_id in sorted(criteria_texts_by_run):
        for entry in criteria_texts_by_run[run_id]:
            criterion_id = entry.get("id")
            text = entry.get("text")
            canonical = acceptance_criteria.get(criterion_id)
            if canonical is None or text != canonical:
                violations.append(f"{run_id}:{criterion_id}")
    return tuple(violations)


def _heading_level(line: str) -> int:
    return len(line) - len(line.lstrip("#"))


def extract_plan_section(plan_text: str, heading: str) -> str:
    """The body of one markdown section: from its heading line (inclusive) to
    the next heading of equal or shallower depth, or end of file.

    Returns the empty string when ``heading`` is not found verbatim as its
    own line -- callers treat that the same as an objective that is absent
    from the section, since there is no section to search.
    """

    lines = plan_text.splitlines()
    heading = heading.strip()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return ""
    level = _heading_level(heading)
    end = len(lines)
    if level:
        for index in range(start + 1, len(lines)):
            candidate = lines[index]
            if candidate.startswith("#") and _heading_level(candidate) <= level:
                end = index
                break
    return "\n".join(lines[start:end])


def check_objective_in_plan_text(
    decomposition: Mapping[str, Any], plan_text: str,
) -> tuple[str, ...]:
    """Every run's ``objective`` must appear verbatim in the plan text of the
    sections it cites (``decomposition["plan_sections"]``).

    The existing harness (``plan_graph_contract.canonical_plan_graph_payload``)
    already validates that every cited section slug is a *key* the plan
    declares; this adds the text-inclusion check the plan doc's opening
    paragraph names as new: "the harness validates key references, not text
    inclusion; the driver adds the byte-identity check (``AC-CC04-8``)."
    Returns the run ids whose objective could not be found.
    """

    sections_map = decomposition.get("plan_sections") or {}
    violations: list[str] = []
    for run in decomposition.get("runs") or ():
        run_id = str(run.get("id"))
        objective = str(run.get("objective") or "")
        bodies = [
            extract_plan_section(plan_text, sections_map[slug])
            for slug in run.get("plan_sections") or ()
            if slug in sections_map
        ]
        combined = "\n".join(bodies)
        if objective.strip() and objective not in combined:
            violations.append(run_id)
    return tuple(violations)


def check_pristine_worktree(repository: Path) -> None:
    """Refuse ``approve`` unless the base worktree is pristine, untracked
    files included (``driver-steps`` step 5's precondition list)."""

    completed = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    if completed.stdout.strip():
        raise ConvergenceCampaignDriverError(
            "approve requires a pristine base worktree (untracked files "
            "included); git status --porcelain reported:\n" + completed.stdout
        )


def run_admission_refinement(
    repository: Path, decomposition_path: Path, *, max_rounds: int = 8,
) -> RefinementOutcome:
    """Run the admission refinement loop first, as ``driver-steps`` step 5
    requires (``approve_plan.py refine`` / ``plan_refinement.refine_decomposition``).

    Narrowing repairs the loop can make on its own are proposals the
    operator re-commits (S3: "the analyzer never mutates an approved
    decomposition in place"), so this never writes the decomposition back;
    the outcome is surfaced on :class:`ApprovalPacket` for the operator.
    """

    return refine_repository_decomposition(
        repository=repository, decomposition_path=decomposition_path, max_rounds=max_rounds,
    )


def commit_findings_owners_paths_table(
    *,
    repository: Path,
    decomposition_path: Path,
    table: Sequence[Mapping[str, Any]],
    relative_table_path: str = FINDINGS_OWNERS_PATHS_RELATIVE_PATH,
) -> dict[str, Any]:
    """Commit the round's findings-owners-paths table into the product repo
    and list it in the decomposition's ``referenced_artifacts`` (``driver
    -steps`` step 5: "The table is committed in the product repo and listed
    in ``referenced_artifacts``"). Idempotent: a table identical to what is
    already committed makes no new commit.
    """

    repository = repository.resolve()
    table_path = repository / relative_table_path
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(
        json.dumps([dict(row) for row in table], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    decomposition = _read_json(decomposition_path) or {}
    referenced = list(decomposition.get("referenced_artifacts") or ())
    if relative_table_path not in referenced:
        referenced.append(relative_table_path)
        decomposition["referenced_artifacts"] = referenced
        decomposition_path.write_text(
            json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8",
        )

    decomposition_relative = str(decomposition_path.resolve().relative_to(repository))
    status = subprocess.run(
        [
            "git", "-C", str(repository), "status", "--porcelain",
            "--", relative_table_path, decomposition_relative,
        ],
        capture_output=True, text=True, check=True,
    )
    if not status.stdout.strip():
        return {"committed": False, "path": relative_table_path}

    subprocess.run(
        ["git", "-C", str(repository), "add", relative_table_path, decomposition_relative],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [
            "git", "-C", str(repository), "commit", "-m",
            f"convergence campaign: findings-owners-paths table ({relative_table_path})",
        ],
        check=True, capture_output=True, text=True,
    )
    return {"committed": True, "path": relative_table_path}


def _text_with_synthesized_objectives(text: str, decomposition: Mapping[str, Any]) -> str:
    """``text`` with every synthesized run's objective appended under the
    heading its own ``plan_sections`` entry names (an objective already
    present in its section's body is not duplicated)."""

    sections_map = decomposition.get("plan_sections") or {}

    for heading in sections_map.values():
        heading = str(heading)
        if any(line.strip() == heading.strip() for line in text.splitlines()):
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text else "") + heading + "\n"

    for run in decomposition.get("runs") or ():
        objective = str(run.get("objective") or "")
        if not objective.strip():
            continue
        for slug in run.get("plan_sections") or ():
            heading = sections_map.get(slug)
            if heading is None or objective in extract_plan_section(text, str(heading)):
                continue
            lines = text.splitlines()
            heading_stripped = str(heading).strip()
            index = next(
                (i for i, line in enumerate(lines) if line.strip() == heading_stripped), None,
            )
            if index is None:
                continue
            lines.insert(index + 1, objective)
            text = "\n".join(lines) + "\n"

    return text


def commit_synthesized_plan(
    *, repository: Path, decomposition: Mapping[str, Any], decomposition_path: Path,
) -> dict[str, Any]:
    """Write the synthesized ``decomposition`` to ``decomposition_path``
    (inside ``repository``) and append every synthesized run's objective
    into the committed plan document at ``decomposition["plan"]``, then
    commit both together.

    Both are required for a synthesized round to have any path from plan to
    approve at all: ``commit_findings_owners_paths_table`` (run at approve)
    requires ``decomposition_path`` to already resolve relative to
    ``repository``, and ``check_objective_in_plan_text`` requires every run
    objective already committed at base -- ``plan_synthesis``'s
    auto-generated objectives never already exist in a hand-authored plan
    document. Idempotent: a decomposition/plan pair identical to what is
    already committed makes no new commit.
    """

    repository = repository.resolve()
    decomposition_path = decomposition_path.resolve()
    relative_decomposition = str(decomposition_path.relative_to(repository))
    decomposition_path.parent.mkdir(parents=True, exist_ok=True)
    decomposition_path.write_text(
        json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8",
    )

    relative_paths = [relative_decomposition]
    plan_path = decomposition.get("plan")
    if isinstance(plan_path, str):
        target = repository / plan_path
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_text_with_synthesized_objectives(text, decomposition), encoding="utf-8")
        relative_paths.append(plan_path)

    status = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain", "--", *relative_paths],
        capture_output=True, text=True, check=True,
    )
    if not status.stdout.strip():
        return {"committed": False, "paths": relative_paths}

    subprocess.run(
        ["git", "-C", str(repository), "add", *relative_paths],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [
            "git", "-C", str(repository), "commit", "-m",
            "convergence campaign: synthesized plan and decomposition",
        ],
        check=True, capture_output=True, text=True,
    )
    return {"committed": True, "paths": relative_paths}


def render_approval_packet(
    *,
    repository: Path,
    decomposition_path: Path,
    output_directory: Path,
    findings_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    warning_acknowledgements: Sequence[Mapping[str, Any]] = (),
    criteria_texts_by_run: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
    plan_text: str | None = None,
    enforce: bool | None = None,
    overrides: Sequence[Mapping[str, object]] = (),
) -> ApprovalPacket:
    """Run admission (``prepare_approval``) and render the operator packet.

    ``enforce``/``overrides`` reach ``prepare_approval`` unchanged, which
    forwards them to the decomposition-conformance analyzer (DTR-LK-SYN):
    a caller driving a synthesized round passes ``enforce=True`` here and
    the identical value to :func:`issue_approval`, so the issue-side
    freshness re-derivation cannot diverge from what this step pinned.

    In order: the base worktree must be pristine (untracked files
    included); the byte-identity checks -- a run's quoted criteria text
    must be byte-identical to the ``acceptance_criteria`` entry it names,
    and a run's objective must appear in the plan text of its cited
    sections -- run first and refuse (``ByteIdentityViolation``) before
    anything mutates the repository, since neither check depends on
    admission having run; only then is the findings-owners-paths table
    committed into the product repo and listed in the decomposition's
    ``referenced_artifacts`` (``driver-steps`` step 5), the admission
    refinement loop run (advisory -- narrowing repairs it can make on its
    own are proposals the operator re-commits, never applied in place; run
    after the table commit so its reported PlanGraph digest is computed
    against the same base commit admission itself will bind, not a
    pre-table-commit one), and admission runs. Refuses -- again before
    anything is returned -- when any gate-evidence warning of any severity
    is unacknowledged (``UnacknowledgedWarningsError``); this last check
    alone requires the table already committed (admission needs the
    committed ``referenced_artifacts`` entry to validate), so it is the one
    refusal that cannot avoid leaving a commit. Both byte-identity checks
    always run -- neither is opt-out-able by omitting a kwarg. Never writes
    ``operator-approval.json``: only ``prepare_approval`` (``subject.json``,
    ``gate-evidence.json``) is called here; the human-authored approval
    file is read, never written, by :func:`issue_approval`.
    """

    check_pristine_worktree(repository)
    decomposition = _read_json(decomposition_path) or {}

    violations = check_criteria_byte_identity(decomposition, criteria_texts_by_run)
    if violations:
        raise ByteIdentityViolation(
            "criteria text is not byte-identical to the acceptance_criteria "
            "entry it names: " + ", ".join(violations)
        )

    resolved_plan_text = plan_text
    plan_path = decomposition.get("plan")
    if resolved_plan_text is None and isinstance(plan_path, str):
        # The worktree is pristine (checked above), so its own copy of the
        # plan file is exactly the blob at HEAD -- reading it directly means
        # this check needs no base_commit, and so does not have to wait for
        # prepare_approval (which only runs after the table commit below).
        resolved_plan_text = (repository / plan_path).read_text(encoding="utf-8")
    if resolved_plan_text is not None:
        violations = check_objective_in_plan_text(decomposition, resolved_plan_text)
        if violations:
            raise ByteIdentityViolation(
                "run objective text is absent from the plan text of its cited "
                "sections at base: " + ", ".join(violations)
            )

    table = render_findings_owners_paths_table(findings_by_run)
    table_commit = commit_findings_owners_paths_table(
        repository=repository, decomposition_path=decomposition_path, table=table,
    )

    refinement = run_admission_refinement(repository, decomposition_path)

    prepared = prepare_approval(
        repository=repository,
        decomposition_path=decomposition_path,
        output_directory=output_directory,
        enforce=enforce,
        overrides=overrides,
    )
    gate_evidence = _read_json(prepared.gate_evidence_path) or {}

    outstanding = unacknowledged_warnings(gate_evidence, warning_acknowledgements)
    if outstanding:
        raise UnacknowledgedWarningsError(
            "unacknowledged admission warnings block the approval packet: "
            + ", ".join(sorted(warning_identity(item) for item in outstanding))
        )

    return ApprovalPacket(
        subject_path=prepared.subject_path,
        gate_evidence_path=prepared.gate_evidence_path,
        findings_table=render_findings_owners_paths_table(findings_by_run),
        warnings=tuple(dict(item) for item in gate_evidence.get("warnings") or ()),
        sibling_overlap_warnings=sibling_overlap_warnings_from_gate_evidence(gate_evidence),
        refinement_status=refinement.status,
        refinement_reason=refinement.reason,
        findings_owners_paths_table_path=table_commit["path"],
    )


def issue_approval(
    *,
    repository: Path,
    subject_path: Path,
    gate_evidence_path: Path,
    operator_approval_path: Path,
    receipt_path: Path,
    enforce: bool | None = None,
    overrides: Sequence[Mapping[str, object]] = (),
) -> Path:
    """Issue the receipt once a human has written the approval file.

    Refuses if ``operator_approval_path`` does not already exist: the driver
    "halts for the human-written file" and must never author it itself --
    not the human's file, and not any derived copy of it either.

    Before delegating to ``issue_receipt``, this re-checks the driver's own
    all-severity acknowledgment gate against the operator's own
    ``warning_acknowledgements`` (``AC-CC04-1``: "refuses to proceed to
    issue while any warning is unacknowledged"). The exact,
    byte-for-byte-unmodified ``operator_approval_path`` is then forwarded to
    ``issue_receipt``, whose own ``operator_approval`` receipt reference
    binds to that same file: a signed receipt must attest to what the
    operator actually wrote, including any advisory-severity acknowledgment,
    not a machine-filtered subset of it. ``issue_receipt``'s own gate
    (``_require_acknowledged_high_warnings``) only recognizes high-severity
    acknowledgements and rejects any other acknowledged warning id as
    "unknown"; when the operator's file legitimately acknowledges a
    non-high-severity warning -- satisfying this driver's own stricter,
    all-severity gate above -- ``issue_receipt`` raises its own
    ``PlanApprovalError`` rather than this driver quietly working around
    that narrower contract by authoring a substitute file.

    ``enforce``/``overrides`` reach ``issue_receipt`` unchanged, which
    recomputes the conformance report fresh from them and refuses
    (``PlanApprovalError``) when it disagrees with what
    :func:`render_approval_packet` pinned into ``gate_evidence_path`` -- a
    caller that threads a different ``enforce`` value here than it passed
    there hits that same TOCTOU refusal, not a silently downgraded gate
    (DTR-LK-SYN).
    """

    if not operator_approval_path.exists():
        raise ConvergenceCampaignDriverError(
            "operator approval is required at "
            f"{operator_approval_path}; the driver halts for the human-written "
            "file and never authors it itself"
        )
    operator = _read_json(operator_approval_path) or {}
    gate_evidence = _read_json(gate_evidence_path) or {}
    acknowledgements = operator.get("warning_acknowledgements") or []
    outstanding = unacknowledged_warnings(gate_evidence, acknowledgements)
    if outstanding:
        raise UnacknowledgedWarningsError(
            "unacknowledged admission warnings block issue: "
            + ", ".join(sorted(warning_identity(item) for item in outstanding))
        )

    return issue_receipt(
        repository=repository,
        subject_path=subject_path,
        gate_evidence_path=gate_evidence_path,
        operator_approval_path=operator_approval_path,
        receipt_path=receipt_path,
        enforce=enforce,
        overrides=overrides,
    )


# ---------------------------------------------------------------------------
# Harvest on both block paths, base adoption (AC-CC04-2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarvestedFinding:
    finding: Mapping[str, Any]
    block_path: str  # "transfer_conflict" | "child_blocked"
    source_node_id: str


def _latest_review_ledger_artifact(run_dir: Path) -> Path | None:
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return None
    candidates = sorted(artifacts_dir.glob("*-review-ledger.json"))
    return candidates[-1] if candidates else None


def _normalize_harvested_finding(record: Mapping[str, Any]) -> dict[str, Any]:
    """Convergence-ledger finding shape (``contracts-finding``) from one
    ``ReviewLedger`` finding record (``harness_labs.featurerun.review_fix``)."""

    file = str(record.get("file") or "")
    subject = str(record.get("subject") or record.get("statement") or "")
    required_paths = [str(path) for path in record.get("required_paths") or ()]
    if file and file not in required_paths:
        required_paths.append(file)
    if not required_paths and file:
        required_paths = [file]
    finding: dict[str, Any] = {
        "file": file,
        "subject": subject,
        "required_paths": required_paths,
        "confidence": None,
        "supersedes_key": None,
    }
    statement = record.get("statement")
    if isinstance(statement, str) and statement.strip():
        finding["statement"] = statement
    category = record.get("category")
    if isinstance(category, str) and category.strip():
        finding["category"] = category
    severity = record.get("severity")
    if severity in FINDING_SEVERITIES:
        finding["severity"] = severity
    if "requires_disposition" in record:
        finding["requires_disposition"] = bool(record["requires_disposition"])
    evidence_refs = record.get("evidence_refs")
    if isinstance(evidence_refs, list) and evidence_refs:
        finding["evidence_refs"] = [str(item) for item in evidence_refs]
    source_ids = record.get("source_finding_ids")
    if isinstance(source_ids, list) and source_ids:
        finding["id"] = str(source_ids[0])
        finding["source_finding_ids"] = [str(item) for item in source_ids]
    return finding


def harvest_unrouted_findings(attempt_dir: Path) -> tuple[HarvestedFinding, ...]:
    """Harvest still-open findings from every blocked/failed child node's own
    review-ledger artifact, on both block paths (``AC-CC04-2``).

    Reads ``escalation.json`` (written by ``PlanGraphAudit.record_block_escalation``)
    for the terminal per-node status and, on the transfer-conflict path, the
    retained ``candidate_commit``; and ``checkpoint.json`` for each node's own
    ``run_dir`` (the same field ``plan_graph_autoresume.QuiescenceMonitor``
    reads). Under that ``run_dir`` it reads the latest ``*-review-ledger.json``
    artifact (``ReviewLedger.as_dict()``, ``harness_labs.featurerun.review_fix``)
    and keeps every finding whose ``outcome`` is ``open`` or ``pending_review``
    -- the review loop's own definition of "not yet routed to a resolution."
    A block path with a retained candidate (``transfer_conflict``, from
    ``PlanGraphAudit.transfer_conflict_blocked``) is distinguished from one
    with none (``child_blocked``, the FeatureRun's own report) purely by
    whether the escalation names a ``candidate_commit`` for that node --
    both paths are harvested identically otherwise.
    """

    escalation = _read_json(attempt_dir / "escalation.json")
    if escalation is None:
        return ()
    checkpoint = _read_json(attempt_dir / "checkpoint.json") or {}
    nodes_state = ((checkpoint.get("state") or {}).get("nodes")) or {}

    harvested: list[HarvestedFinding] = []
    for node in escalation.get("nodes") or ():
        if not isinstance(node, Mapping):
            continue
        node_id = node.get("node_id")
        status = node.get("status")
        if status not in ("failed", "blocked") or not isinstance(node_id, str):
            continue
        node_state = nodes_state.get(node_id) if isinstance(nodes_state, Mapping) else None
        run_dir_value = node_state.get("run_dir") if isinstance(node_state, Mapping) else None
        if not isinstance(run_dir_value, str) or not run_dir_value:
            continue
        block_path = "transfer_conflict" if node.get("candidate_commit") else "child_blocked"
        ledger_path = _latest_review_ledger_artifact(Path(run_dir_value))
        if ledger_path is None:
            continue
        ledger_doc = _read_json(ledger_path)
        if ledger_doc is None:
            continue
        for record in (ledger_doc.get("findings") or {}).values():
            if not isinstance(record, Mapping) or record.get("outcome") not in (
                "open", "pending_review",
            ):
                continue
            normalized = _normalize_harvested_finding(record)
            if not normalized["file"] or not normalized["subject"]:
                continue
            harvested.append(HarvestedFinding(normalized, block_path, node_id))
    return tuple(harvested)


def join_regression_node_id(runs: Sequence[Mapping[str, Any]]) -> str:
    """The unique run that transitively depends on every other run and that
    no other run depends on -- the round's join-and-regression node
    (``sizing-s1-s10`` S9: "only the join/regression node exceeds
    [fan-in]... and it carries integration criteria only")."""

    ids = [str(run["id"]) for run in runs]
    depends_on = {str(run["id"]): [str(dep) for dep in run.get("depends_on", ())] for run in runs}
    cache: dict[str, set[str]] = {}

    def ancestors(node_id: str) -> set[str]:
        if node_id in cache:
            return cache[node_id]
        cache[node_id] = set()  # cycle guard
        found: set[str] = set()
        for dep in depends_on.get(node_id, ()):
            found.add(dep)
            found |= ancestors(dep)
        cache[node_id] = found
        return found

    sinks = [
        node_id for node_id in ids
        if not any(node_id in depends_on[other] for other in ids if other != node_id)
    ]
    candidates = [node_id for node_id in sinks if ancestors(node_id) == set(ids) - {node_id}]
    if len(candidates) != 1:
        raise ConvergenceCampaignDriverError(
            "could not identify a unique join-and-regression node "
            f"(candidates={candidates!r})"
        )
    return candidates[0]


def base_adoption_decision(
    *, run_result: Mapping[str, Any], attempt_dir: Path, join_node_id: str | None,
) -> tuple[bool, str | None]:
    """Adopt the round's joined candidate as the next base only when the
    join-and-regression node itself sealed (``AC-CC04-2``, ``driver-steps``).

    A graph that finished with a success status necessarily sealed the join
    node (it is the sink every repair node feeds), so its
    ``candidate_commit`` is adopted directly. Otherwise -- including a block
    reached after every repair node sealed but the join node did not -- the
    join node's own terminal status in ``checkpoint.json`` decides; anything
    else keeps the current base and returns ``(False, None)`` so the caller
    re-bases the next round on the current base and harvests findings
    instead.
    """

    status = str(run_result.get("status"))
    if PlanGraph._status_flags(status)["success"]:
        candidate = run_result.get("candidate_commit")
        return (True, candidate) if isinstance(candidate, str) and candidate else (False, None)
    if not join_node_id:
        return False, None
    checkpoint = _read_json(attempt_dir / "checkpoint.json") or {}
    nodes_state = ((checkpoint.get("state") or {}).get("nodes")) or {}
    join_state = nodes_state.get(join_node_id) if isinstance(nodes_state, Mapping) else None
    join_status = join_state.get("status") if isinstance(join_state, Mapping) else None
    if join_status != "succeeded":
        return False, None
    candidate = join_state.get("candidate_commit") if isinstance(join_state, Mapping) else None
    return (True, candidate) if isinstance(candidate, str) and candidate else (False, None)


def join_node_sealed(attempt_dir: Path, join_node_id: str | None) -> bool:
    """Whether the join-and-regression node's own checkpoint status is
    ``"succeeded"`` -- distinct from whether a usable ``candidate_commit``
    was adopted from it (``AC-CC04-3``).

    :func:`base_adoption_decision` returns ``(False, None)`` both when the
    join node never sealed and when it sealed with no usable candidate to
    adopt; those are not the same fact, and only the former should suppress
    the automatic post-repair measure.
    """

    if not join_node_id:
        return False
    checkpoint = _read_json(attempt_dir / "checkpoint.json") or {}
    nodes_state = ((checkpoint.get("state") or {}).get("nodes")) or {}
    join_state = nodes_state.get(join_node_id) if isinstance(nodes_state, Mapping) else None
    join_status = join_state.get("status") if isinstance(join_state, Mapping) else None
    return join_status == "succeeded"


# ---------------------------------------------------------------------------
# Repair-round bound, stall, regression_suspect (AC-CC04-3, AC-CC04-6)
# ---------------------------------------------------------------------------


@dataclass
class RepairRoundBudget:
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS
    repair_rounds_used: int = 0

    def __post_init__(self) -> None:
        if self.max_repair_rounds < 1:
            raise ConvergenceCampaignDriverError("repair-round bound must be positive")

    def permits_plan_step(self) -> bool:
        return self.repair_rounds_used < self.max_repair_rounds

    def record_plan_step(self) -> None:
        """Consume one round of budget. The post-repair ``measure`` step never
        calls this -- audits are counted outside the bound (``AC-CC04-3``)."""

        if not self.permits_plan_step():
            raise RepairRoundBoundExceeded(
                f"repair-round bound of {self.max_repair_rounds} reached; a "
                "fourth plan step is blocked, though the post-repair measure "
                "step remains permitted"
            )
        self.repair_rounds_used += 1


def guard_before_plan(*, ledger: ConvergenceLedger, budget: RepairRoundBudget) -> None:
    """Refuse the ``plan`` step in any escalation condition it must defer to.

    A ``target_amended`` record with no stated ``invalidation_scope`` blocks
    every later round until a later amendment states one
    (``TargetAmendedWithoutScopeError``, ``ConvergenceLedger.is_blocked()``
    -- one of ``bounds-termination``'s named blocked end states); a stalled
    key escalates instead of launching another round (``StallEscalation``);
    otherwise an exhausted repair-round bound blocks a fourth ``plan`` step
    (``RepairRoundBoundExceeded``). The target-amendment check is checked
    first (the target itself is in question, which pre-empts even a stall
    read), then stall: a campaign that is both stalled and out of rounds is
    a stall, not a bound exhaustion, for the operator's read of why it
    stopped.
    """

    if ledger.is_blocked():
        raise TargetAmendedWithoutScopeError(
            "a target amendment with no stated invalidation_scope blocks "
            "further repair rounds until a later amendment states one"
        )
    stalled = ledger.stalled_keys()
    if stalled:
        raise StallEscalation(
            "stalled keys require operator escalation instead of another "
            "repair round: " + ", ".join(f"{file}:{subject}" for file, subject in sorted(stalled))
        )
    if not budget.permits_plan_step():
        raise RepairRoundBoundExceeded(
            f"repair-round bound of {budget.max_repair_rounds} reached with "
            "keys still open"
        )


def tag_regression_suspects(
    *,
    newly_opened_findings: Sequence[Mapping[str, Any]],
    prior_repair_grants: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Keys among ``newly_opened_findings`` whose ``file`` intersects the
    prior round's repair grants (``AC-CC04-6``).

    "Intersects" is path containment (a grant may be a directory), reusing
    ``plan_graph_contract.path_is_allowed`` -- the same containment rule
    admission and refinement use -- rather than exact string membership,
    which would miss a finding under a granted directory. These are tagged
    ``regression_suspect`` and order the rule step (the driver's ``rule``
    refuses to proceed while one is unanswered) but never stall on their
    own: a brand-new ``finding_opened`` key starts with zero unsuccessful
    repair claims and no fixed/reopened history, so
    ``ConvergenceLedger.stalled_keys`` structurally cannot select it yet.
    """

    grants = list(prior_repair_grants)
    suspects: list[tuple[str, str]] = []
    for finding in newly_opened_findings:
        file = str(finding.get("file") or "")
        if file and grants and path_is_allowed(file, grants):
            suspects.append((file, str(finding.get("subject") or "")))
    return tuple(suspects)


# ---------------------------------------------------------------------------
# Success termination predicate (AC-CC04-5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminationReport:
    success: bool
    amendment_ratio: float
    zero_new_required_findings: bool
    no_unobserved: bool
    full_coverage: bool
    recall_ok: bool
    amendment_ok: bool
    missing_coverage_cells: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "amendment_ratio": self.amendment_ratio,
            "zero_new_required_findings": self.zero_new_required_findings,
            "no_unobserved": self.no_unobserved,
            "full_coverage": self.full_coverage,
            "recall_ok": self.recall_ok,
            "amendment_ok": self.amendment_ok,
            "missing_coverage_cells": list(self.missing_coverage_cells),
        }


def evaluate_success_termination(
    *,
    ledger: ConvergenceLedger,
    required_cells: Sequence[str],
    new_required_findings: int,
    inspector_recall: float,
    recall_threshold: float,
    amendment_ratio_threshold: float,
    amendment_ratio_acknowledged: bool,
    emit: Callable[[str], None] = print,
) -> TerminationReport:
    """Success only with every ``bounds-termination`` condition satisfied.

    Zero new required findings; every key ``observed_fixed`` or ruled (no
    ``unobserved`` -- :meth:`ConvergenceLedger.success` alone only checks
    the *latest* audit left nothing unobserved, which passes while an
    already-open key was never re-audited at all, so this also requires
    :meth:`ConvergenceLedger.open_set` to be empty: a key merely ``open`` or
    ``fix_claimed``, never ``observed_fixed`` and never ruled to a closed
    disposition, still blocks); full required capture coverage (every cell
    in ``required_cells`` recorded ``ok`` -- missing or ``unreachable`` both
    block); inspector recall at the configured threshold; and an amendment
    ratio that is either under the configured threshold or explicitly
    acknowledged. The amendment ratio is printed on *every* call, success or
    not (``AC-CC04-5``: "print the amendment ratio at every termination").
    """

    amendment_ratio = ledger.amendment_ratio()
    emit(json.dumps({"event": "termination", "amendment_ratio": amendment_ratio}, sort_keys=True))
    no_unobserved = ledger.success() and not ledger.open_set()
    coverage_state = ledger.coverage_state()
    missing = tuple(cell for cell in required_cells if coverage_state.get(cell) != "ok")
    full_coverage = not missing
    recall_ok = inspector_recall >= recall_threshold
    amendment_ok = amendment_ratio <= amendment_ratio_threshold or amendment_ratio_acknowledged
    zero_new = new_required_findings == 0
    success = zero_new and no_unobserved and full_coverage and recall_ok and amendment_ok
    return TerminationReport(
        success=success,
        amendment_ratio=amendment_ratio,
        zero_new_required_findings=zero_new,
        no_unobserved=no_unobserved,
        full_coverage=full_coverage,
        recall_ok=recall_ok,
        amendment_ok=amendment_ok,
        missing_coverage_cells=missing,
    )


# ---------------------------------------------------------------------------
# Resume: reconstruct every argument from escalation.json (AC-CC04-4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeDirective:
    logical_graph_id: str
    predecessor_attempt_id: str
    retry_frontier: tuple[str, ...]
    blocker_evidence_ref: str
    argv: tuple[str, ...] = ()

    def as_argv(self) -> tuple[str, ...]:
        return self.argv


def resume_directive_from_escalation(
    run_root: Path,
    seed_attempt_id: str,
    *,
    resume_command: Sequence[str] = DEFAULT_RESUME_COMMAND,
    round_number: int = 1,
) -> ResumeDirective:
    """Reconstruct every ``run_plan_graph.py run --resume`` argument from
    ``escalation.json`` alone -- no second source of truth.

    Delegates entirely to ``scripts.plan_graph_autoresume``: ``find_predecessor``
    selects the resumable lineage leaf and reads its escalation,
    ``reconcile_frontier`` derives the retry frontier the same way the
    autoresume driver does (cross-checked against the attempt's own audit
    events rather than trusting the escalation template alone), and the
    full argv -- including ``--graph-attempt-id`` and ``--run-root``, which
    ``run_plan_graph.py`` requires and a from-scratch reimplementation had
    dropped -- is built by ``AutoresumeDriver.resume_argv`` itself, not a
    second copy of its flag spelling. ``round_number`` (the campaign's
    ``resume --round N``) is the iteration ``resume_argv`` uses to derive
    the successor's own attempt id.
    """

    predecessor = find_predecessor(run_root, seed_attempt_id)
    reconciliation = reconcile_frontier(predecessor.escalation, predecessor.events)
    directive = predecessor.escalation.get("resume_directive_template")
    logical = directive.get("logical_graph_id") if isinstance(directive, Mapping) else None
    if not isinstance(logical, str) or not logical:
        raise ConvergenceCampaignDriverError(
            "escalation.json has no logical_graph_id to resume from"
        )
    autoresume_driver = AutoresumeDriver(
        run_root=Path(run_root), seed_attempt_id=seed_attempt_id,
        resume_command=tuple(resume_command),
    )
    argv = autoresume_driver.resume_argv(predecessor, reconciliation.frontier, round_number)
    return ResumeDirective(
        logical_graph_id=logical,
        predecessor_attempt_id=predecessor.attempt_id,
        retry_frontier=reconciliation.frontier,
        blocker_evidence_ref=predecessor.blocker_evidence_ref,
        argv=argv,
    )


def predecessor_is_resumable(run_root: Path, attempt_id: str) -> bool:
    """Whether ``run_root/attempt_id`` finalized in a resumable status.

    Reuses ``scripts.plan_graph_autoresume``'s own ``_RESUMABLE_ATTEMPT_STATUSES``
    (``{"failed", "blocked"}``) -- the exact vocabulary ``find_predecessor``
    uses to select a resumable lineage leaf -- rather than
    ``PlanGraph._status_flags``'s different, narrower ``"resumable"`` flag
    (``{"blocked", "externally_blocked"}``), which the resume machinery this
    module delegates to does not use.
    """

    manifest = _read_json(run_root / attempt_id / "manifest.json")
    if manifest is None:
        return False
    status = manifest.get("status")
    if not isinstance(status, str) or not status:
        return False
    return status in _RESUMABLE_ATTEMPT_STATUSES


# ---------------------------------------------------------------------------
# Round grant validation (S1/S4-lite, used by the plan step)
# ---------------------------------------------------------------------------


def _ancestors(
    run_id: str, depends_on: Mapping[str, Sequence[str]], cache: dict[str, set[str]],
) -> set[str]:
    if run_id in cache:
        return cache[run_id]
    cache[run_id] = set()  # cycle guard
    found: set[str] = set()
    for dep in depends_on.get(run_id, ()):
        found.add(dep)
        found |= _ancestors(dep, depends_on, cache)
    cache[run_id] = found
    return found


def validate_round_grants(
    decomposition: Mapping[str, Any],
    findings_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    join_node_id: str,
) -> None:
    """Every repair node's ``allowed_paths`` equals the union of its owned
    findings' ``required_paths``, and no two dependency-unordered repair
    nodes' grants overlap (``driver-steps``, ``sizing-s1-s10`` S1/S4)."""

    runs = list(decomposition.get("runs") or ())
    depends_on = {str(run["id"]): list(run.get("depends_on", ())) for run in runs}
    grants = {str(run["id"]): set(run.get("allowed_paths", ())) for run in runs}
    for run in runs:
        run_id = str(run["id"])
        if run_id == join_node_id:
            continue
        owned = findings_by_run.get(run_id, ())
        expected: set[str] = set()
        for finding in owned:
            expected.update(str(path) for path in finding.get("required_paths", ()))
        if grants[run_id] != expected:
            raise ConvergenceCampaignDriverError(
                f"round grant mismatch: run {run_id!r} allowed_paths must equal "
                "the union of its owned findings' required_paths"
            )
    cache: dict[str, set[str]] = {}
    repair_ids = [str(run["id"]) for run in runs if str(run["id"]) != join_node_id]
    for index, first in enumerate(repair_ids):
        for second in repair_ids[index + 1:]:
            if (
                first in _ancestors(second, depends_on, cache)
                or second in _ancestors(first, depends_on, cache)
            ):
                continue
            overlap = grants[first] & grants[second]
            if overlap:
                raise ConvergenceCampaignDriverError(
                    f"round grant overlap: {first!r} and {second!r} share "
                    f"writable paths {sorted(overlap)!r} with no dependency "
                    "ordering between them"
                )


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


class ConvergenceCampaignDriver:
    """Sequences one campaign's rounds, persisting a checkpoint at every step
    of the measure/ingest/rule/plan/approve/run/close machine so a crash at
    any point resumes from the checkpoint (``AC-CC04-7``)."""

    def __init__(
        self,
        *,
        campaign_root: Path,
        campaign_id: str,
        max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
        repository: Path | None = None,
    ) -> None:
        self.campaign_root = Path(campaign_root)
        self.campaign_id = campaign_id
        self.max_repair_rounds = max_repair_rounds
        self.repository = Path(repository) if repository is not None else None
        self.ledger = ConvergenceLedger(self.campaign_root / "ledger.jsonl")
        self.checkpoint = CampaignCheckpointStore(self.campaign_root / "checkpoint.json")
        self.artifacts = CampaignArtifactStore(self.campaign_root / "artifacts")

    # -- checkpoint plumbing -------------------------------------------------

    def _repository_head(self) -> str | None:
        """The repository's current HEAD, when the driver was constructed
        with a ``repository`` -- computed fresh on every call so a commit
        made between two steps is observed."""

        if self.repository is None:
            return None
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return completed.stdout.strip()

    def state(self, *, repository_head: str | None = None) -> dict[str, Any]:
        """Reconstruct campaign state from the checkpoint, or ``{}`` before
        the campaign is opened. Idempotent: replaying it after a crash at any
        step returns the same state a live driver held at that step, because
        every mutation below is followed immediately by a checkpoint save.

        ``repository_head`` defaults to :meth:`_repository_head` -- the
        driver's own configured ``repository``, when it was constructed with
        one -- so every internal ``self.state()`` call across the
        measure/ingest/rule/plan/approve/run/close machine requests staleness
        verification, not only a caller that happens to pass the argument
        explicitly. It is forwarded to :meth:`CampaignCheckpointStore.load`
        so its staleness check (a loaded ``base_commit`` that disagrees with
        the repository head presented) and its sequence-regression guard are
        actually reachable from the step machine -- both are distinct, typed
        refusals and are re-raised, not swallowed with every other
        :class:`ConvergenceCampaignError`, since only "no checkpoint yet"
        (before the campaign is opened) should read as an empty state.
        """

        if repository_head is None:
            repository_head = self._repository_head()
        try:
            return dict(self.checkpoint.load(repository_head=repository_head).state)
        except (CampaignCheckpointStaleError, CampaignCheckpointSequenceError):
            raise
        except ConvergenceCampaignError:
            return {}

    def _save(self, *, lifecycle: str, state: Mapping[str, Any]) -> CampaignCheckpoint:
        if lifecycle not in LIFECYCLES:
            raise ConvergenceCampaignDriverError(f"unknown lifecycle: {lifecycle!r}")
        base_commit = state.get("current_base_commit")
        if not base_commit:
            raise ConvergenceCampaignDriverError(
                "checkpoint state must carry current_base_commit"
            )
        return self.checkpoint.save(
            campaign_id=self.campaign_id,
            lifecycle=lifecycle,
            base_commit=base_commit,
            state=state,
        )

    def _emit_amendment_ratio(self, *, emit: Callable[[str], None] = print) -> float:
        """Print the amendment ratio at a blocked termination, in the same
        shape :func:`evaluate_success_termination` prints on every call
        (``AC-CC04-5``: "the amendment ratio is printed at every
        termination" -- a blocked ending is a termination too)."""

        amendment_ratio = self.ledger.amendment_ratio()
        emit(json.dumps({"event": "termination", "amendment_ratio": amendment_ratio}, sort_keys=True))
        return amendment_ratio

    # -- open -----------------------------------------------------------------

    def open_campaign(
        self,
        *,
        predecessor_run_root: Path | None = None,
        **pin_target_kwargs: Any,
    ) -> dict[str, Any]:
        predecessor_graph_id = pin_target_kwargs.get("predecessor_graph_id")
        if predecessor_graph_id:
            if predecessor_run_root is None:
                raise ConvergenceCampaignDriverError(
                    "predecessor_run_root is required whenever predecessor_graph_id "
                    "is set, to check whether the predecessor graph is still "
                    "resumable; omitting it must not silently skip the check "
                    "(AC-CC04-4)"
                )
            if predecessor_is_resumable(Path(predecessor_run_root), predecessor_graph_id):
                # No campaign_opened record exists yet at this refusal, so
                # there is nothing in self.ledger to fold -- the ratio is
                # printed directly rather than via self.ledger, which would
                # create the campaign's journal file as a side effect of a
                # refusal to open it.
                print(json.dumps({"event": "termination", "amendment_ratio": 0.0}, sort_keys=True))
                raise PredecessorResumableError(
                    f"predecessor graph {predecessor_graph_id!r} is still resumable; "
                    "resume it instead of opening a new campaign"
                )
        record = pin_target(self.ledger, campaign_root=self.campaign_root, **pin_target_kwargs)
        state = {
            "round": 0,
            "repair_rounds_used": 0,
            "current_base_commit": pin_target_kwargs["base_commit"],
            "prior_repair_grants": [],
            "join_regression_node_id": None,
        }
        self._save(lifecycle="opened", state=state)
        return record

    # -- measure ---------------------------------------------------------------

    def measure(
        self,
        *,
        capture_argv: Sequence[str],
        out_dir: Path,
        timeout: float | None = None,
        require_preflight_success: bool = True,
        evidence_sources: Mapping[str, Path] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        _state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``measure(campaign_state) -> audit_result digest`` (``driver-steps``).

        Capture runs as a controller preflight: its exit code is required to
        be zero when it ran at all (``require_preflight_success``). Per the
        shipped capture contract (``scripts/ui_fidelity_capture.py``), the
        capture process writes its result to ``<out_dir>/receipt.json`` and
        prints nothing to stdout on success -- ``out_dir`` is the same
        directory the caller names in ``capture_argv`` (e.g. via ``--out``),
        supplied here explicitly so this domain-neutral driver never has to
        parse a measurer's own argv to find it. The raw receipt content is
        passed through the configured ``pre_journal_sanitizer`` hook before
        anything is journaled, digested, or sealed (``measurer-requirements``).
        The sanitized output is sealed as one immutable content-addressed
        artifact via :class:`CampaignArtifactStore`, unconditionally -- this
        step is never gated by the repair-round bound (``AC-CC04-3``). When
        the capture also names the evidence files its findings'
        ``evidence_refs`` point at (``evidence_sources``, a caller-supplied
        ref-to-path resolution), every one of them is sealed too via
        :meth:`CampaignArtifactStore.seal_audit_result`.

        ``_state``, used only by :meth:`close`'s own automatic post-repair
        chaining, supplies the state :meth:`close` just saved directly
        rather than re-loading it through :meth:`state`. A base adoption
        moves ``current_base_commit`` to a candidate commit the campaign's
        configured repository has not been checked out to (candidates are
        commit-tree objects, never a worktree checkout -- see
        ``PlanGraph._merge_parents``), so a fresh :meth:`state` call
        immediately afterward would compare that new ``base_commit`` against
        the *old* live repository head and misreport staleness, even though
        nothing external changed between the two calls in the same
        synchronous step. An externally invoked ``measure`` (the CLI, or any
        other caller) always omits ``_state`` and gets the full live-head
        staleness re-verification, unchanged.
        """

        state = dict(_state) if _state is not None else self.state()
        self._save(lifecycle="measuring", state=state)
        out_dir = Path(out_dir)
        completed = runner(
            list(capture_argv), capture_output=True, text=True, timeout=timeout, check=False,
        )
        if require_preflight_success and completed.returncode != 0:
            raise ConvergenceCampaignDriverError(
                f"capture preflight exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        receipt_path = out_dir / "receipt.json"
        try:
            raw_receipt = receipt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConvergenceCampaignDriverError(
                f"capture preflight exited {completed.returncode} but wrote no "
                f"receipt at {receipt_path}: {exc}"
            ) from exc
        config = campaign_config(self.ledger)
        try:
            sanitized_receipt = sanitize_before_journaling(config, raw_receipt)
        except SanitizerFailure:
            self._emit_amendment_ratio()
            raise
        audit_result = json.loads(sanitized_receipt)
        raw = (json.dumps(audit_result, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor, temp_name = tempfile.mkstemp(dir=self.campaign_root, prefix=".audit-result-")
        temp_path = Path(temp_name)
        try:
            with open(descriptor, "wb") as handle:
                handle.write(raw)
            record = self.artifacts.seal(temp_path, media_type="application/json")
        finally:
            temp_path.unlink(missing_ok=True)
        if evidence_sources:
            self.artifacts.seal_audit_result(audit_result, evidence_sources=evidence_sources)
        state["pending_audit_digest"] = record.digest
        self._save(lifecycle="measured", state=state)
        return {"digest": record.digest, "audit_result": audit_result}

    # -- ingest ------------------------------------------------------------

    def ingest(
        self,
        *,
        digest: str | None = None,
        audit_result: Mapping[str, Any] | None = None,
        history_roots: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        """``ingest(digest)`` -- folds exactly one sealed artifact; idempotent
        by digest (delegated entirely to :meth:`ConvergenceLedger.ingest_audit`).

        Any findings :meth:`close` harvested from the prior round's blocked
        child review-ledgers (``state["harvested_findings"]``) are folded in
        alongside this ingest's own genuine verdicts -- the real fold the
        module docstring promises, rather than a value only ever written and
        never read. They are consumed exactly once: this ingest clears
        ``harvested_findings`` from state so a later ingest does not re-fold
        the same items.

        ``history_roots`` (EM-D2) names prior campaign roots to consult via
        ``harness_labs.plangraph.finding_history`` for every key this ingest
        newly opens. A key a named prior campaign ruled ``waive`` (folds to
        terminal status ``excluded``) gets one recurrence-annotation
        artifact sealed through the existing :class:`CampaignArtifactStore`
        -- carrying the prior disposition, its statement, and the prior
        campaign's label -- with the sealed digest recorded in the
        checkpoint ``state`` under ``recurrence_annotations``, keyed by
        finding key. This never touches this campaign's own ledger journal:
        the annotation is a sealed artifact, not a ledger record, so the
        folded ledger's record-kind vocabulary is unchanged.

        A retried ingest of an already-folded digest (``ingest_audit``
        reports ``idempotent``, e.g. re-running after a first attempt died
        inside :meth:`_seal_recurrence_annotations` with a mistyped root)
        recovers the keys that digest opened from the ledger's own durable
        ``finding_opened`` records rather than from ``summary["opened"]``
        (empty on the short-circuited retry), so the annotation still gets
        sealed instead of silently returning ``{}``.
        """

        state = self.state()
        self._save(lifecycle="ingesting", state=state)
        digest = digest or state.get("pending_audit_digest")
        if audit_result is None:
            if not digest:
                raise ConvergenceCampaignDriverError(
                    "ingest requires a sealed digest or an explicit audit_result"
                )
            audit_result = json.loads(self.artifacts.open_bytes(digest))
        harvested_findings = state.get("harvested_findings") or []
        if harvested_findings:
            audit_result = dict(audit_result)
            audit_result["findings"] = [
                *audit_result.get("findings", []),
                *(dict(finding) for finding in harvested_findings),
            ]
        summary = self.ledger.ingest_audit(audit_result)
        opened_keys = {tuple(key) for key in summary["opened"]}
        opened_findings = [
            finding
            for finding in audit_result.get("findings", [])
            if (finding.get("file"), finding.get("subject")) in opened_keys
        ]
        suspects = tag_regression_suspects(
            newly_opened_findings=opened_findings,
            prior_repair_grants=state.get("prior_repair_grants", ()),
        )
        new_required_findings = sum(
            1 for finding in opened_findings if finding.get("requires_disposition")
        )
        annotation_keys = opened_keys
        if summary["idempotent"]:
            annotation_keys = {
                tuple(record["key"])
                for record in self.ledger.records()
                if record.get("type") == RECORD_KIND_FINDING_OPENED
                and record.get("digest") == summary["digest"]
            }
        recurrence_annotations = self._seal_recurrence_annotations(
            history_roots, annotation_keys
        )
        if recurrence_annotations:
            merged = dict(state.get("recurrence_annotations") or {})
            merged.update(recurrence_annotations)
            state["recurrence_annotations"] = merged
        state["pending_audit_digest"] = None
        state["last_ingest_digest"] = digest
        state["harvested_findings"] = []
        state["regression_suspects"] = [list(item) for item in suspects]
        state["last_ingest_new_required_findings"] = new_required_findings
        self._save(lifecycle="ingested", state=state)
        return {
            "summary": summary,
            "regression_suspect_keys": suspects,
            "recurrence_annotations": recurrence_annotations,
        }

    def _seal_recurrence_annotations(
        self,
        history_roots: Sequence[str | Path],
        opened_keys: set[tuple[str, str]],
    ) -> dict[str, str]:
        """Seal one recurrence-annotation artifact per arriving key a named
        prior campaign ruled ``waive``, keyed by ``"file:subject"``.

        Volume is bounded by arriving findings (``opened_keys``, this
        ingest's own newly-opened keys), and lookup is exact-key only --
        ``FindingHistory.for_key``, never similarity retrieval. Folding a
        named root's journal never writes to it (``finding_history``'s own
        contract); sealing lands only in this campaign's own artifact store,
        never in its ledger journal.
        """

        if not history_roots or not opened_keys:
            return {}
        if self.repository is None:
            raise ConvergenceCampaignDriverError(
                "ingest --history-roots requires the driver to be configured "
                "with a repository, to resolve the repository_id finding "
                "history is scoped to"
            )
        identity_path = self.repository / REPOSITORY_IDENTITY_PATH
        try:
            identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConvergenceCampaignDriverError(
                f"could not read repository identity at {identity_path}: {exc}"
            ) from exc
        repository_id = load_repository_id(identity_payload)

        entries: list[tuple[Path, str, str]] = []
        for root in history_roots:
            root = Path(root)
            journal_path = root / "ledger.jsonl"
            checkpoint_path = root / "checkpoint.json"
            try:
                checkpoint_raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ConvergenceCampaignDriverError(
                    f"could not read history root checkpoint at {checkpoint_path}: {exc}"
                ) from exc
            entries.append((journal_path, str(checkpoint_raw["campaign_id"]), repository_id))

        history = fold_campaigns(entries)
        digests: dict[str, str] = {}
        for file, subject in sorted(opened_keys):
            lineage = history.for_key(file, subject)
            waived = next(
                (
                    (entry, ruling)
                    for entry in lineage
                    if entry.status == "excluded"
                    for ruling in reversed(entry.rulings)
                    if ruling.disposition == "waive"
                ),
                None,
            )
            if waived is None:
                continue
            entry, ruling = waived
            payload = {
                "protocol": RECURRENCE_ANNOTATION_PROTOCOL,
                "file": file,
                "subject": subject,
                "prior_campaign_label": entry.campaign_label,
                "prior_disposition": ruling.disposition,
                "prior_statement": ruling.statement,
            }
            raw = (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            descriptor, temp_name = tempfile.mkstemp(
                dir=self.campaign_root, prefix=".recurrence-annotation-"
            )
            temp_path = Path(temp_name)
            try:
                with open(descriptor, "wb") as handle:
                    handle.write(raw)
                record = self.artifacts.seal(temp_path, media_type="application/json")
            finally:
                temp_path.unlink(missing_ok=True)
            digests[f"{file}:{subject}"] = record.digest
        return digests

    # -- rule ----------------------------------------------------------------

    def rule(self, *, dispositions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Human dispositions; blocks until every regression-suspect key from
        this round's ingest has been answered."""

        state = self.state()
        self._save(lifecycle="ruling", state=state)
        required = {tuple(key) for key in state.get("regression_suspects", ())}
        supplied: set[tuple[str, str]] = set()
        for item in dispositions:
            key = (str(item["key"][0]), str(item["key"][1]))
            self.ledger.record_ruling(
                key,
                disposition=item["disposition"],
                statement=item["statement"],
                actor=item.get("actor", "operator"),
            )
            supplied.add(key)
        missing = required - supplied
        if missing:
            state["blocked_reason"] = (
                "rule step blocks until every regression-suspect key is answered; "
                "missing: " + ", ".join(f"{file}:{subject}" for file, subject in sorted(missing))
            )
            self._emit_amendment_ratio()
            self._save(lifecycle="blocked", state=state)
            raise ConvergenceCampaignDriverError(state["blocked_reason"])
        self._save(lifecycle="ruled", state=state)
        return {"ruled": sorted(supplied)}

    # -- plan ------------------------------------------------------------------

    def plan(
        self,
        *,
        decomposition: Mapping[str, Any] | None = None,
        findings_by_run: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        synthesis: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register the round's own decomposition, bounded by the repair-round
        budget (``AC-CC04-3``) and refused on a stall (``AC-CC04-6``).

        When ``decomposition`` is omitted, this step invokes
        ``plan_synthesis`` itself (DTR-LK-SYN) against the ledger's own open
        findings -- ``synthesis`` carries that function's keyword arguments
        (``plan_path``/``plan_section_id``/``plan_section_heading`` and any
        of its other optional knobs); ``repository``/``base_commit`` are
        filled in from this driver's own configured repository and its
        current HEAD (the same ``base_commit`` ``plan_approval.prepare_approval``
        re-derives at approve time -- ``_git(repository, "rev-parse", "HEAD")``,
        not the checkpointed ``current_base_commit``) unless ``synthesis``
        already names them, so synthesized ``path_intents`` resolve against
        the real base commit rather than a static guess. ``findings_by_run`` is then
        taken directly from the synthesis result, so
        :func:`validate_round_grants` below checks synthesis's own ownership
        computation, never a second, possibly-diverging copy of it. A caller
        that already holds a hand-authored decomposition keeps passing
        ``decomposition`` and ``findings_by_run`` explicitly, unchanged from
        before.

        The synthesized ``findings_by_run`` is written to
        ``campaign_root/plans/round-<n>-findings-by-run.json`` and its path
        is both returned and checkpointed. The synthesized ``decomposition``
        itself is handled differently depending on whether this driver is
        configured with a ``repository`` (the same one ``synthesis``'s
        ``repository``/``base_commit`` are filled in from above): with one,
        it is written inside the repository (under ``.harness/campaign-plans``)
        and committed together with every synthesized run's auto-generated
        objective, appended into the committed plan document at
        ``decomposition["plan"]`` under the heading its own
        ``plan_sections`` entry names (:func:`commit_synthesized_plan`) --
        both are required for the round to reach ``approve prepare`` at all:
        ``commit_findings_owners_paths_table`` needs ``decomposition_path``
        to resolve relative to ``repository``, and
        ``check_objective_in_plan_text`` needs every objective already
        committed at base, which ``plan_synthesis``'s auto-generated
        objectives never already are in a hand-authored plan document.
        Without a configured ``repository`` neither can be done here, and
        the decomposition is written to ``campaign_root/plans`` instead, for
        a caller to commit (with its plan text) itself. Either way the
        returned/checkpointed ``decomposition_path`` is what a caller driving
        the round through the CLI hands to ``approve prepare`` as
        ``--decomposition``.
        """

        state = self.state()
        budget = RepairRoundBudget(self.max_repair_rounds, int(state.get("repair_rounds_used", 0)))
        try:
            guard_before_plan(ledger=self.ledger, budget=budget)
        except (StallEscalation, RepairRoundBoundExceeded, TargetAmendedWithoutScopeError) as exc:
            state["blocked_reason"] = str(exc)
            self._emit_amendment_ratio()
            self._save(lifecycle="blocked", state=state)
            raise
        self._save(lifecycle="planning", state=state)
        synthesized = decomposition is None
        if synthesized:
            if synthesis is None:
                raise ConvergenceCampaignDriverError(
                    "plan requires either an explicit decomposition (with "
                    "findings_by_run) or synthesis keyword arguments for "
                    "plan_synthesis"
                )
            synthesis_kwargs = dict(synthesis)
            if self.repository is not None:
                synthesis_kwargs.setdefault("repository", self.repository)
                synthesis_kwargs.setdefault("base_commit", self._repository_head())
            result = plan_synthesis(self.ledger, **synthesis_kwargs)
            decomposition = result.decomposition
            findings_by_run = result.findings_by_run
        elif findings_by_run is None:
            raise ConvergenceCampaignDriverError(
                "plan requires findings_by_run alongside an explicit decomposition"
            )
        join_node_id = join_regression_node_id(decomposition["runs"])
        validate_round_grants(decomposition, findings_by_run, join_node_id)
        budget.record_plan_step()
        repair_grants: set[str] = set()
        for run in decomposition["runs"]:
            if str(run["id"]) != join_node_id:
                repair_grants.update(run.get("allowed_paths", ()))
        # Accumulated, not overwritten: a finding under a path a *previous*
        # round repaired is just as much a regression suspect as one under
        # this round's own grants (``AC-CC04-6``).
        all_repair_grants = set(state.get("prior_repair_grants", ())) | repair_grants
        round_number = int(state.get("round", 0)) + 1
        result_payload: dict[str, Any] = {
            "join_regression_node_id": join_node_id, "round": round_number,
        }
        if synthesized:
            plans_directory = self.campaign_root / "plans"
            plans_directory.mkdir(parents=True, exist_ok=True)
            findings_by_run_path = plans_directory / f"round-{round_number}-findings-by-run.json"
            findings_by_run_path.write_text(
                json.dumps(findings_by_run, sort_keys=True) + "\n", encoding="utf-8",
            )
            if self.repository is not None:
                # commit_findings_owners_paths_table (run at approve) needs
                # decomposition_path to resolve relative to repository, so a
                # synthesized decomposition is written inside it, not to
                # campaign_root/plans -- and committed there together with
                # every synthesized objective, giving the round an actual
                # path from plan to approve (check_objective_in_plan_text).
                decomposition_path = (
                    self.repository / ".harness" / "campaign-plans"
                    / f"round-{round_number}-decomposition.json"
                )
                result_payload["plan_commit"] = commit_synthesized_plan(
                    repository=self.repository,
                    decomposition=decomposition,
                    decomposition_path=decomposition_path,
                )
            else:
                decomposition_path = plans_directory / f"round-{round_number}-decomposition.json"
                decomposition_path.write_text(
                    json.dumps(decomposition, sort_keys=True) + "\n", encoding="utf-8",
                )
            state["decomposition_path"] = str(decomposition_path)
            state["findings_by_run_path"] = str(findings_by_run_path)
            result_payload["decomposition_path"] = str(decomposition_path)
            result_payload["findings_by_run_path"] = str(findings_by_run_path)
        state.update(
            {
                "round": round_number,
                "repair_rounds_used": budget.repair_rounds_used,
                "join_regression_node_id": join_node_id,
                "prior_repair_grants": sorted(all_repair_grants),
            }
        )
        self._save(lifecycle="planned", state=state)
        return result_payload

    # -- approve ---------------------------------------------------------------

    def approve_prepare(self, **kwargs: Any) -> ApprovalPacket:
        state = self.state()
        self._save(lifecycle="approving", state=state)
        return render_approval_packet(**kwargs)

    def approve_issue(self, **kwargs: Any) -> Path:
        receipt = issue_approval(**kwargs)
        state = self.state()
        self._save(lifecycle="approved", state=state)
        return receipt

    # -- run -------------------------------------------------------------------

    def run_graph(
        self,
        *,
        argv: Sequence[str],
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> dict[str, Any]:
        """Existing PlanGraph execution, delegated wholesale to
        ``scripts/run_plan_graph.py`` as a subprocess.

        Refuses before dispatching the subprocess unless the caller's own
        ``argv`` carries both of ``driver-steps`` step 6's required
        bindings: ``--on-block-argv`` (``run_plan_graph.py``'s own block
        hook, so a blocked graph is never left with no automatic
        notification) and, whenever ``argv`` names a registration via
        ``--registration``, an automatic-recovery authority already baked
        into that registration (``register_plan_graph``'s own
        ``automatic_recovery`` field, read back from the file ``argv``
        names). An operator driving a campaign round with no block hook --
        or with a registration that never bound a recovery authority -- is
        refused, not silently permitted. (When ``argv`` instead names
        ``--approval-receipt``, the registration it resolves to is not
        re-derived here; only the directly-named-registration case is
        checked.)
        """

        argv = list(argv)
        if "--on-block-argv" not in argv:
            raise ConvergenceCampaignDriverError(
                "run requires --on-block-argv in argv (driver-steps step 6: "
                "'run' sets a block hook so a blocked graph is never left "
                "with no automatic notification)"
            )
        if "--registration" in argv:
            registration_index = argv.index("--registration") + 1
            if registration_index >= len(argv):
                raise ConvergenceCampaignDriverError("--registration requires a path")
            registration = _read_json(Path(argv[registration_index])) or {}
            if not registration.get("automatic_recovery"):
                raise ConvergenceCampaignDriverError(
                    "run requires the named --registration to carry an "
                    "automatic_recovery authority (driver-steps step 6: "
                    "'an automatic-recovery authority baked in')"
                )

        state = self.state()
        self._save(lifecycle="running", state=state)
        completed = runner(list(argv), capture_output=True, text=True, check=False)
        try:
            result = json.loads(completed.stdout)
        except ValueError as exc:
            raise ConvergenceCampaignDriverError(
                f"run_plan_graph.py produced no parseable result: {exc}"
            ) from exc
        succeeded = bool((result.get("status_flags") or {}).get("success"))
        state["last_run_result"] = result
        self._save(lifecycle="run_succeeded" if succeeded else "run_blocked", state=state)
        return result

    # -- close -----------------------------------------------------------------

    def close(
        self,
        *,
        run_result: Mapping[str, Any],
        attempt_dir: Path | None = None,
        evidence_sources: Mapping[str, Path] | None = None,
        capture_argv: Sequence[str] | None = None,
        out_dir: Path | None = None,
        measure_kwargs: Mapping[str, Any] | None = None,
        termination_kwargs: Mapping[str, Any] | None = None,
        resume_kwargs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Base adoption and both-block-path harvest (``AC-CC04-2``).

        The next audit launches automatically the moment the join and
        regression node seals (``AC-CC04-3``, ``bounds-termination``:
        "audits follow every round automatically and never consume the
        round bound") -- ``join_sealed`` is derived from ``run_result``/the
        join node's own checkpoint status via :func:`join_node_sealed`, not
        a constant and not an alias for whether a candidate was adopted
        (:func:`base_adoption_decision` also returns no candidate when the
        join node sealed with nothing usable to adopt), and when
        ``capture_argv`` is supplied this actually launches :meth:`measure`
        rather than only reporting the flag.

        When ``termination_kwargs`` is supplied, :meth:`evaluate_termination`
        is likewise actually launched from this step of the machine (and
        from the ``close`` CLI subcommand's own ``--termination-file``)
        rather than remaining a function only a test calls directly
        (``AC-CC04-5``): the round that just closed is exactly the point at
        which ``bounds-termination``'s success predicate is meaningful to
        check. Omitting it leaves ``close`` behaviorally unchanged.

        Harvested findings are *not* folded through
        :meth:`ConvergenceLedger.ingest_audit`: that call represents a full
        audit sweep with a verdict for every prior key, and a harvest is
        neither -- folding it marks every other open key ``unobserved`` and,
        worse, re-emitting the same still-open finding across two blocked
        rounds reads as two failed repair claims with no audit evidence,
        fabricating a stall (``state-ledger``: "round outcomes projected
        from review-ledger artifacts by one adapter function", not a second
        audit). Harvested findings are instead carried in checkpoint state
        for the next round's real ``measure``/``ingest`` cycle to fold in
        alongside genuine verdicts.

        When the round stayed on the blocked path *without* adopting a new
        base (the next round re-bases on the current base, per
        ``driver-steps`` step 7), supplying ``resume_kwargs`` actually wires
        :meth:`resume_directive` into this step of the round loop -- rather
        than leaving it a standalone CLI subcommand nobody in the loop
        calls -- so the round's already-sealed, non-join node candidates are
        carried forward via the existing reuse path (``PlanGraph.resume``'s
        own ``reused_completed`` reconstruction from the predecessor
        checkpoint) instead of being re-run from scratch. ``run_root`` and
        ``seed_attempt_id`` default from ``attempt_dir`` (its parent and
        name) and may be overridden via ``resume_kwargs``. Omitting
        ``resume_kwargs`` leaves ``close`` behaviorally unchanged.
        """

        state = self.state()
        self._save(lifecycle="closing", state=state)
        status = str(run_result.get("status"))
        join_node_id = state.get("join_regression_node_id")
        harvested: tuple[HarvestedFinding, ...] = ()
        graph_succeeded = PlanGraph._status_flags(status)["success"]
        if graph_succeeded:
            # A graph that finished successfully necessarily sealed the join
            # node -- it is the sink every repair node feeds.
            join_sealed = True
            candidate = run_result.get("candidate_commit")
            adopted = isinstance(candidate, str) and bool(candidate)
            new_base = candidate if adopted else None
        else:
            if attempt_dir is None:
                raise ConvergenceCampaignDriverError(
                    "a blocked round's close step requires attempt_dir to harvest findings"
                )
            harvested = harvest_unrouted_findings(attempt_dir)
            adopted, new_base = base_adoption_decision(
                run_result=run_result, attempt_dir=attempt_dir, join_node_id=join_node_id,
            )
            join_sealed = join_node_sealed(attempt_dir, join_node_id)

        harvested_payload: list[dict[str, Any]] = []
        if harvested:
            config = campaign_config(self.ledger)
            for item in harvested:
                sanitized_json = sanitize_before_journaling(
                    config, json.dumps(dict(item.finding), sort_keys=True),
                )
                harvested_payload.append(json.loads(sanitized_json))
            if evidence_sources:
                self.artifacts.seal_audit_result(
                    {"findings": harvested_payload}, evidence_sources=evidence_sources,
                )

        if adopted and new_base:
            state["current_base_commit"] = new_base
        state["harvested_findings"] = harvested_payload

        resume_directive_result: ResumeDirective | None = None
        if resume_kwargs is not None and not graph_succeeded and not adopted:
            resume_arguments: dict[str, Any] = {
                "run_root": attempt_dir.parent, "seed_attempt_id": attempt_dir.name,
            }
            resume_arguments.update(dict(resume_kwargs))
            resume_directive_result = self.resume_directive(**resume_arguments)
            state["next_round_resume_argv"] = list(resume_directive_result.as_argv())

        self._save(lifecycle="closed", state=state)

        measure_result: dict[str, Any] | None = None
        if join_sealed and capture_argv is not None:
            if out_dir is None:
                raise ConvergenceCampaignDriverError(
                    "out_dir is required whenever capture_argv is supplied, so the "
                    "post-repair measure step knows where to read receipt.json from"
                )
            measure_result = self.measure(
                capture_argv=capture_argv, out_dir=out_dir, _state=state,
                **dict(measure_kwargs or {}),
            )
            # measure() mutated its own copy of state (the _state bypass
            # passes a value, not a reference the callee saves back into);
            # mirror its one mutation here so a chained evaluate_termination
            # below -- and its own checkpoint save on success -- does not
            # clobber the just-recorded pending_audit_digest with a stale,
            # pre-measure snapshot.
            state["pending_audit_digest"] = measure_result["digest"]

        termination_report: TerminationReport | None = None
        if termination_kwargs is not None:
            termination_report = self.evaluate_termination(_state=state, **dict(termination_kwargs))

        return {
            "base_adopted": adopted,
            "new_base_commit": new_base,
            "harvested_findings": harvested_payload,
            "join_sealed": join_sealed,
            "auto_launch_measure": join_sealed,
            "measure_result": measure_result,
            "termination": termination_report.as_mapping() if termination_report else None,
            "resume_directive": (
                {"argv": list(resume_directive_result.as_argv())}
                if resume_directive_result is not None else None
            ),
        }

    # -- termination (AC-CC04-5) ------------------------------------------

    def evaluate_termination(
        self,
        *,
        required_cells: Sequence[str] = (),
        new_required_findings: int | None = None,
        inspector_recall: float | None = None,
        amendment_ratio_acknowledged: bool = False,
        emit: Callable[[str], None] = print,
        _state: Mapping[str, Any] | None = None,
    ) -> TerminationReport:
        """Evaluate ``bounds-termination``'s success predicate against the
        campaign's own configured thresholds (``AC-CC04-5``), and checkpoint
        ``succeeded`` when it is met -- the terminal lifecycle CC-02 declares
        but nothing wrote.

        ``new_required_findings`` defaults to the count of ``requires_disposition``
        findings the most recent ``ingest`` opened (``state['last_ingest_new_required_findings']``,
        written by :meth:`ingest`) rather than a blind ``0`` -- a caller may
        still override it, but the gate no longer defaults to declaring away
        a required finding the campaign's own ledger already recorded.

        ``inspector_recall`` defaults to the score sealed in the campaign's
        own ``recall_report_digest`` artifact (``dtr-mc``:
        ``scripts/commission_measurer.py recall``'s output, read back via
        ``self.artifacts``) rather than a hardcoded ``0.0`` -- the calibrated
        number this way actually reaches the recall-threshold gate. A caller
        passing ``inspector_recall`` explicitly still wins over the sealed
        value; a config with no ``recall_report_digest`` at all (a
        ``commissioning_override`` campaign) falls back to ``0.0``, same as
        before this field existed.

        ``_state`` is the same close()-supplied in-memory state bypass
        :meth:`measure` accepts, and for the same reason: :meth:`close` may
        chain into this immediately after adopting a new base, before that
        candidate has been checked out anywhere a fresh :meth:`state` call's
        live-head staleness check would find it.
        """

        state = dict(_state) if _state is not None else self.state()
        if new_required_findings is None:
            new_required_findings = int(state.get("last_ingest_new_required_findings", 0))
        config = campaign_config(self.ledger)
        if inspector_recall is None:
            inspector_recall = self._sealed_inspector_recall(config)
        report = evaluate_success_termination(
            ledger=self.ledger,
            required_cells=required_cells,
            new_required_findings=new_required_findings,
            inspector_recall=inspector_recall,
            recall_threshold=float(config.get(CONFIG_RECALL_THRESHOLD_KEY, 1.0)),
            amendment_ratio_threshold=float(config.get(CONFIG_AMENDMENT_RATIO_THRESHOLD_KEY, 0.0)),
            amendment_ratio_acknowledged=amendment_ratio_acknowledged,
            emit=emit,
        )
        if report.success:
            state["termination"] = report.as_mapping()
            self._save(lifecycle="succeeded", state=state)
        return report

    def _sealed_inspector_recall(self, config: Mapping[str, Any]) -> float:
        """Read the calibrated recall score back out of the sealed recall
        report named by the config's ``recall_report_digest`` (``dtr-mc``),
        or ``0.0`` when the campaign carries none (a
        ``commissioning_override`` campaign)."""

        digest = config.get(CONFIG_RECALL_REPORT_DIGEST_KEY)
        if not digest:
            return 0.0
        report = json.loads(self.artifacts.open_bytes(digest))
        return float(report.get("recall", 0.0))

    # -- resume ------------------------------------------------------------

    def resume_directive(
        self,
        *,
        run_root: Path,
        seed_attempt_id: str,
        resume_command: Sequence[str] = DEFAULT_RESUME_COMMAND,
        round_number: int = 1,
    ) -> ResumeDirective:
        return resume_directive_from_escalation(
            run_root, seed_attempt_id, resume_command=resume_command, round_number=round_number,
        )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """A CLI subcommand per step of the measure/ingest/rule/plan/approve/run
    /close machine, plus ``resume`` and ``state`` -- ``build-order-cc-04``'s
    "through the shipped CLIs" lifecycle otherwise has no surface to drive
    a campaign with (``AC-CC04-7``, ``AC-CC04-9``... i.e. the step-machine
    finding: a sequencer library nobody outside a test can call)."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[2] if __doc__ else "")
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--max-repair-rounds", type=int, default=DEFAULT_MAX_REPAIR_ROUNDS)
    parser.add_argument(
        "--repository", dest="campaign_repository", type=Path, default=None,
        help="when given, every checkpoint load in this invocation -- across "
        "every step, not only 'state' -- requests staleness verification "
        "against this repository's current HEAD "
        "(CampaignCheckpointStaleError/CampaignCheckpointSequenceError)",
    )
    subparsers = parser.add_subparsers(dest="step", required=True)

    measure = subparsers.add_parser("measure")
    measure.add_argument("--capture-argv", nargs="+", required=True)
    measure.add_argument("--out-dir", type=Path, required=True)
    measure.add_argument("--timeout", type=float, default=None)
    measure.add_argument(
        "--no-require-preflight-success", dest="require_preflight_success",
        action="store_false", default=True,
    )

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--digest", default=None)
    ingest.add_argument("--audit-result-file", type=Path, default=None)
    ingest.add_argument(
        "--history-roots", nargs="+", type=Path, default=None,
        help="one or more prior campaign roots to consult via finding_history "
        "at ingest; a key a named root's journal ruled 'waive' gets a "
        "recurrence-annotation artifact sealed through this campaign's own "
        "artifact store, digest recorded in checkpoint state keyed by "
        "finding key (requires the top-level --repository)",
    )

    rule = subparsers.add_parser("rule")
    rule.add_argument("--dispositions-file", type=Path, required=True)

    plan_cmd = subparsers.add_parser("plan")
    plan_cmd.add_argument("--decomposition", type=Path, default=None)
    plan_cmd.add_argument("--findings-by-run-file", type=Path, default=None)
    plan_cmd.add_argument(
        "--synthesis-config", type=Path, default=None,
        help="a JSON object of plan_synthesis's own keyword arguments "
        "(plan_path/plan_section_id/plan_section_heading and any of its "
        "other optional knobs); when given, the plan step invokes "
        "plan_synthesis itself against the ledger's own open findings "
        "instead of reading --decomposition/--findings-by-run-file "
        "(DTR-LK-SYN, mutually exclusive with them)",
    )

    approve = subparsers.add_parser("approve")
    approve_steps = approve.add_subparsers(dest="approve_step", required=True)
    approve_prepare = approve_steps.add_parser("prepare")
    approve_prepare.add_argument("--repository", type=Path, required=True)
    approve_prepare.add_argument("--decomposition", type=Path, required=True)
    approve_prepare.add_argument("--output-directory", type=Path, required=True)
    approve_prepare.add_argument("--findings-by-run-file", type=Path, required=True)
    approve_prepare.add_argument("--warning-acknowledgements-file", type=Path, default=None)
    approve_prepare.add_argument(
        "--criteria-texts-by-run-file", type=Path, default=None,
        help="a JSON object of {run_id: [{'id': criterion_id, 'text': quoted "
        "text}, ...]} -- the run-owned criteria quotes to byte-identity "
        "-check against the decomposition's acceptance_criteria (AC-CC04-8); "
        "when omitted, the check runs against the decomposition's own "
        "runs/acceptance_criteria (a tautology -- see check_criteria_byte_identity)",
    )
    approve_prepare.add_argument(
        "--enforce", action="store_true", default=None,
        help="force decomposition-conformance enforcement regardless of "
        "whether the decomposition is conformance-aware (DTR-LK-SYN); pass "
        "the identical flag to 'approve issue' or its fresh re-derivation "
        "refuses on drift",
    )
    approve_issue = approve_steps.add_parser("issue")
    approve_issue.add_argument("--repository", type=Path, required=True)
    approve_issue.add_argument("--subject", type=Path, required=True)
    approve_issue.add_argument("--gate-evidence", type=Path, required=True)
    approve_issue.add_argument("--operator-approval", type=Path, required=True)
    approve_issue.add_argument("--receipt", type=Path, required=True)
    approve_issue.add_argument(
        "--enforce", action="store_true", default=None,
        help="must match the value 'approve prepare' was given, or the "
        "fresh conformance re-derivation refuses on drift (DTR-LK-SYN)",
    )

    run_cmd = subparsers.add_parser("run")
    run_cmd.add_argument("--argv", nargs="+", required=True)

    close_cmd = subparsers.add_parser("close")
    close_cmd.add_argument("--run-result-file", type=Path, required=True)
    close_cmd.add_argument("--attempt-dir", type=Path, default=None)
    close_cmd.add_argument("--next-capture-argv", nargs="+", default=None)
    close_cmd.add_argument("--next-out-dir", type=Path, default=None)
    close_cmd.add_argument(
        "--termination-file", type=Path, default=None,
        help="a JSON file with evaluate_termination's own keyword arguments "
        "(required_cells, new_required_findings, inspector_recall, "
        "amendment_ratio_acknowledged); new_required_findings defaults to "
        "the last ingest's own requires_disposition count when omitted; "
        "when given, close evaluates bounds-termination's success "
        "predicate and checkpoints 'succeeded' when it is met (AC-CC04-5)",
    )

    resume = subparsers.add_parser("resume")
    resume.add_argument("--round", type=int, required=True)
    resume.add_argument("--run-root", type=Path, required=True)
    resume.add_argument("--seed-attempt-id", required=True)
    resume.add_argument("--resume-command", nargs="+", default=list(DEFAULT_RESUME_COMMAND))

    subparsers.add_parser("state")
    return parser


def _load_json_or_default(path: Path | None, default: Any) -> Any:
    if path is None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _dispatch(arguments: argparse.Namespace, driver: ConvergenceCampaignDriver) -> Any:
    step = arguments.step
    if step == "state":
        return driver.state()
    if step == "measure":
        return driver.measure(
            capture_argv=arguments.capture_argv, out_dir=arguments.out_dir,
            timeout=arguments.timeout,
            require_preflight_success=arguments.require_preflight_success,
        )
    if step == "ingest":
        audit_result = _load_json_or_default(arguments.audit_result_file, None)
        return driver.ingest(
            digest=arguments.digest, audit_result=audit_result,
            history_roots=arguments.history_roots or (),
        )
    if step == "rule":
        dispositions = _load_json_or_default(arguments.dispositions_file, [])
        return driver.rule(dispositions=dispositions)
    if step == "plan":
        if arguments.synthesis_config is not None:
            synthesis = json.loads(arguments.synthesis_config.read_text(encoding="utf-8"))
            return driver.plan(synthesis=synthesis)
        if arguments.decomposition is None or arguments.findings_by_run_file is None:
            raise ConvergenceCampaignDriverError(
                "plan requires either --synthesis-config or both "
                "--decomposition and --findings-by-run-file"
            )
        decomposition = json.loads(arguments.decomposition.read_text(encoding="utf-8"))
        findings_by_run = json.loads(arguments.findings_by_run_file.read_text(encoding="utf-8"))
        return driver.plan(decomposition=decomposition, findings_by_run=findings_by_run)
    if step == "approve" and arguments.approve_step == "prepare":
        findings_by_run = json.loads(arguments.findings_by_run_file.read_text(encoding="utf-8"))
        acknowledgements = _load_json_or_default(arguments.warning_acknowledgements_file, [])
        criteria_texts_by_run = _load_json_or_default(arguments.criteria_texts_by_run_file, None)
        packet = driver.approve_prepare(
            repository=arguments.repository, decomposition_path=arguments.decomposition,
            output_directory=arguments.output_directory, findings_by_run=findings_by_run,
            warning_acknowledgements=acknowledgements,
            criteria_texts_by_run=criteria_texts_by_run,
            enforce=arguments.enforce,
        )
        return packet.as_mapping()
    if step == "approve" and arguments.approve_step == "issue":
        receipt = driver.approve_issue(
            repository=arguments.repository, subject_path=arguments.subject,
            gate_evidence_path=arguments.gate_evidence,
            operator_approval_path=arguments.operator_approval, receipt_path=arguments.receipt,
            enforce=arguments.enforce,
        )
        return {"receipt": str(receipt)}
    if step == "run":
        return driver.run_graph(argv=arguments.argv)
    if step == "close":
        run_result = json.loads(arguments.run_result_file.read_text(encoding="utf-8"))
        termination_kwargs = _load_json_or_default(arguments.termination_file, None)
        return driver.close(
            run_result=run_result, attempt_dir=arguments.attempt_dir,
            capture_argv=arguments.next_capture_argv, out_dir=arguments.next_out_dir,
            termination_kwargs=termination_kwargs,
        )
    # step == "resume"
    state = driver.state()
    directive = driver.resume_directive(
        run_root=arguments.run_root, seed_attempt_id=arguments.seed_attempt_id,
        resume_command=tuple(arguments.resume_command), round_number=arguments.round,
    )
    return {
        "round": arguments.round, "argv": list(directive.as_argv()),
        "checkpoint_state": state,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    driver = ConvergenceCampaignDriver(
        campaign_root=arguments.campaign_root,
        campaign_id=arguments.campaign_id,
        max_repair_rounds=arguments.max_repair_rounds,
        repository=arguments.campaign_repository,
    )
    try:
        payload = _dispatch(arguments, driver)
    except (
        ConvergenceCampaignDriverError,
        ConvergenceCampaignError,
        ConvergenceLedgerError,
        FindingHistoryError,
        AutoresumeError,
        PlanSynthesisError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
