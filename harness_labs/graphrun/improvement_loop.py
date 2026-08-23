"""Convergence bridge and loop driver (SI-05).

Turns an **accepted** ``improvement-proposal/1`` (SI-04,
``harness_labs/graphrun/improvement_program.py``) into a bounded convergence
campaign over the harness repository itself, reusing the existing
machinery end to end rather than rebuilding it:

* :class:`~harness_labs.plangraph.convergence_ledger.ConvergenceLedger`
  (CC-01) is the campaign's real cross-round state -- :func:`open_campaign`
  seeds it with the proposal's ``success_criteria`` as the seed finding
  batch, and :func:`remeasure` folds re-audit verdicts into it exactly like
  any other convergence campaign.
* :func:`~harness_labs.plangraph.plan_synthesis.plan_synthesis` (DTR-LK-SYN)
  turns ``ledger.open_findings()`` into one PlanGraph decomposition per
  round -- one repair run per connected ``required_paths`` group, plus the
  join-and-regression run -- and this module never re-implements that
  grouping.
* :mod:`harness_labs.plangraph.plan_approval` (``scripts/approve_plan.py
  prepare``/``issue``) is the *only* door a round's PlanGraph dispatches
  through: :func:`dispatch_round` refuses to call ``launch`` at all without
  an on-disk, ``status: approved`` ``plan-approval-receipt/1`` whose pinned
  subject cites this exact round's decomposition (by
  ``canonical_plan_graph_payload`` digest) -- a receipt for any other
  decomposition, or none at all, is refused.
* :class:`~harness_labs.plangraph.convergence_campaign.CampaignCheckpointStore`
  (CC-02) is this driver's own lifecycle record (atomic replace,
  monotonic sequence): ``open`` / ``succeeded`` / ``incomplete``.

Campaign-root layout (operator ruling, SI-06 escalated finding
``checker-default-root-vs-committed-decompositions``): every artifact this
module writes -- the ledger journal, the checkpoint, the seed-assertions
map, every round's ``decomposition.json``/``findings-by-run.json``, and the
draft decision record a successful close produces -- lives under
``logs/improvement/campaigns/<campaign-id>/``. Round artifacts are
operational state, not committed governance artifacts: nothing this module
writes ever lands under ``docs/improvement/`` or directly under
``docs/decisions/`` (those trees hold only accepted proposals, cited
pattern records, and operator-reviewed decision records --
``scripts/dev/check_improvement_artifacts.py`` is the only checker with a
protocol for that tree, and a real decision record is committed by an
operator, never authored in place by this driver). A round's decomposition
*is* committed into git at its campaign-root path --
``plan_approval.prepare_approval`` requires the decomposition file to
already be a git blob at ``base_commit`` -- but that commit lives under
``logs/improvement/`` alongside the rest of the round's operational state,
never under ``docs/improvement/``.

Termination (SI-05): success when every seeded key is ``observed_fixed`` or
excluded (``waive``); a key whose assertion did not execute folds
``unobserved`` and blocks success termination, exactly as
``ConvergenceLedger.ingest_audit`` already implements it. Hitting the round
bound (default 4) with keys still open closes the campaign ``incomplete``
and reverts every pattern the originating proposal cites back to
``status: candidate`` (``logs/improvement/patterns/<pattern_id>.json``). A
successful close instead promotes every cited pattern to ``status:
addressed`` -- naming, as ``schemas/blocker-pattern.schema.json`` requires
for that status, the ``campaign_id`` that closed it and the
``landing_commit`` its fix landed at -- and drafts (but never commits) a
``docs/decisions/`` record from ``docs/decisions/TEMPLATE.md`` under the
campaign root for an operator to review and land by hand.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness_labs.plangraph.convergence_campaign import (
    CampaignCheckpoint,
    CampaignCheckpointStore,
)
from harness_labs.plangraph.convergence_ledger import (
    ConvergenceLedger,
    ConvergenceLedgerError,
)
from harness_labs.plangraph.plan_approval import (
    PlanApprovalAdmission,
    PlanApprovalError,
)
from harness_labs.plangraph.plan_graph_contract import (
    canonical_plan_graph_payload,
    sha256_json,
)
from harness_labs.plangraph.plan_synthesis import plan_synthesis
from harness_labs.observability.run_forensics import Refusal, SkippedDir

PROPOSAL_PROTOCOL = "improvement-proposal/1"
CAMPAIGN_DOMAIN = "self-improvement"
DEFAULT_ROUND_BOUND = 4
DEFAULT_PLAN_PATH = "docs/development/self-improvement-agent-plan.md"
DEFAULT_PLAN_SECTION_ID = "si-05-loop"
DEFAULT_PLAN_SECTION_HEADING = (
    "## SI-05 — Convergence bridge, loop driver, CLI [si-05-loop]"
)
DEFAULT_CAMPAIGNS_ROOT = Path("logs/improvement/campaigns")
DEFAULT_PATTERNS_ROOT = Path("logs/improvement/patterns")
DEFAULT_DECISIONS_ROOT = Path("docs/decisions")
DEFAULT_DECISION_TEMPLATE_PATH = Path("docs/decisions/TEMPLATE.md")

LIFECYCLE_OPEN = "open"
LIFECYCLE_SUCCEEDED = "succeeded"
LIFECYCLE_INCOMPLETE = "incomplete"
_LIFECYCLES = frozenset({LIFECYCLE_OPEN, LIFECYCLE_SUCCEEDED, LIFECYCLE_INCOMPLETE})

_ACTIVE_KEY_STATUSES = ("open", "fix_claimed")


class ImprovementLoopError(ValueError):
    """Base error for this module."""


class ProposalNotAccepted(ImprovementLoopError):
    """``open`` refuses a proposal with no operator ``accept`` ruling."""


class CampaignAlreadyOpen(ImprovementLoopError):
    """A campaign checkpoint already exists at the target campaign root."""


class CampaignClosed(ImprovementLoopError):
    """The campaign's checkpoint is no longer ``open``."""


class ReceiptMissing(ImprovementLoopError):
    """No issued ``plan-approval-receipt/1`` exists for this round."""


class ReceiptMismatch(ImprovementLoopError):
    """An issued receipt exists but is not for this exact decomposition."""


# ---------------------------------------------------------------------------
# campaign root
# ---------------------------------------------------------------------------


def campaign_root_for(
    repository: Path, campaign_id: str, *, campaigns_root: Path = DEFAULT_CAMPAIGNS_ROOT,
) -> Path:
    return Path(repository).resolve() / campaigns_root / campaign_id


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise ImprovementLoopError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _save_checkpoint(
    campaign_root: Path,
    *,
    campaign_id: str,
    lifecycle: str,
    base_commit: str,
    state: Mapping[str, Any],
) -> CampaignCheckpoint:
    if lifecycle not in _LIFECYCLES:
        raise ImprovementLoopError(f"lifecycle must be one of {sorted(_LIFECYCLES)}")
    store = CampaignCheckpointStore(campaign_root / "checkpoint.json")
    return store.save(
        campaign_id=campaign_id, lifecycle=lifecycle, base_commit=base_commit, state=state,
    )


def _load_checkpoint(campaign_root: Path) -> CampaignCheckpoint:
    store = CampaignCheckpointStore(campaign_root / "checkpoint.json")
    try:
        return store.load()
    except ImprovementLoopError:
        raise
    except Exception as exc:  # convergence_campaign.ConvergenceCampaignError
        raise ImprovementLoopError(
            f"no campaign checkpoint at {campaign_root}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# open: accepted proposal -> campaign root + seeded ConvergenceLedger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedCampaign:
    campaign_id: str
    root: Path
    base_commit: str
    seed_keys: tuple[tuple[str, str], ...]
    pattern_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "root": str(self.root),
            "base_commit": self.base_commit,
            "seed_keys": [list(key) for key in self.seed_keys],
            "pattern_ids": list(self.pattern_ids),
        }


def _load_proposal(proposal_path: Path) -> dict[str, Any]:
    try:
        raw = proposal_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImprovementLoopError(
            f"cannot read proposal at {proposal_path}: {exc}"
        ) from exc
    try:
        proposal = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImprovementLoopError(
            f"proposal at {proposal_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(proposal, Mapping) or proposal.get("protocol") != PROPOSAL_PROTOCOL:
        raise ImprovementLoopError(
            f"proposal at {proposal_path} does not declare protocol "
            f"{PROPOSAL_PROTOCOL!r}"
        )
    return dict(proposal)


def _require_accept_ruling(
    proposal: Mapping[str, Any], *, proposal_path: Path,
) -> dict[str, Any]:
    ruling = proposal.get("ruling")
    if not isinstance(ruling, Mapping) or ruling.get("disposition") != "accept":
        raise ProposalNotAccepted(
            f"proposal at {proposal_path} has no operator 'accept' ruling; "
            "open refuses to campaign against an unruled or rejected/waived "
            "proposal"
        )
    for ruling_field in ("actor", "statement", "ruled_at"):
        value = ruling.get(ruling_field)
        if not isinstance(value, str) or not value.strip():
            raise ProposalNotAccepted(
                f"proposal at {proposal_path} accept ruling is missing a "
                f"non-empty, human-authored {ruling_field!r}"
            )
    return dict(ruling)


def _seed_finding_envelope(proposal_id: str, criterion: Mapping[str, Any]) -> dict[str, Any]:
    file_path = criterion.get("file")
    subject = criterion.get("subject")
    required_paths = criterion.get("required_paths")
    if not isinstance(file_path, str) or not file_path.strip():
        raise ImprovementLoopError(
            "success_criteria entry is missing a non-empty 'file'"
        )
    if not isinstance(subject, str) or not subject.strip():
        raise ImprovementLoopError(
            f"success_criteria entry for {file_path!r} is missing a "
            "non-empty 'subject'"
        )
    if not isinstance(required_paths, list) or not required_paths:
        raise ImprovementLoopError(
            f"success_criteria entry {file_path}/{subject} is missing "
            "non-empty 'required_paths'"
        )
    if file_path not in required_paths:
        raise ImprovementLoopError(
            f"success_criteria entry {file_path}/{subject} 'file' must be a "
            "member of its own 'required_paths'"
        )
    statement = criterion.get("statement")
    return {
        "file": file_path,
        "subject": subject,
        "required_paths": list(required_paths),
        "confidence": "C+S",
        "supersedes_key": None,
        "id": f"{proposal_id}::{file_path}::{subject}",
        "statement": statement if isinstance(statement, str) and statement.strip() else None,
        "category": "self-improvement",
        "severity": None,
        "requires_disposition": False,
        "evidence_refs": [],
        "source_finding_ids": [proposal_id],
    }


def open_campaign(
    *,
    repository: Path,
    proposal_path: Path,
    campaign_id: str | None = None,
    campaigns_root: Path = DEFAULT_CAMPAIGNS_ROOT,
    round_bound: int = DEFAULT_ROUND_BOUND,
) -> OpenedCampaign:
    """Open a bounded convergence campaign for an accepted proposal.

    Refuses (``ProposalNotAccepted``) unless ``proposal_path`` carries a
    ``ruling`` with ``disposition: "accept"`` and a non-empty human actor
    and statement. For an accepted proposal, creates the campaign root
    under ``campaigns_root`` (default ``logs/improvement/campaigns/``),
    opens a real :class:`ConvergenceLedger` there, and ingests every
    ``success_criteria`` entry as the seed finding batch -- the ledger's
    resulting :meth:`~ConvergenceLedger.open_set` is asserted to equal
    exactly the criteria's ``(file, subject)`` pairs before returning.
    """

    repository = Path(repository).resolve()
    proposal_path = Path(proposal_path)
    proposal = _load_proposal(proposal_path)
    ruling = _require_accept_ruling(proposal, proposal_path=proposal_path)

    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise ImprovementLoopError(
            f"proposal at {proposal_path} is missing a non-empty proposal_id"
        )

    success_criteria = proposal.get("success_criteria")
    if not isinstance(success_criteria, list) or not success_criteria:
        raise ImprovementLoopError(
            f"proposal {proposal_id} has no success_criteria to seed a "
            "campaign from"
        )
    seed_findings = [
        _seed_finding_envelope(proposal_id, criterion) for criterion in success_criteria
    ]
    seed_keys: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for finding in seed_findings:
        key = (finding["file"], finding["subject"])
        if key in seen_keys:
            raise ImprovementLoopError(
                f"proposal {proposal_id} success_criteria has duplicate key {key!r}"
            )
        seen_keys.add(key)
        seed_keys.append(key)

    resolved_campaign_id = campaign_id or proposal_id
    root = campaign_root_for(repository, resolved_campaign_id, campaigns_root=campaigns_root)
    if (root / "checkpoint.json").exists():
        raise CampaignAlreadyOpen(f"campaign already opened at {root}")

    base_commit = _git(repository, "rev-parse", "HEAD")
    root.mkdir(parents=True, exist_ok=True)

    # Copied verbatim (never re-serialized) so the recorded target digest
    # addresses exactly the bytes at snapshot_path -- a content-addressed
    # seal, like convergence_campaign.pin_target's own copy-then-digest, not
    # a digest of the source computed separately from a re-encoded copy.
    proposal_bytes = proposal_path.read_bytes()
    proposal_digest = hashlib.sha256(proposal_bytes).hexdigest()
    snapshot_path = root / "proposal.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(proposal_bytes)
    seed_audit_digest = f"seed:{proposal_digest}"

    assertions = {
        f"{criterion['file']}::{criterion['subject']}": criterion.get("assertion")
        for criterion in success_criteria
        if isinstance(criterion, Mapping)
    }
    _write_json(root / "seed-assertions.json", assertions)

    pattern_ids = tuple(
        str(pattern_id) for pattern_id in (proposal.get("pattern_ids") or [])
        if isinstance(pattern_id, str) and pattern_id.strip()
    )

    ledger = ConvergenceLedger(root / "ledger.jsonl")
    ledger.open_campaign(
        domain=CAMPAIGN_DOMAIN,
        target={
            "kind": "improvement-proposal",
            "digest": proposal_digest,
            "snapshot_path": "proposal.json",
        },
        base_commit=base_commit,
        seed_audit_digest=seed_audit_digest,
        config={
            "round_bound": round_bound,
            "proposal_id": proposal_id,
            "pattern_ids": list(pattern_ids),
            "proposal_path": str(proposal_path),
        },
    )
    ledger.ingest_audit({
        "digest": seed_audit_digest,
        "findings": seed_findings,
        "verdicts": [],
        "confirmed_good": [],
        "capture_coverage": {},
    })

    observed_keys = ledger.open_set()
    expected_keys = frozenset(seed_keys)
    if observed_keys != expected_keys:
        raise ImprovementLoopError(
            "seed ingest did not establish the expected key set: expected "
            f"{sorted(expected_keys)}, got {sorted(observed_keys)}"
        )

    _save_checkpoint(
        root, campaign_id=resolved_campaign_id, lifecycle=LIFECYCLE_OPEN,
        base_commit=base_commit,
        state={
            "round_bound": round_bound,
            "rounds_completed": 0,
            "pattern_ids": list(pattern_ids),
            "proposal_id": proposal_id,
            "ruling_actor": ruling["actor"],
        },
    )

    return OpenedCampaign(
        campaign_id=resolved_campaign_id, root=root, base_commit=base_commit,
        seed_keys=tuple(sorted(expected_keys)), pattern_ids=pattern_ids,
    )


# ---------------------------------------------------------------------------
# round: plan_synthesis over open_findings(), written under the campaign root
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesizedRound:
    round_number: int
    round_dir: Path
    decomposition_path: Path
    findings_by_run_path: Path
    decomposition: dict[str, Any]
    findings_by_run: dict[str, list[dict[str, Any]]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_number": self.round_number,
            "round_dir": str(self.round_dir),
            "decomposition_path": str(self.decomposition_path),
            "findings_by_run_path": str(self.findings_by_run_path),
            "run_ids": sorted(self.findings_by_run),
        }


#: The round regression gates cited plan section SI-05 [si-05-loop] names
#: for ``round --launch``: ``python3 -m pytest tests/ -q`` and
#: ``python3 scripts/check_repository_contracts.py``. ``red_green_check.py``'s
#: ``--finding-tests`` red/green gate is built for a review-fix node with one
#: concrete new failing test file to prove red-then-green; a proposal-
#: synthesized repair run has no such file to name (its criteria come from
#: the proposal's ``success_criteria``, not a reviewer's finding), so it is
#: not wired here -- ``pytest tests/ -q`` already runs every test in the
#: tree, including any a repair adds.
_ROUND_REGRESSION_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "pytest", "tests/", "-q"),
    ("python3", "scripts/check_repository_contracts.py"),
)


def _round_regression_verification_argv(referent: str) -> list[str]:
    """``plan_synthesis``'s ``verification_argv_builder`` hook, replacing its
    default referent-existence check with the round's real regression gates.

    Still asserts the observable referent exists (so the check stays real,
    never a no-op) and carries ``referent`` as a literal substring of the
    returned command, satisfying ``decomposition_conformance`` S6 (the
    observable must be reachable from ``verification_argv``).
    """

    commands = json.dumps([list(command) for command in _ROUND_REGRESSION_COMMANDS])
    script = (
        "import json, pathlib, subprocess, sys\n"
        f"assert pathlib.Path({referent!r}).exists(), "
        f"'observable referent missing: {referent}'\n"
        f"for command in json.loads({commands!r}):\n"
        "    subprocess.run(command, check=True)\n"
    )
    return ["python3", "-c", script]


def synthesize_round(
    *,
    repository: Path,
    campaign_root: Path,
    plan_path: str = DEFAULT_PLAN_PATH,
    plan_section_id: str = DEFAULT_PLAN_SECTION_ID,
    plan_section_heading: str = DEFAULT_PLAN_SECTION_HEADING,
    commit_message: str | None = None,
) -> SynthesizedRound:
    """Synthesize the next round's decomposition from
    ``ConvergenceLedger.open_findings()`` and write it under the campaign
    root (operator ruling: never under ``docs/improvement/``), committing
    it into git -- ``plan_approval.prepare_approval`` requires the
    decomposition it admits to already be a git blob at ``base_commit``, so
    a round with no committed decomposition has no path to an issued
    receipt at all.
    """

    repository = Path(repository).resolve()
    campaign_root = Path(campaign_root)
    checkpoint = _load_checkpoint(campaign_root)
    if checkpoint.lifecycle != LIFECYCLE_OPEN:
        raise CampaignClosed(
            f"campaign {checkpoint.campaign_id} is {checkpoint.lifecycle!r}; "
            "no further rounds"
        )

    ledger = ConvergenceLedger(campaign_root / "ledger.jsonl")
    if not ledger.open_findings():
        raise ImprovementLoopError(
            f"campaign {checkpoint.campaign_id} has no open findings to "
            "synthesize a round from"
        )

    base_commit = _git(repository, "rev-parse", "HEAD")
    round_number = int(checkpoint.state.get("rounds_completed", 0)) + 1
    round_dir = campaign_root / "rounds" / str(round_number)
    round_dir.mkdir(parents=True, exist_ok=True)

    result = plan_synthesis(
        ledger,
        plan_path=plan_path,
        plan_section_id=plan_section_id,
        plan_section_heading=plan_section_heading,
        repository=repository,
        base_commit=base_commit,
        verification_argv_builder=_round_regression_verification_argv,
    )

    decomposition_path = round_dir / "decomposition.json"
    decomposition_path.write_text(
        json.dumps(result.decomposition, sort_keys=True) + "\n", encoding="utf-8",
    )
    findings_by_run_path = round_dir / "findings-by-run.json"
    findings_by_run_path.write_text(
        json.dumps(result.findings_by_run, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    relative_decomposition = decomposition_path.relative_to(repository).as_posix()
    relative_findings = findings_by_run_path.relative_to(repository).as_posix()
    # `--ignored` is required here: plain `git status --porcelain`, even
    # pathspec-scoped to these exact paths, silently omits a path an ignore
    # rule covers (never reports it as `??`) -- `logs/improvement/**` stays
    # gitignored (operator ruling, SI-00/AC-SI06-1) -- so without it this
    # guard would see an empty status for every round and skip the add
    # entirely, and prepare_approval's git-blob requirement would fail
    # downstream with no commit ever attempted.
    status = _git(
        repository, "status", "--porcelain", "--ignored", "--",
        relative_decomposition, relative_findings,
    )
    if status.strip():
        # `-f` is required for the same reason as `--ignored` above: `git
        # add` on a path an ignore rule covers exits nonzero without it. The
        # commit itself is still scoped to the same two paths via `--`, so
        # any other work an operator had already staged elsewhere in the
        # index is left exactly as staged, never swept into this round's
        # decomposition commit the way a bare `git commit` would.
        _git(repository, "add", "-f", relative_decomposition, relative_findings)
        _git(
            repository, "commit", "-m",
            commit_message or (
                f"self-improve: campaign {checkpoint.campaign_id} round "
                f"{round_number} decomposition"
            ),
            "--", relative_decomposition, relative_findings,
        )

    return SynthesizedRound(
        round_number=round_number, round_dir=round_dir,
        decomposition_path=decomposition_path, findings_by_run_path=findings_by_run_path,
        decomposition=result.decomposition, findings_by_run=result.findings_by_run,
    )


def load_round(campaign_root: Path, round_number: int) -> SynthesizedRound:
    """Reload a previously synthesized round from disk (used by ``round
    --launch``, a separate CLI invocation from the one that synthesized
    it)."""

    round_dir = Path(campaign_root) / "rounds" / str(round_number)
    decomposition_path = round_dir / "decomposition.json"
    findings_by_run_path = round_dir / "findings-by-run.json"
    if not decomposition_path.exists():
        raise ImprovementLoopError(
            f"no round {round_number} decomposition at {decomposition_path}"
        )
    decomposition = json.loads(decomposition_path.read_text(encoding="utf-8"))
    findings_by_run = (
        json.loads(findings_by_run_path.read_text(encoding="utf-8"))
        if findings_by_run_path.exists() else {}
    )
    return SynthesizedRound(
        round_number=round_number, round_dir=round_dir,
        decomposition_path=decomposition_path, findings_by_run_path=findings_by_run_path,
        decomposition=decomposition, findings_by_run=findings_by_run,
    )


def latest_round_number(campaign_root: Path) -> int | None:
    rounds_dir = Path(campaign_root) / "rounds"
    if not rounds_dir.exists():
        return None
    numbers = [int(entry.name) for entry in rounds_dir.iterdir() if entry.is_dir() and entry.name.isdigit()]
    return max(numbers) if numbers else None


# ---------------------------------------------------------------------------
# dispatch: receipt-gated PlanGraph launch via campaign_launcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundLaunchRequest:
    repository: Path
    campaign_id: str
    round_number: int
    round_dir: Path
    decomposition_path: Path
    decomposition: Mapping[str, Any]
    findings_by_run: Mapping[str, Sequence[Mapping[str, Any]]]
    receipt_path: Path


@dataclass(frozen=True)
class RoundLaunchResult:
    success: bool
    detail: Mapping[str, Any] = field(default_factory=dict)


LaunchCallable = Callable[[RoundLaunchRequest], RoundLaunchResult]


def _default_launch(request: RoundLaunchRequest) -> RoundLaunchResult:
    """Production dispatch: ``campaign_launcher.run_graph`` over the issued
    receipt (the only entry point that starts a PlanGraph attempt).

    ``campaign_launcher.run_graph`` has no repository parameter of its own --
    every path inside it is anchored at its own module-level ``ROOT`` (the
    harness checkout this module lives in), never at a caller-supplied
    repository. Dispatching a campaign opened against any other repository
    through this function would therefore silently run against the wrong
    tree, so it refuses outright instead.
    """

    from harness_labs.graphrun import campaign_launcher

    if request.repository != campaign_launcher.ROOT:
        raise ImprovementLoopError(
            "the production launch dispatches only against the harness "
            f"checkout campaign_launcher.ROOT ({campaign_launcher.ROOT}); "
            f"refusing to dispatch campaign {request.campaign_id!r} opened "
            f"against {request.repository} -- campaign_launcher.run_graph "
            "has no repository parameter to redirect it"
        )

    config = campaign_launcher.build_campaign_launch_config(
        plan_path=DEFAULT_PLAN_PATH,
        decomposition_path=str(
            request.decomposition_path.relative_to(request.repository)
        ),
        logical_graph_id=f"self-improve-{request.campaign_id}",
    )
    graph_attempt_id = f"{request.campaign_id}-round-{request.round_number}"
    run_root = request.round_dir / "attempt"
    status = campaign_launcher.run_graph(
        config, request.receipt_path, graph_attempt_id, run_root,
    )
    return RoundLaunchResult(success=(status == 0), detail={"exit_status": status})


def _resolve_receipt_path(round_dir: Path, receipt_path: Path | None) -> Path:
    path = (receipt_path or (round_dir / "approval" / "receipt.json")).resolve()
    if not path.exists():
        raise ReceiptMissing(
            f"no issued plan-approval receipt at {path}; run "
            "'scripts/approve_plan.py prepare' then 'issue' before "
            "dispatching this round -- the receipt is the only dispatch door"
        )
    return path


def _admit_receipt_for_round(
    repository: Path, round_dir: Path, decomposition: Mapping[str, Any],
    receipt_path: Path | None,
) -> Path:
    """Admit the round's receipt through ``plan_approval.PlanApprovalAdmission``
    -- the same repository-bound revalidation (policy id, gate evidence,
    operator approval, high-severity acknowledgement, and every referenced
    git artifact at ``base_commit``) ``campaign_launcher.run_graph`` performs
    before a real launch -- rather than a hand-rolled subset of it. Every
    launch this driver dispatches, stub or production, relies on this gate
    alone; only the production launch would otherwise recover the full check
    on its own.

    ``PlanApprovalAdmission`` resolves the receipt's own subject/gate/
    operator-approval references relative to ``receipt_path``'s own
    directory (``plan_approval._load_referenced_json``), never a fixed
    ``round_dir / "approval"`` guess, so a ``--receipt`` living outside the
    round directory still reads the files it actually names.
    """

    resolved_path = _resolve_receipt_path(round_dir, receipt_path)
    admission = PlanApprovalAdmission(repository=repository, receipt_path=resolved_path)
    try:
        validated = admission.validate()
    except PlanApprovalError as exc:
        raise ReceiptMismatch(f"{resolved_path} failed plan-approval admission: {exc}") from exc

    expected_digest = sha256_json(canonical_plan_graph_payload(decomposition))
    admitted_digest = sha256_json(canonical_plan_graph_payload(validated.decomposition))
    if admitted_digest != expected_digest:
        raise ReceiptMismatch(
            "issued receipt is not for this round's decomposition: admitted "
            f"decomposition canonical digest {admitted_digest!r} != {expected_digest!r}"
        )
    return resolved_path


def dispatch_round(
    *,
    repository: Path,
    campaign_root: Path,
    round: SynthesizedRound,
    receipt_path: Path | None = None,
    launch: LaunchCallable = _default_launch,
) -> RoundLaunchResult:
    """Dispatch one round's PlanGraph, refusing without an issued receipt
    for exactly this round's decomposition.

    On a successful ``launch``, every key the round's ``findings_by_run``
    covers is projected ``finding_fix_claimed`` (the ledger's own "round
    success" record kind) -- ``remeasure`` is what actually closes a key,
    never this step.
    """

    repository = Path(repository).resolve()
    campaign_root = Path(campaign_root)
    checkpoint = _load_checkpoint(campaign_root)
    if checkpoint.lifecycle != LIFECYCLE_OPEN:
        raise CampaignClosed(
            f"campaign {checkpoint.campaign_id} is {checkpoint.lifecycle!r}; "
            "cannot dispatch a round"
        )

    resolved_receipt_path = _admit_receipt_for_round(
        repository, round.round_dir, round.decomposition, receipt_path,
    )

    request = RoundLaunchRequest(
        repository=repository, campaign_id=checkpoint.campaign_id,
        round_number=round.round_number, round_dir=round.round_dir,
        decomposition_path=round.decomposition_path, decomposition=round.decomposition,
        findings_by_run=round.findings_by_run, receipt_path=resolved_receipt_path,
    )
    outcome = launch(request)

    ledger = ConvergenceLedger(campaign_root / "ledger.jsonl")
    if outcome.success:
        for findings in round.findings_by_run.values():
            for finding in findings:
                key = (finding["file"], finding["subject"])
                if ledger.key_status(key) in _ACTIVE_KEY_STATUSES:
                    try:
                        ledger.record_fix_claimed(
                            key, source="graph_success", round_id=str(round.round_number),
                        )
                    except ConvergenceLedgerError:
                        pass

    _write_json(
        round.round_dir / "outcome.json",
        {"success": outcome.success, "detail": dict(outcome.detail)},
    )

    state = dict(checkpoint.state)
    state["rounds_completed"] = max(int(state.get("rounds_completed", 0)), round.round_number)
    _save_checkpoint(
        campaign_root, campaign_id=checkpoint.campaign_id, lifecycle=LIFECYCLE_OPEN,
        base_commit=checkpoint.base_commit, state=state,
    )
    return outcome


# ---------------------------------------------------------------------------
# remeasure: re-run assertions, fold verdicts, bounded termination
# ---------------------------------------------------------------------------


AssertionRunner = Callable[[tuple[str, str], "Mapping[str, Any] | None", Path], str]
#: Returns ``"pass"`` / ``"fail"`` / ``"unexecuted"``.


def _assertion_text(assertion: "Mapping[str, Any] | None") -> str:
    if not isinstance(assertion, Mapping):
        return "assertion"
    argv = assertion.get("argv")
    if isinstance(argv, list) and argv:
        return " ".join(str(item) for item in argv)
    signature_absent = assertion.get("signature_absent")
    if isinstance(signature_absent, str) and signature_absent.strip():
        return f"signature_absent:{signature_absent}"
    return "assertion"


def _default_assertion_runner(
    key: tuple[str, str], assertion: "Mapping[str, Any] | None", repository: Path,
) -> str:
    """Re-execute one key's proposal-authored assertion.

    ``{"argv": [...], "timeout_seconds": N}`` runs as a subprocess in
    ``repository``; return code 0 is ``"pass"``. ``{"signature_absent":
    "<sig>"}`` re-runs the SI-02 miner (``run_forensics.mine``) over
    ``logs/runs`` and checks whether the signature reappears among the
    *new* (post-round, watermark-fresh) observations -- absent from a
    nonempty fresh batch is ``"pass"``; present is ``"fail"``; no fresh
    runs to mine from is ``"unexecuted"`` (there is nothing to observe an
    absence against yet). Anything the runner cannot actually execute
    (malformed assertion, missing interpreter, timeout) is ``"unexecuted"``,
    never guessed as a pass or a fail.
    """

    if not isinstance(assertion, Mapping):
        return "unexecuted"
    argv = assertion.get("argv")
    if isinstance(argv, list) and argv:
        timeout = assertion.get("timeout_seconds") or 60
        try:
            completed = subprocess.run(
                [str(item) for item in argv], cwd=repository, timeout=float(timeout),
                capture_output=True, check=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return "unexecuted"
        return "pass" if completed.returncode == 0 else "fail"

    signature_absent = assertion.get("signature_absent")
    if isinstance(signature_absent, str) and signature_absent.strip():
        runs_root = repository / "logs" / "runs"
        state_root = repository / "logs" / "improvement" / "state"
        if not runs_root.exists():
            return "unexecuted"
        try:
            from harness_labs.observability.run_forensics import mine
            result = mine(runs_root, state_root=state_root)
        except Exception:
            return "unexecuted"
        if not result.new_run_dirs:
            return "unexecuted"
        signatures = {observation.get("signature") for observation in result.observations}
        return "fail" if signature_absent in signatures else "pass"

    return "unexecuted"


def _load_seed_assertions(campaign_root: Path) -> dict[str, Any]:
    path = Path(campaign_root) / "seed-assertions.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _revert_patterns_to_candidate(
    repository: Path, pattern_ids: Sequence[str], *, patterns_root: Path = DEFAULT_PATTERNS_ROOT,
) -> list[str]:
    root = Path(repository) / patterns_root
    reverted: list[str] = []
    for pattern_id in pattern_ids:
        path = root / f"{pattern_id}.json"
        if not path.exists():
            continue
        pattern = json.loads(path.read_text(encoding="utf-8"))
        if pattern.get("status") == "candidate":
            continue
        pattern["status"] = "candidate"
        _write_json(path, pattern)
        reverted.append(pattern_id)
    return reverted


def _remeasure_digest(
    campaign_id: str, round_number: int, results: Mapping[str, str],
) -> str:
    """A digest that changes whenever this remeasure's *observed results*
    change, not just when the open-key set does.

    ``ConvergenceLedger.ingest_audit`` is idempotent by digest alone -- a
    re-ingest of an already-seen digest folds nothing, on purpose. A digest
    built only from ``(campaign_id, round, keys)`` (the open-key set) is
    identical across two remeasures run at the same round with an unchanged
    open-key set even when their assertion outcomes differ, so the second
    remeasure's real verdicts would be silently swallowed. Folding
    ``results`` (the per-key raw assertion outcome) in means a remeasure
    whose observations actually changed always gets a fresh digest and is
    always folded; only a byte-identical repeat stays idempotent.
    """

    payload = json.dumps(
        {
            "campaign_id": campaign_id, "round": round_number,
            "results": dict(sorted(results.items())),
        },
        sort_keys=True,
    )
    return "remeasure:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _target_surface_key(target_surface: Sequence[Mapping[str, Any]]) -> str:
    """The anti-thrash ledger's ``target_surface`` identity for a proposal:
    its cited paths, sorted and joined -- the same derivation ``run_audit``
    uses when it opens a proposal-ledger entry for a drafted proposal, so a
    campaign's close-out promotion can find and close that same entry."""

    return "|".join(
        sorted(
            str(entry["path"]) for entry in target_surface
            if isinstance(entry, Mapping) and entry.get("path")
        )
    )


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload) if isinstance(payload, list) else []


def _promote_patterns_to_addressed(
    repository: Path, campaign_root: Path, pattern_ids: Sequence[str],
    *, campaign_id: str, landing_commit: str, patterns_root: Path = DEFAULT_PATTERNS_ROOT,
) -> tuple[str, ...]:
    """Close-out promotion (cited plan section SI-05 [si-05-loop]: "The
    pattern flips to ``addressed``"; module docstring: "the campaign
    orchestration library, including close-out promotion").

    Runs only on a campaign's ``succeeded`` close. Flips every cited
    pattern's ``status`` to ``"addressed"`` and stamps ``campaign_id``
    (this campaign) and ``landing_commit`` (``repository``'s ``HEAD`` at
    promotion time -- the post-integration commit the fix actually landed
    at) on it, exactly as ``schemas/blocker-pattern.schema.json`` requires
    of every ``status: "addressed"`` record. Also closes its anti-thrash
    proposal-ledger entry (``observability.improvement_index.close_proposal``)
    when one is open -- a proposal opened by hand rather than through
    ``run_audit``'s ``--propose-if-ready`` path never had a ledger entry to
    close, which is not an error here.
    """

    from harness_labs.observability.improvement_index import close_proposal

    proposal_path = Path(campaign_root) / "proposal.json"
    if not proposal_path.exists() or not pattern_ids:
        return ()
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    surface_key = _target_surface_key(proposal.get("target_surface") or [])
    now = datetime.now(timezone.utc).isoformat()

    repository = Path(repository)
    root = repository / patterns_root
    ledger_path = repository / DEFAULT_PATTERNS_ROOT.parent / "proposal-ledger.json"
    proposal_ledger = _load_json_list(ledger_path)

    promoted: list[str] = []
    for pattern_id in pattern_ids:
        pattern_path = root / f"{pattern_id}.json"
        if not pattern_path.exists():
            continue
        pattern = json.loads(pattern_path.read_text(encoding="utf-8"))
        if (
            pattern.get("status") != "addressed"
            or pattern.get("campaign_id") != campaign_id
            or pattern.get("landing_commit") != landing_commit
        ):
            pattern["status"] = "addressed"
            pattern["campaign_id"] = campaign_id
            pattern["landing_commit"] = landing_commit
            _write_json(pattern_path, pattern)
            promoted.append(pattern_id)
        observation_count = int(
            (pattern.get("support") or {}).get("observation_count", 0)
        )
        try:
            proposal_ledger = close_proposal(
                proposal_ledger, target_surface=surface_key, pattern_id=pattern_id,
                disposition="closed", observation_count=observation_count, now=now,
            )
        except ValueError:
            continue

    _write_json(ledger_path, proposal_ledger)
    return tuple(promoted)


# ---------------------------------------------------------------------------
# draft_decision_record: close-out decision drafting (never committed here)
# ---------------------------------------------------------------------------


_DECISION_FILENAME_RE = re.compile(r"^(\d{4})-.*\.md$")
_DECISION_FRONTMATTER_KEYS = (
    "Status", "Supersedes", "Concerns-paths", "Valid-from-commit",
    "Date", "Owners", "Run",
)


def _slugify(text: str, *, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "self-improvement"


def _next_decision_number(decisions_root: Path) -> str:
    """The next free ``NNNN`` in ``decisions_root`` -- one past the highest
    numbered ``NNNN-*.md`` record already on disk there (``0001`` if none),
    computed only for numbering: nothing is ever read from or written to
    ``decisions_root`` beyond this scan."""

    highest = 0
    root = Path(decisions_root)
    if root.is_dir():
        for entry in root.iterdir():
            match = _DECISION_FILENAME_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{highest + 1:04d}"


def _render_decision_frontmatter(template_text: str, values: Mapping[str, str]) -> list[str]:
    lines = template_text.splitlines()
    rendered: list[str] = []
    for line in lines:
        replaced = False
        for key in _DECISION_FRONTMATTER_KEYS:
            prefix = f"{key}:"
            if line.startswith(prefix) and key in values:
                rendered.append(f"{key}: {values[key]}")
                replaced = True
                break
        if not replaced:
            rendered.append(line)
    return rendered


def draft_decision_record(
    *,
    campaign_root: Path,
    proposal: Mapping[str, Any],
    template_path: Path,
    decisions_root: Path,
    campaign_id: str,
    landing_commit: str,
    observed_fixed_keys: Sequence[tuple[str, str]] = (),
    excluded_keys: Sequence[tuple[str, str]] = (),
    remeasure_digest: str | None = None,
    now: str | None = None,
) -> Path:
    """Draft (never commit) a ``docs/decisions/`` record for a campaign
    whose keys are all ``observed_fixed`` or excluded (``waive``).

    Reads the decision template at ``template_path`` (the
    ``docs/decisions/TEMPLATE.md`` shape -- a path parameter, never
    hardcoded), renders a ``NNNN-<slug>.md`` with ``Concerns-paths:``
    filled from ``proposal``'s ``target_surface`` paths and a before/after
    evidence summary appended to the "Validation and reversal" section, and
    writes it under ``campaign_root`` -- never directly into a real
    ``docs/decisions/``, which stays an operator's hand-reviewed commit.
    ``NNNN`` is one past the highest ``NNNN-*.md`` already present in
    ``decisions_root`` (the real decisions tree, consulted only for
    numbering).
    """

    template_path = Path(template_path)
    if not template_path.is_file():
        raise ImprovementLoopError(
            f"no decision template at {template_path}; draft_decision_record "
            "cannot render a decision record without one"
        )
    template_text = template_path.read_text(encoding="utf-8")

    proposal_id = str(proposal.get("proposal_id") or campaign_id)
    choice = proposal.get("choice")
    title_text = str(choice) if isinstance(choice, str) and choice.strip() else proposal_id
    number = _next_decision_number(decisions_root)
    slug = _slugify(title_text)
    filename = f"{number}-{slug}.md"

    concerns_paths = ", ".join(
        sorted(
            str(entry.get("path")) for entry in (proposal.get("target_surface") or [])
            if isinstance(entry, Mapping) and entry.get("path")
        )
    )
    now = now or datetime.now(timezone.utc).isoformat()
    values = {
        "Status": "proposed",
        "Concerns-paths": concerns_paths or "(none declared)",
        "Valid-from-commit": landing_commit,
        "Date": now[:10],
        "Owners": "self-improvement agent (SI-05 loop driver) -- operator review required",
        "Run": f"campaign {campaign_id}",
    }
    lines = _render_decision_frontmatter(template_text, values)
    if lines and lines[0].startswith("# "):
        lines[0] = f"# {number} — {title_text}"

    fixed_text = ", ".join(f"{file}::{subject}" for file, subject in sorted(observed_fixed_keys))
    excluded_text = ", ".join(f"{file}::{subject}" for file, subject in sorted(excluded_keys))
    evidence_lines = [
        "",
        "Draft evidence (auto-appended by "
        "harness_labs.graphrun.improvement_loop.draft_decision_record; "
        "operator review required before this record is committed):",
        f"- Before: campaign {campaign_id} opened with keys "
        f"{fixed_text or '(none)'} open"
        + (f", and {excluded_text} excluded from the start" if excluded_text else "") + ".",
        "- After: every seeded key closed observed_fixed or excluded at commit "
        f"{landing_commit}"
        + (f" (remeasure digest {remeasure_digest})." if remeasure_digest else "."),
    ]

    body = "\n".join(lines).rstrip("\n") + "\n" + "\n".join(evidence_lines) + "\n"

    draft_path = Path(campaign_root) / "decision-draft" / filename
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(body, encoding="utf-8")
    return draft_path


@dataclass(frozen=True)
class RemeasureOutcome:
    digest: str
    observed_fixed: tuple[tuple[str, str], ...]
    reopened: tuple[tuple[str, str], ...]
    unexecuted: tuple[tuple[str, str], ...]
    still_broken: tuple[tuple[str, str], ...]
    lifecycle: str
    open_keys: tuple[tuple[str, str], ...]
    reverted_pattern_ids: tuple[str, ...] = ()
    promoted_pattern_ids: tuple[str, ...] = ()
    decision_draft_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "observed_fixed": [list(k) for k in self.observed_fixed],
            "reopened": [list(k) for k in self.reopened],
            "unexecuted": [list(k) for k in self.unexecuted],
            "still_broken": [list(k) for k in self.still_broken],
            "lifecycle": self.lifecycle,
            "open_keys": [list(k) for k in self.open_keys],
            "reverted_pattern_ids": list(self.reverted_pattern_ids),
            "promoted_pattern_ids": list(self.promoted_pattern_ids),
            "decision_draft_path": self.decision_draft_path,
        }


def remeasure(
    *,
    repository: Path,
    campaign_root: Path,
    assertion_runner: AssertionRunner = _default_assertion_runner,
    digest: str | None = None,
    decisions_root: Path | None = None,
    decision_template_path: Path | None = None,
) -> RemeasureOutcome:
    """Re-run every open key's assertion and fold the resulting verdicts.

    Only a ``"pass"`` result closes a key (``observed_fixed``); a
    ``"fail"`` result against a ``fix_claimed`` key demotes it back to
    ``open`` (``reopened`` -- an unsuccessful repair claim); a ``"fail"``
    against a key never claimed fixed is recorded in ``still_broken``
    (executed, and still observed broken) -- distinct from ``unexecuted``
    (the assertion did not run at all). Neither ``still_broken`` nor
    ``unexecuted`` is placed in the ingested ``verdicts`` list, so
    ``ConvergenceLedger.ingest_audit`` folds both into that audit's
    ``unobserved`` set automatically -- this driver never authors an
    ``unobserved`` verdict itself. Closes the campaign ``succeeded`` once
    :meth:`~ConvergenceLedger.open_set` is empty -- promoting every cited
    pattern to ``status: addressed`` (with its closing ``campaign_id`` and
    ``landing_commit``) and drafting a decision record under the campaign
    root via :func:`draft_decision_record` -- or ``incomplete`` (and
    reverts every cited pattern to ``status: candidate``) once the round
    bound is hit with keys still open.
    """

    repository = Path(repository).resolve()
    campaign_root = Path(campaign_root)
    checkpoint = _load_checkpoint(campaign_root)
    ledger = ConvergenceLedger(campaign_root / "ledger.jsonl")

    if checkpoint.lifecycle != LIFECYCLE_OPEN:
        return RemeasureOutcome(
            digest="", observed_fixed=(), reopened=(), unexecuted=(), still_broken=(),
            lifecycle=checkpoint.lifecycle, open_keys=tuple(sorted(ledger.open_set())),
        )

    assertions = _load_seed_assertions(campaign_root)
    active_keys = sorted(ledger.open_set())

    verdicts: list[dict[str, Any]] = []
    coverage: dict[str, str] = {}
    observed_fixed: list[tuple[str, str]] = []
    reopened: list[tuple[str, str]] = []
    unexecuted: list[tuple[str, str]] = []
    still_broken: list[tuple[str, str]] = []
    results: dict[str, str] = {}

    for key in active_keys:
        file_path, subject = key
        assertion = assertions.get(f"{file_path}::{subject}")
        result = assertion_runner(key, assertion, repository)
        results[f"{file_path}::{subject}"] = result
        cell = f"assertion::{file_path}::{subject}"
        if result == "pass":
            coverage[cell] = "ok"
            verdicts.append({
                "key": list(key), "verdict": "observed_fixed",
                "capture_cell": cell, "assertion": _assertion_text(assertion),
            })
            observed_fixed.append(key)
        elif result == "fail" and ledger.key_status(key) == "fix_claimed":
            verdicts.append({"key": list(key), "verdict": "reopened"})
            reopened.append(key)
        elif result == "fail":
            still_broken.append(key)
        else:
            unexecuted.append(key)

    round_number = int(checkpoint.state.get("rounds_completed", 0))
    ingest_digest = digest or _remeasure_digest(
        checkpoint.campaign_id, round_number, results,
    )
    ledger.ingest_audit({
        "digest": ingest_digest,
        "findings": [],
        "verdicts": verdicts,
        "confirmed_good": [],
        "capture_coverage": coverage,
    })

    open_keys = tuple(sorted(ledger.open_set()))
    round_bound = int(checkpoint.state.get("round_bound", DEFAULT_ROUND_BOUND))
    lifecycle = LIFECYCLE_OPEN
    reverted: tuple[str, ...] = ()
    promoted: tuple[str, ...] = ()
    decision_draft_path: str | None = None
    if not open_keys:
        lifecycle = LIFECYCLE_SUCCEEDED
        landing_commit = _git(repository, "rev-parse", "HEAD")
        promoted = _promote_patterns_to_addressed(
            repository, campaign_root, checkpoint.state.get("pattern_ids", []),
            campaign_id=checkpoint.campaign_id, landing_commit=landing_commit,
        )
        proposal_path = campaign_root / "proposal.json"
        if proposal_path.is_file():
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            # A campaign that just succeeded has no open keys left, so every
            # key this ledger has ever tracked is either excluded (waive) or
            # was, at some round, observed_fixed -- key_lineage() names the
            # full cumulative set across every round, not just this one.
            excluded_keys = tuple(sorted(ledger.exclusion_set()))
            all_keys = tuple(sorted(ledger.key_lineage().keys()))
            fixed_keys = tuple(key for key in all_keys if key not in excluded_keys)
            draft_path = draft_decision_record(
                campaign_root=campaign_root,
                proposal=proposal,
                template_path=decision_template_path or (repository / DEFAULT_DECISION_TEMPLATE_PATH),
                decisions_root=decisions_root or (repository / DEFAULT_DECISIONS_ROOT),
                campaign_id=checkpoint.campaign_id,
                landing_commit=landing_commit,
                observed_fixed_keys=fixed_keys,
                excluded_keys=excluded_keys,
                remeasure_digest=ingest_digest,
            )
            decision_draft_path = str(draft_path)
    elif round_number >= round_bound:
        lifecycle = LIFECYCLE_INCOMPLETE
        reverted = tuple(
            _revert_patterns_to_candidate(repository, checkpoint.state.get("pattern_ids", []))
        )

    state = dict(checkpoint.state)
    state["last_remeasure_digest"] = ingest_digest
    state["open_keys"] = [list(key) for key in open_keys]
    if lifecycle != LIFECYCLE_OPEN:
        state["closed_reason"] = (
            "every specification key is observed_fixed or excluded"
            if lifecycle == LIFECYCLE_SUCCEEDED
            else f"round bound {round_bound} reached with {len(open_keys)} key(s) still open"
        )
        state["reverted_pattern_ids"] = list(reverted)
        state["promoted_pattern_ids"] = list(promoted)
        if decision_draft_path is not None:
            state["decision_draft_path"] = decision_draft_path
    _save_checkpoint(
        campaign_root, campaign_id=checkpoint.campaign_id, lifecycle=lifecycle,
        base_commit=checkpoint.base_commit, state=state,
    )

    return RemeasureOutcome(
        digest=ingest_digest, observed_fixed=tuple(observed_fixed),
        reopened=tuple(reopened), unexecuted=tuple(unexecuted), still_broken=tuple(still_broken),
        lifecycle=lifecycle, open_keys=open_keys, reverted_pattern_ids=reverted,
        promoted_pattern_ids=promoted, decision_draft_path=decision_draft_path,
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignStatus:
    campaign_id: str
    lifecycle: str
    rounds_completed: int
    round_bound: int
    open_keys: tuple[tuple[str, str], ...]
    excluded_keys: tuple[tuple[str, str], ...]
    pattern_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "lifecycle": self.lifecycle,
            "rounds_completed": self.rounds_completed,
            "round_bound": self.round_bound,
            "open_keys": [list(key) for key in self.open_keys],
            "excluded_keys": [list(key) for key in self.excluded_keys],
            "pattern_ids": list(self.pattern_ids),
        }


def campaign_status(*, campaign_root: Path) -> CampaignStatus:
    campaign_root = Path(campaign_root)
    checkpoint = _load_checkpoint(campaign_root)
    ledger = ConvergenceLedger(campaign_root / "ledger.jsonl")
    return CampaignStatus(
        campaign_id=checkpoint.campaign_id, lifecycle=checkpoint.lifecycle,
        rounds_completed=int(checkpoint.state.get("rounds_completed", 0)),
        round_bound=int(checkpoint.state.get("round_bound", DEFAULT_ROUND_BOUND)),
        open_keys=tuple(sorted(ledger.open_set())),
        excluded_keys=tuple(sorted(ledger.exclusion_set())),
        pattern_ids=tuple(checkpoint.state.get("pattern_ids", [])),
    )


# ---------------------------------------------------------------------------
# audit: SI-02 mining + SI-03 clustering (deterministic; --propose-if-ready
# additionally drafts proposals when a judgment callable is injected)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditResult:
    observations: tuple[dict[str, Any], ...]
    patterns: tuple[dict[str, Any], ...]
    proposals: tuple[dict[str, Any], ...]
    skipped: tuple[SkippedDir, ...] = ()
    refused: tuple[Refusal, ...] = ()
    excluded_run_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_count": len(self.observations),
            "pattern_ids": sorted(
                str(pattern.get("pattern_id")) for pattern in self.patterns
            ),
            "proposal_ids": sorted(
                str(proposal.get("proposal_id")) for proposal in self.proposals
            ),
            "skipped": [s.as_dict() for s in self.skipped],
            "refused": [r.as_dict() for r in self.refused],
            "excluded_run_ids": list(self.excluded_run_ids),
        }


def _repository_id(repository: Path) -> str:
    identity_path = repository / ".harness" / "repository.json"
    if identity_path.exists():
        try:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "unknown"
        value = payload.get("repository_id") or payload.get("id")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _discover_campaign_journals(
    repository: Path, *, campaigns_root: Path = DEFAULT_CAMPAIGNS_ROOT,
) -> list[tuple[Path, str, str]]:
    campaigns_dir = repository / campaigns_root
    if not campaigns_dir.exists():
        return []
    repository_id = _repository_id(repository)
    entries: list[tuple[Path, str, str]] = []
    for campaign_dir in sorted(campaigns_dir.iterdir()):
        ledger_path = campaign_dir / "ledger.jsonl"
        if ledger_path.is_file():
            entries.append((ledger_path, campaign_dir.name, repository_id))
    return entries


#: The status values ``improvement_index.cluster_observations`` can itself
#: assign (its own module docstring: "The remaining schema-level statuses
#: ... are set by later layers ... and are never touched here"). Anything
#: else already on disk came from a later layer (SI-04 drafting, SI-05
#: close-out, a human ruling) and a re-audit must not clobber it back.
_CLUSTERING_OWNED_STATUSES = frozenset({"observed", "candidate"})


def _merge_pattern_record(path: Path, fresh: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh a pattern record's clustering-owned fields from ``fresh``
    without clobbering what a later layer already wrote on disk.

    Without this, every audit run's wholesale rewrite would erase the
    candidate status a prior campaign close already set (back to whatever
    the fresh cluster recomputes -- ``observed``/``candidate``) and any
    SI-04 generalizability verdict, since ``cluster_observations`` always
    starts a fresh record's ``generalizability.verdict`` at ``None``. Also
    preserves the ``campaign_id``/``landing_commit`` a successful SI-05
    close already stamped -- a fresh cluster pass never computes either.
    """

    if not path.exists():
        return dict(fresh)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fresh)
    if not isinstance(existing, Mapping):
        return dict(fresh)

    merged = dict(fresh)
    if existing.get("status") not in _CLUSTERING_OWNED_STATUSES:
        merged["status"] = existing["status"]
    existing_generalizability = existing.get("generalizability")
    if (
        isinstance(existing_generalizability, Mapping)
        and existing_generalizability.get("verdict") is not None
    ):
        merged["generalizability"] = dict(existing_generalizability)
    existing_recurrence = existing.get("recurrence")
    if existing_recurrence:
        merged["recurrence"] = existing_recurrence
    existing_first_seen = existing.get("first_seen_at")
    if isinstance(existing_first_seen, str) and existing_first_seen:
        merged["first_seen_at"] = existing_first_seen
    if isinstance(existing.get("campaign_id"), str) and existing.get("campaign_id"):
        merged["campaign_id"] = existing["campaign_id"]
    if isinstance(existing.get("landing_commit"), str) and existing.get("landing_commit"):
        merged["landing_commit"] = existing["landing_commit"]
    return merged


def run_audit(
    *,
    repository: Path,
    propose_if_ready: bool = False,
    judgment: Callable[[Mapping[str, Any]], Any] | None = None,
) -> AuditResult:
    """Mine ``logs/runs`` (SI-02) and cluster into patterns (SI-03),
    writing pattern records to ``logs/improvement/patterns/``.

    Deterministic and safe on a schedule. ``--propose-if-ready`` only
    drafts proposals (SI-04's ``draft_proposal``) when a real ``judgment``
    callable is injected -- with none, mining and clustering still run in
    full, but no proposal is fabricated from a model this call was never
    given. When it does draft, every candidate is additionally gated by
    SI-03's anti-thrash ledger (``observability.improvement_index.
    evaluate_anti_thrash``/``open_proposal``, persisted at
    ``logs/improvement/proposal-ledger.json``) against a real
    ``DecisionRegistry`` loaded from ``docs/decisions`` -- never an empty
    one, which would make SI-04's governed-path citation refusal and SI-03's
    re-proposal bars both vacuous.
    """

    from harness_labs.observability.improvement_index import cluster_observations, is_proposable
    from harness_labs.observability.run_forensics import mine

    repository = Path(repository).resolve()
    runs_root = repository / "logs" / "runs"
    state_root = repository / "logs" / "improvement" / "state"
    patterns_root = repository / DEFAULT_PATTERNS_ROOT
    if not runs_root.exists():
        return AuditResult(observations=(), patterns=(), proposals=())


    now = datetime.now(timezone.utc).isoformat()
    mining = mine(runs_root, state_root=state_root)
    patterns = cluster_observations(mining.observations, now=now)
    patterns_root.mkdir(parents=True, exist_ok=True)
    for pattern in patterns:
        pattern_id = pattern.get("pattern_id")
        if isinstance(pattern_id, str) and pattern_id:
            pattern_path = patterns_root / f"{pattern_id}.json"
            _write_json(pattern_path, _merge_pattern_record(pattern_path, pattern))

    proposals: list[dict[str, Any]] = []
    if propose_if_ready and judgment is not None:
        from harness_labs.core.decision_registry import DecisionRegistry, load_decisions
        from harness_labs.plangraph.finding_history import fold_campaigns
        from harness_labs.graphrun.improvement_program import draft_proposal, ProposalRefused
        from harness_labs.observability.improvement_index import evaluate_anti_thrash, open_proposal

        history = fold_campaigns(_discover_campaign_journals(repository))
        decisions_dir = repository / "docs" / "decisions"
        registry = load_decisions(decisions_dir) if decisions_dir.is_dir() else DecisionRegistry(())
        ledger_path = repository / DEFAULT_PATTERNS_ROOT.parent / "proposal-ledger.json"
        proposal_ledger = _load_json_list(ledger_path)
        for pattern in patterns:
            if not is_proposable(pattern):
                continue
            try:
                proposal = draft_proposal(
                    pattern, judgment=judgment, finding_history=history,
                    decision_registry=registry,
                    proposal_id=f"proposal-{pattern['pattern_id']}",
                )
            except ProposalRefused:
                continue
            surface_key = _target_surface_key(proposal.target_surface)
            decision = evaluate_anti_thrash(
                proposal_ledger, target_surface=surface_key,
                pattern_id=pattern["pattern_id"],
                observation_count=pattern["support"]["observation_count"], now=now,
            )
            if not decision.allowed:
                continue
            proposal_ledger = open_proposal(
                proposal_ledger, target_surface=surface_key,
                pattern_id=pattern["pattern_id"],
                observation_count=pattern["support"]["observation_count"], now=now,
            )
            proposals.append(proposal.to_dict())
        _write_json(ledger_path, proposal_ledger)

    return AuditResult(
        observations=tuple(mining.observations), patterns=tuple(patterns),
        proposals=tuple(proposals),
        skipped=tuple(mining.skipped), refused=tuple(mining.refused),
        excluded_run_ids=tuple(mining.excluded_run_ids),
    )


__all__ = [
    "AssertionRunner",
    "AuditResult",
    "CampaignAlreadyOpen",
    "CampaignClosed",
    "CampaignStatus",
    "DEFAULT_CAMPAIGNS_ROOT",
    "DEFAULT_DECISIONS_ROOT",
    "DEFAULT_DECISION_TEMPLATE_PATH",
    "DEFAULT_PATTERNS_ROOT",
    "DEFAULT_PLAN_PATH",
    "DEFAULT_PLAN_SECTION_HEADING",
    "DEFAULT_PLAN_SECTION_ID",
    "DEFAULT_ROUND_BOUND",
    "ImprovementLoopError",
    "LaunchCallable",
    "LIFECYCLE_INCOMPLETE",
    "LIFECYCLE_OPEN",
    "LIFECYCLE_SUCCEEDED",
    "OpenedCampaign",
    "ProposalNotAccepted",
    "ReceiptMismatch",
    "ReceiptMissing",
    "RemeasureOutcome",
    "RoundLaunchRequest",
    "RoundLaunchResult",
    "SynthesizedRound",
    "campaign_root_for",
    "campaign_status",
    "dispatch_round",
    "draft_decision_record",
    "latest_round_number",
    "load_round",
    "open_campaign",
    "remeasure",
    "run_audit",
    "synthesize_round",
]
