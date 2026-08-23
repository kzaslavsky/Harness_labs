"""Deterministic, read-only forensic miner: run journals -> blocker-observation/1.

Admits a run only when its audit hash chain verifies via
`harness_labs.observability.run_metrics.project_run_metrics` -- authenticated
input or nothing (AC-SI02-1). A run whose chain fails verification is never
silently dropped: it is reported in ``MiningResult.refused`` with the
verification failure as its reason.

Only ``production_lifecycle`` runs are folded into the observation aggregate;
every other evidence classification (``component``, ``synthetic``,
``fabricated_fixture``) is still admitted and parsed -- so a chain-verification
refusal is never confused with a classification exclusion -- but contributes
nothing to the emitted observations (AC-SI02-3).

Only the artifact files a chain-verified event actually declares in its own
``artifacts`` list are ever mined: ``_verify_event_journal`` hash-checks every
declared artifact regardless of run terminality, so an artifacts/ file that
no event references is unauthenticated bytes and is skipped rather than
trusted on path-existence alone.

Run discovery follows the dashboard run-catalog's audit-root semantics
(``run_catalog.build_run_catalog``): the direct, non-symlinked, non-dotted
children of a root are its runs. PlanGraph campaigns, however, register the
*graph root* as the audit root and nest their runs one or more levels deeper
-- ``logs/runs/<graph-root>/<run-id>/`` for a plain campaign, and
``logs/runs/<graph-root>/feature-runs/<run-id>/`` when an intermediate,
non-run-shaped grouping directory (e.g. ``feature-runs/``) sits between the
graph root and its runs -- so a miner that only ever looks at immediate
children of ``logs/runs`` sees nothing but containers and mines zero
observations. ``_iter_run_dirs`` therefore recognizes a directory that is
not itself run-shaped (no ``events.jsonl``) but holds run-shaped descendants
as a *container* and recurses into it, up to ``MAX_CONTAINER_DEPTH`` levels
of non-run-shaped nesting, rather than stopping after one. Watermark keys
stay stable for flat runs (the directory name) and are qualified with every
intermediate directory name for nested ones (``<graph-root>/<run-id>``, or
``<graph-root>/<intermediate>/<run-id>`` for the two-level shape), joined
with ``/`` down the full path from the runs root, so two graph roots (or two
intermediate groupings) holding same-named run directories never collide.
Every directory that is neither run-shaped nor a container -- and every
non-run descendant of a container that itself yields no run-shaped
descendant within the depth bound -- is reported in ``MiningResult.skipped``
with the reason it was not mined, so an empty or thin harvest always
explains itself instead of silently capping coverage.

Mining is watermarked per run directory under a caller-supplied state root
(``logs/improvement/state/`` in production, per SI-02), keyed on that run's
verified chain head hash and event count, not on directory name alone: an
unfinished run whose journal has grown since it was last watermarked is
revisited rather than sealed forever, and a run refused for a chain failure
is never watermarked at all, so it is retried on every call until it either
verifies or the corruption is fixed. A second call over a corpus whose
watermarked runs are otherwise unchanged emits no new observations and
adding one new run directory mines only that run (AC-SI02-4).

Every ``signature`` field is normalized: run ids (both the emitting run's own
id and any other run id that happens to appear in free text), absolute
paths, and timestamps (full and date-only) are stripped so nothing secret or
run-specific survives into a value that gets aggregated across runs
(AC-SI02-2).

Cause-shaped extraction
-----------------------

``classification`` and ``signature`` are resolved from the strongest
*node-level* source a record carries, never from the lifecycle event name
that merely reports the incident. For one failed/blocked event the ladder
is, first match wins:

1. the deterministic-verification classifier's own output nested under
   ``payload["failure"]`` (its ``rule_id`` and 6-value ``classification``);
2. a structured, per-event-type cause: a kernel command rejection's
   ``receipt.error_code`` plus ``command.type``, a deliverable-floor
   ``reason`` (e.g. ``placeholder_token``), a bypassed verification gate
   slot, a backend transport's transport + return code;
3. a bounded table of stable cause phrases matched against the record's own
   free text (required-findings-escalated-without-discharge, "writable
   worker completed without changing the repository", recovery-limit
   exhaustion, a Claude CLI ``api_error_status`` / nonzero exit);
4. a ``review_fix_completed`` ``stop_reason``.

Artifacts contribute their own node-level sources: ``retry-budget-ledger/1``
``failure_keys`` and ``classification``, ``review-ledger/1`` finding keys,
categories and ``escalation_reason``, and ``plan-graph-block-escalation/1``
per-node ``classification`` and reason. ``signature`` carries the *coarse*
cause key so one cause clusters across runs; ``rule_id`` carries the most
specific stable identifier available (verification rule id, command
type + error code, finding key, failure keys) so the detail is not lost.
Both live inside the closed ``blocker-observation/1`` field set -- no field
is added for this.

Within-run dedup rule
---------------------

One incident used to mint an observation at every level that re-reported it
(the node's own ``review_fix_completed``, the coordinator's
``recovery_decision`` quoting it verbatim, and the graph's ``run_failed`` /
``plan_node_failed`` / ``plan_graph_completed``), so a single root cause
echoed across several patterns. Within one run, in this order:

* **(a) same cause** -- observations sharing a ``(signature, node_id)`` pair
  collapse to the earliest by event sequence.
* **(b) quoted echo** -- an observation with no recognized cause whose
  normalized signature strictly *contains* another observation's normalized
  signature is dropped as a restatement of that more specific one.
* **(c) lifecycle echo** -- an observation from a pure lifecycle event
  (``_LIFECYCLE_EVENT_TYPES``: events that report *that* a run ended, with
  no reason text of their own) is dropped when the run produced any
  cause-shaped or reason-bearing observation. If it produced none, the
  earliest lifecycle observation is kept, so a failing run is never
  represented by zero observations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from harness_labs.core.audit import AuditError
from harness_labs.observability.run_metrics import project_run_metrics


PROTOCOL = "blocker-observation/1"
STATE_PROTOCOL = "run-forensics-state/1"

#: Documentation default for production wiring (SI-05); ``mine()`` itself
#: takes explicit paths so tests never touch the real repository tree.
DEFAULT_STATE_DIR = Path("logs/improvement/state")
STATE_FILENAME = "run_forensics_watermark.json"

AGGREGATE_EVIDENCE_CLASSIFICATION = "production_lifecycle"

RESOLUTIONS = frozenset(
    {
        "self_recovered",
        "repair_attempt",
        "retry_renewed",
        "operator_intervention",
        "prompt_workaround",
        "transferred",
        "unresolved_blocked",
    }
)
CLASSIFICATIONS = frozenset(
    {
        "product",
        "indeterminate",
        "infrastructure_transient",
        "harness_or_configuration",
        "policy_violation",
        "structural_decision",
    }
)

_FAILED_OR_BLOCKED = frozenset({"failed", "blocked"})
_RETRY_BUDGET_EVENTS = frozenset({"abandoned", "extended"})

#: Events that report *that* a run or node ended, carrying no cause of their
#: own (their payloads are a terminal status, a node id, or an evidence
#: reference). An observation minted from one of these is a lifecycle echo
#: of whatever actually failed, and is dropped by dedup rule (c) whenever
#: the same run also produced a cause-shaped or reason-bearing observation.
#: Cap on a signature built from unrecognized free text. A gate command's
#: stderr can run to kilobytes; without a bound one such record would mint a
#: signature no other record could ever match, and would bloat every
#: downstream pattern file that quotes it.
MAX_REASON_SIGNATURE_LENGTH = 160

_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "run_failed",
        "plan_node_failed",
        "plan_graph_completed",
        "plan_graph_block_escalated",
        "plan_graph_node_failed",
        "node_completed",
    }
)

#: Finding categories a reviewer uses for behaviour defects; a finding in
#: one of these is a ``product`` blocker rather than an unclassifiable one.
#: Anything outside the set (style, docs, test hygiene) stays
#: ``indeterminate`` unless it is flagged ``contract_violation``.
_PRODUCT_FINDING_CATEGORIES = frozenset(
    {"correctness", "logic", "bug", "runtime", "behavior", "behaviour"}
)


# --------------------------------------------------------------------------
# Signature normalization
# --------------------------------------------------------------------------

_ABS_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_.\-]+){2,}")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
#: Any token shaped like this repository's run ids (``run-<slug>-<slug>...``),
#: not just the id of the run whose text is currently being normalized: a
#: run's free-text fields (e.g. an operator note) can quote a *different*
#: run's id, and that id must not survive into an aggregated signature.
_RUN_ID_RE = re.compile(r"\brun-[a-z0-9]+(?:-[a-z0-9]+){1,6}\b", re.IGNORECASE)
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{8,64}\b")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_signature_text(text: str, *, strip_literal: tuple[str, ...] = ()) -> str:
    """Strip absolute paths, ids, and timestamps out of free-form text.

    ``strip_literal`` additionally removes exact substrings the caller knows
    are run- or attempt-specific (e.g. the run id) even when they do not
    match the id/path heuristics below.
    """

    scrubbed = text
    for literal in strip_literal:
        if literal:
            scrubbed = scrubbed.replace(literal, "<id>")
    scrubbed = _UUID_RE.sub("<id>", scrubbed)
    scrubbed = _RUN_ID_RE.sub("<id>", scrubbed)
    scrubbed = _ABS_PATH_RE.sub("<path>", scrubbed)
    scrubbed = _TIMESTAMP_RE.sub("<ts>", scrubbed)
    scrubbed = _DATE_RE.sub("<ts>", scrubbed)
    scrubbed = _HEX_ID_RE.sub("<id>", scrubbed)
    return _WHITESPACE_RE.sub(" ", scrubbed).strip()


def _build_signature(run_id: str, *parts: str) -> str:
    normalized = [
        normalize_signature_text(str(part), strip_literal=(run_id,))
        for part in parts
        if part
    ]
    return ":".join(part for part in normalized if part)


# --------------------------------------------------------------------------
# Public data shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    run_dir: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"run_dir": self.run_dir, "reason": self.reason}


@dataclass(frozen=True)
class SkippedDir:
    """A directory under the runs root that was never a mining candidate.

    Surfaced so an empty or thin harvest is always explained: a runs root
    full of container directories (the PlanGraph nesting that DEFECT 1 was
    about) or of loose non-run directories reports *why* it produced
    nothing, rather than silently reporting zero.
    """

    path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class MiningResult:
    """The delta produced by one ``mine()`` call over a run-directory corpus."""

    observations: tuple[dict[str, Any], ...]
    excluded_run_ids: tuple[str, ...]
    refused: tuple[Refusal, ...]
    new_run_dirs: tuple[str, ...]
    skipped: tuple[SkippedDir, ...] = ()


# --------------------------------------------------------------------------
# Watermark state
# --------------------------------------------------------------------------


class WatermarkStateError(RuntimeError):
    """Raised when the watermark state file exists but cannot be trusted.

    A torn or corrupt state file must never be treated as "no state": that
    would silently re-mine the whole corpus and duplicate every observation
    downstream. It is surfaced instead, the same way a chain-verification
    failure is surfaced as a refusal rather than swallowed.
    """


def _state_path(state_root: Path) -> Path:
    return state_root / STATE_FILENAME


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_state(state_root: Path) -> dict[str, dict[str, Any]]:
    path = _state_path(state_root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WatermarkStateError(f"watermark state at {path} is unreadable or corrupt") from exc
    if not isinstance(payload, Mapping):
        raise WatermarkStateError(f"watermark state at {path} is not a JSON object")
    processed = payload.get("processed_run_dirs")
    if not isinstance(processed, Mapping):
        raise WatermarkStateError(f"watermark state at {path} has no processed_run_dirs object")
    return {
        str(key): dict(value)
        for key, value in processed.items()
        if isinstance(value, Mapping)
    }


def _save_state(state_root: Path, processed: Mapping[str, Mapping[str, Any]]) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocol": STATE_PROTOCOL,
        "processed_run_dirs": {
            key: dict(processed[key]) for key in sorted(processed)
        },
    }
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path = _state_path(state_root)
    descriptor, temporary_name = tempfile.mkstemp(dir=state_root, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(state_root)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


# --------------------------------------------------------------------------
# Run enumeration and admission
# --------------------------------------------------------------------------


#: A directory is "run-shaped" when it carries the audit journal every
#: chain verification starts from. Matching ``core.audit``'s own file name
#: keeps discovery honest: a directory without it cannot be verified, so it
#: cannot be a run.
RUN_JOURNAL_FILENAME = "events.jsonl"

#: How many levels of non-run-shaped container nesting ``_iter_run_dirs``
#: will descend below a top-level candidate before giving up on it. PlanGraph
#: nests one level for a plain campaign (``logs/runs/<graph-root>/<run-id>/``)
#: and two when an intermediate grouping directory sits in between
#: (``logs/runs/<graph-root>/feature-runs/<run-id>/``); the bound is set to
#: cover both known shapes explicitly, so a deep, unrelated tree under the
#: runs root can never turn discovery into an unbounded walk.
MAX_CONTAINER_DEPTH = 2


def _is_run_dir(path: Path) -> bool:
    return (path / RUN_JOURNAL_FILENAME).is_file()


def _candidate_children(directory: Path) -> tuple[list[Path], list[Path]]:
    """``(candidates, bookkeeping)`` subdirectories of ``directory``.

    Mirrors ``run_catalog.build_run_catalog``: symlinks are not followed and
    dotted names (``.plan-graph-budgets``, ``.plan-graph-locks``) are ledger
    and lock bookkeeping that lives beside runs, not runs. Bookkeeping is
    returned rather than dropped so it can be reported as skipped instead of
    vanishing.
    """

    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return [], []
    candidates: list[Path] = []
    bookkeeping: list[Path] = []
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        (bookkeeping if entry.name.startswith(".") else candidates).append(entry)
    return candidates, bookkeeping


def _iter_run_dirs(runs_root: Path) -> tuple[list[tuple[str, Path]], list[SkippedDir]]:
    """Discover ``(watermark_key, run_dir)`` pairs plus every skipped entry.

    A run-shaped directory keeps its full path relative to ``runs_root``
    (joined with ``/``) as its watermark key -- a bare directory name for a
    flat run, ``<container>/<run>`` for a run nested one container level
    down, ``<container>/<intermediate>/<run>`` for two, and so on -- so runs
    already watermarked by an earlier release stay watermarked and two
    containers holding same-named run directories never share a watermark
    entry. A directory that is *not* itself run-shaped is a candidate
    container and is recursed into, up to ``MAX_CONTAINER_DEPTH`` levels of
    such nesting; a container that yields no run-shaped descendant within
    that bound is reported once, at the point descent stopped, in
    ``skipped``.
    """

    if not runs_root.is_dir():
        return [], [SkippedDir(path=str(runs_root), reason="runs root is not a directory")]

    discovered: list[tuple[str, Path]] = []
    skipped: list[SkippedDir] = []

    def note_bookkeeping(entries: list[Path], prefix: str = "") -> None:
        for entry in entries:
            skipped.append(
                SkippedDir(
                    path=f"{prefix}{entry.name}",
                    reason="dotted bookkeeping directory beside runs, not a run",
                )
            )

    def walk(entry: Path, key: str, depth: int) -> bool:
        """Discover ``entry``; return whether it is or contains a run.

        ``depth`` counts levels of non-run-shaped container nesting already
        crossed to reach ``entry`` (a top-level candidate starts at 1).
        """

        if _is_run_dir(entry):
            discovered.append((key, entry))
            return True
        if depth > MAX_CONTAINER_DEPTH:
            skipped.append(
                SkippedDir(
                    path=key,
                    reason=(
                        f"nested directory holds no {RUN_JOURNAL_FILENAME}; container "
                        f"descent is bounded at depth {MAX_CONTAINER_DEPTH}"
                    ),
                )
            )
            return False
        children, bookkeeping = _candidate_children(entry)
        note_bookkeeping(bookkeeping, prefix=f"{key}/")
        if not children:
            skipped.append(
                SkippedDir(
                    path=key,
                    reason=(
                        f"directory holds no {RUN_JOURNAL_FILENAME} and no run-shaped "
                        "child directory"
                    ),
                )
            )
            return False
        found_any = False
        for child in children:
            if walk(child, f"{key}/{child.name}", depth + 1):
                found_any = True
        return found_any

    top_level, top_bookkeeping = _candidate_children(runs_root)
    note_bookkeeping(top_bookkeeping)
    for entry in top_level:
        walk(entry, entry.name, depth=1)
    return discovered, skipped


def _run_kind(run_dir: Path) -> str:
    descriptor_path = run_dir / "descriptor.json"
    if not descriptor_path.is_file():
        return "unknown"
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    if not isinstance(descriptor, Mapping):
        return "unknown"
    run_kind = descriptor.get("run_kind")
    return run_kind if isinstance(run_kind, str) and run_kind else "unknown"


# --------------------------------------------------------------------------
# Cause resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cause:
    """One resolved root cause: what to cluster on, and how to classify it.

    ``key`` is the coarse, cause-shaped signature (one key per cause, so one
    cause clusters across runs). ``rule_id`` is the most specific stable
    identifier the source offered, kept in the observation's existing
    ``rule_id`` field so the detail survives without widening the closed
    ``blocker-observation/1`` field set.
    """

    key: str
    classification: str | None = None
    rule_id: str | None = None


#: Stable cause phrases, checked in order against a record's own free text.
#: Each entry is (substring, cause key, classification). These are the
#: recurring, named failure modes the harness itself emits; matching on the
#: phrase rather than on the emitting event type is what lets a node's own
#: report and the coordinator's ``recovery_decision`` quoting it verbatim
#: resolve to the *same* cause key, which is what dedup rule (a) then
#: collapses.
_REASON_CAUSES: tuple[tuple[str, str, str], ...] = (
    (
        "required findings escalated without discharge",
        "required_findings_open",
        "policy_violation",
    ),
    (
        "writable worker completed without changing the repository",
        "worker_completed_without_change",
        "harness_or_configuration",
    ),
    ("recovery limit of", "recovery_limit_exhausted", "harness_or_configuration"),
    ("dirty base", "dirty_base_refused", "harness_or_configuration"),
    # Review/fix loop terminations. Keyed to match the ``stop_reason`` the
    # same loop writes (``review_fix:<stop_reason>``) so the node's own
    # structured report and any prose restatement of it collapse together.
    ("cycle limit reached", "review_fix:cycle_limit", "policy_violation"),
    ("fixer made no progress", "review_fix:no_progress", "policy_violation"),
    (
        "recovery proposal repeated an unchanged strategy",
        "recovery_strategy_unchanged",
        "harness_or_configuration",
    ),
    # Budget and join failures the PlanGraph layer names in prose; the node
    # id embedded in the text is what used to fragment these into one
    # pattern per node.
    ("has merge conflicts between", "plan_graph_join_merge_conflict", "harness_or_configuration"),
    ("budget exhausted for node", "retry_budget_exhausted", "harness_or_configuration"),
    (
        "declared verification command still fails after repair budget",
        "verification_unrepaired",
        "product",
    ),
)

#: A Claude CLI result envelope quoted into a reason: the API status is the
#: root cause, not the nonzero exit that reports it.
_API_ERROR_RE = re.compile(r'"api_error_status"\s*:\s*(\d{3})|API Error:\s*(\d{3})')
_CLI_EXIT_RE = re.compile(r"Claude exited with status (\d+)")
_ERROR_TYPE_RE = re.compile(r"['\"]error_type['\"]\s*:\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
_NON_KEY_RE = re.compile(r"[^a-z0-9]+")


def _cause_slug(text: str) -> str:
    """Fold free text into a bounded, lowercase, punctuation-free key part."""

    slug = _NON_KEY_RE.sub("_", normalize_signature_text(text).lower()).strip("_")
    return slug[:80]


def _cause_from_reason_text(text: str) -> _Cause | None:
    if not text:
        return None
    lowered = text.lower()
    for phrase, key, classification in _REASON_CAUSES:
        if phrase in lowered:
            return _Cause(key=key, classification=classification, rule_id=key)
    api_error = _API_ERROR_RE.search(text)
    if api_error:
        status = api_error.group(1) or api_error.group(2)
        return _Cause(
            key=f"claude_cli_api_error:{status}",
            classification="infrastructure_transient",
            rule_id=f"claude_cli_api_error:{status}",
        )
    exited = _CLI_EXIT_RE.search(text)
    if exited:
        return _Cause(
            key="claude_cli_exit_nonzero",
            classification="infrastructure_transient",
            rule_id=f"claude_cli_exit:{exited.group(1)}",
        )
    error_type = _ERROR_TYPE_RE.search(text)
    if error_type:
        slug = _cause_slug(error_type.group(1))
        return _Cause(key=f"worker_error:{slug}", classification=None, rule_id=f"worker_error:{slug}")
    return None


def _structured_cause(event_type: str, payload: Mapping[str, Any]) -> _Cause | None:
    """Per-event-type causes read out of structured payload fields."""

    if event_type == "command_rejected":
        receipt = payload.get("receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        command = payload.get("command")
        command = command if isinstance(command, Mapping) else {}
        error_code = receipt.get("error_code")
        error_code = error_code if isinstance(error_code, str) and error_code else "unspecified"
        command_type = command.get("type")
        command_type = command_type if isinstance(command_type, str) and command_type else "unspecified"
        return _Cause(
            key=f"command_rejected:{error_code}",
            classification="harness_or_configuration",
            rule_id=f"command_rejected:{command_type}:{error_code}",
        )
    if event_type == "deliverable_floor_refused":
        reason = payload.get("reason")
        reason = _cause_slug(reason) if isinstance(reason, str) and reason else "unspecified"
        field = payload.get("field")
        field = _cause_slug(field) if isinstance(field, str) and field else "unspecified"
        return _Cause(
            key=f"deliverable_floor:{reason}",
            classification="policy_violation",
            rule_id=f"deliverable_floor:{field}:{reason}",
        )
    if event_type == "plan_graph_gate_slot_bypassed":
        # A node with verification gates completed without ever entering the
        # graph-owned slot: the mutual-exclusion guarantee did not apply.
        return _Cause(
            key="gate_slot_bypassed",
            classification="harness_or_configuration",
            rule_id="gate_slot_bypassed",
        )
    if event_type in ("verified_command_completed", "functionality_test_completed"):
        # A declared gate command that exited nonzero. The command's own
        # tool is the cause-shaped part and the exit code is detail; the
        # command's stdout/stderr is never a signature, since it is
        # unbounded per-run prose. No classification is invented here: only
        # the verification classifier (rung 1) is entitled to say *why* a
        # gate command failed.
        argv = payload.get("argv")
        if not isinstance(argv, list):
            command = payload.get("command")
            argv = command.get("argv") if isinstance(command, Mapping) else None
        tool = "unspecified"
        if isinstance(argv, list) and argv and isinstance(argv[0], str) and argv[0]:
            tool = _cause_slug(PurePosixPath(argv[0]).name) or "unspecified"
            if tool in ("python3", "python") and len(argv) > 1 and isinstance(argv[1], str):
                # ``python3 -m pytest`` / ``python3 scripts/x.py``: the
                # interpreter is not the cause, what it ran is.
                target = argv[2] if argv[1] == "-m" and len(argv) > 2 else argv[1]
                if isinstance(target, str) and target:
                    tool = _cause_slug(PurePosixPath(target).name) or tool
        exit_code = payload.get("exit_code")
        suffix = f":exit_{exit_code}" if isinstance(exit_code, int) else ""
        prefix = (
            "verified_command_failed"
            if event_type == "verified_command_completed"
            else "functionality_test_failed"
        )
        return _Cause(
            key=f"{prefix}:{tool}",
            classification=None,
            rule_id=f"{prefix}:{tool}{suffix}",
        )
    if event_type == "backend_transport":
        transport = payload.get("transport")
        transport = _cause_slug(transport) if isinstance(transport, str) and transport else "unspecified"
        returncode = payload.get("returncode")
        suffix = f":exit_{returncode}" if isinstance(returncode, int) else ""
        return _Cause(
            key=f"backend_transport:{transport}",
            classification="infrastructure_transient",
            rule_id=f"backend_transport:{transport}{suffix}",
        )
    return None


def _event_cause_text(payload: Mapping[str, Any]) -> str:
    """Every free-text field on a payload that can carry a cause, joined.

    ``recovery_decision`` puts the node's own failure text in
    ``blocked_reason`` and restates it inside ``reason``; a rejected command
    puts it in ``receipt.message``; a live worker puts it in ``error``.
    """

    parts: list[str] = []
    for key in ("blocked_reason", "reason", "error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    receipt = payload.get("receipt")
    if isinstance(receipt, Mapping):
        message = receipt.get("message")
        if isinstance(message, str) and message:
            parts.append(message)
    controller_event = payload.get("controller_event")
    if isinstance(controller_event, Mapping):
        nested = controller_event.get("reason")
        if isinstance(nested, str) and nested:
            parts.append(nested)
    return "\n".join(parts)


def _event_primary_reason(payload: Mapping[str, Any]) -> str:
    """The single most specific free-text field on a payload.

    Used for the *signature* of a record whose cause could not be resolved.
    A coordinator payload restates the node's ``blocked_reason`` inside its
    own ``reason`` ("non-transient blocked at stage X; escalating with
    classified evidence: <blocked_reason>"), so signing on the join would
    mint a longer, run-shaped string that no other record can ever match.
    Signing on the innermost field instead lets dedup rule (b) see one
    record quoting another.
    """

    for key in ("blocked_reason", "reason", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    receipt = payload.get("receipt")
    if isinstance(receipt, Mapping):
        message = receipt.get("message")
        if isinstance(message, str) and message:
            return message
    controller_event = payload.get("controller_event")
    if isinstance(controller_event, Mapping):
        nested = controller_event.get("reason")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def _resolve_event_cause(event: Mapping[str, Any]) -> _Cause | None:
    """Strongest node-level cause for one failed/blocked event, or ``None``.

    The ladder is documented in the module docstring; the first rung that
    matches wins, so a deterministic-verification rule id always beats a
    phrase scraped out of prose.
    """

    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    event_type = str(event.get("event_type", ""))

    failure = payload.get("failure")
    if isinstance(failure, Mapping):
        rule_id = failure.get("rule_id")
        if isinstance(rule_id, str) and rule_id:
            classification = failure.get("classification")
            classification = classification if isinstance(classification, str) else None
            return _Cause(key=_cause_slug(rule_id), classification=classification, rule_id=rule_id)

    structured = _structured_cause(event_type, payload)
    if structured is not None:
        return structured

    from_text = _cause_from_reason_text(_event_cause_text(payload))
    if from_text is not None:
        return from_text

    if event_type == "review_fix_completed":
        stop_reason = payload.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            slug = _cause_slug(stop_reason)
            return _Cause(key=f"review_fix:{slug}", classification=None, rule_id=f"review_fix:{slug}")
    return None


# --------------------------------------------------------------------------
# Observation assembly
# --------------------------------------------------------------------------


def _resolution_cost() -> dict[str, Any]:
    return {
        "retries": 0,
        "repair_dispatches": 0,
        "wall_clock_ms": 0,
        "tokens": None,
        "diff_churn_lines": 0,
    }


def _make_observation(
    *,
    run_id: str,
    run_kind: str,
    evidence_classification: str,
    node_id: str | None,
    attempt_id: str | None,
    phase: str,
    classification: str,
    rule_id: str | None,
    signature: str,
    first_event_sequence: int,
    event_hashes: tuple[str, ...],
    resolution: str,
    artifact_refs: tuple[dict[str, str], ...],
) -> dict[str, Any]:
    if classification not in CLASSIFICATIONS:
        classification = "indeterminate"
    if resolution not in RESOLUTIONS:
        resolution = "unresolved_blocked"
    return {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "run_kind": run_kind,
        "evidence_classification": evidence_classification,
        "node_id": node_id,
        "attempt_id": attempt_id or run_id,
        "phase": phase,
        "classification": classification,
        "rule_id": rule_id,
        "signature": signature,
        "first_event_sequence": first_event_sequence,
        "event_hashes": list(event_hashes),
        "resolution": resolution,
        "resolution_cost": _resolution_cost(),
        "artifact_refs": [dict(ref) for ref in artifact_refs],
        "redaction_applied": True,
    }


def _is_retry_event(event: Mapping[str, Any]) -> bool:
    event_type = str(event.get("event_type", ""))
    if "retry" in event_type.lower():
        return True
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        controller_event = payload.get("controller_event")
        if isinstance(controller_event, Mapping):
            # The audit event's own outer ``event_type`` is the literal
            # "controller_event"; the kernel's retry/replan classification
            # lives one level down, on the *nested* KernelEvent, whose
            # ``as_dict()`` (controller_commands.py) serializes it under
            # "event_type" -- not "type".
            return str(controller_event.get("event_type", "")) == "retry.request"
    return False


def _event_reason_text(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    reason = payload.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    controller_event = payload.get("controller_event")
    if isinstance(controller_event, Mapping):
        nested_reason = controller_event.get("reason")
        if isinstance(nested_reason, str):
            return nested_reason
    return ""


def _event_node_id(event: Mapping[str, Any]) -> str | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        node_id = payload.get("node_id")
        if isinstance(node_id, str) and node_id:
            return node_id
    return None


#: Mining-time provenance for one observation, used only by the within-run
#: dedup pass and never emitted (``blocker-observation/1`` is closed).
_CAUSE_SHAPED = "cause"
_REASON_SHAPED = "reason"
_LIFECYCLE_SHAPED = "lifecycle"


def _mine_events(
    run_id: str, run_kind: str, evidence_classification: str, events: list[Any]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Mine one run's events into ``(shape, source, observation)`` triples.

    ``shape`` records which rung of the cause ladder produced the record and
    ``source`` names the event type (or artifact protocol) it came from, so
    :func:`_dedup_run_observations` can tell one level restating another's
    incident from the same level genuinely hitting the same cause twice.
    Neither leaves the miner.
    """

    tagged: list[tuple[str, str, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        sequence = event.get("sequence")
        event_hash = event.get("event_hash")
        event_hashes = (event_hash,) if isinstance(event_hash, str) else ()
        attempt_id = event.get("attempt_id")
        attempt_id = attempt_id if isinstance(attempt_id, str) else None
        node_id = _event_node_id(event)
        reason_text = _event_reason_text(event)

        if _is_retry_event(event):
            retry_cause = _cause_from_reason_text(reason_text)
            tagged.append(
                (
                    _CAUSE_SHAPED if retry_cause is not None else _REASON_SHAPED,
                    "retry.request",
                    _make_observation(
                        run_id=run_id,
                        run_kind=run_kind,
                        evidence_classification=evidence_classification,
                        node_id=node_id,
                        attempt_id=attempt_id,
                        phase="retry",
                        classification=(retry_cause.classification if retry_cause else None) or "indeterminate",
                        rule_id=retry_cause.rule_id if retry_cause else None,
                        signature=_build_signature(
                            run_id, "retry", retry_cause.key if retry_cause else reason_text
                        ),
                        first_event_sequence=sequence if isinstance(sequence, int) else 0,
                        event_hashes=event_hashes,
                        resolution="retry_renewed",
                        artifact_refs=(),
                    ),
                )
            )

        status = event.get("status")
        if status not in _FAILED_OR_BLOCKED:
            continue

        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        event_type = str(event.get("event_type", ""))
        cause = _resolve_event_cause(event)

        # Classification precedence: the cause's own source first (a
        # verification classifier's verdict, a structured cause's known
        # nature), then whatever the payload itself declared, then nothing.
        classification = cause.classification if cause is not None else None
        if classification not in CLASSIFICATIONS:
            candidate = payload.get("classification")
            classification = candidate if isinstance(candidate, str) else None
        if classification not in CLASSIFICATIONS:
            classification = "indeterminate"

        cause_text = _event_primary_reason(payload) or reason_text
        rule_id = None
        if cause is not None:
            shape = _CAUSE_SHAPED
            signature = _build_signature(run_id, cause.key)
            rule_id = cause.rule_id
        else:
            # No recognized cause. A record that still carries its own free
            # text is reason-shaped -- it says *something* about why -- and
            # its signature is that text alone, with no event-name prefix,
            # so dedup rule (b) can see one record quoting another. A record
            # with no text at all is a bare lifecycle report.
            signature = (
                _build_signature(run_id, cause_text)[:MAX_REASON_SIGNATURE_LENGTH].strip()
                if cause_text
                else ""
            )
            if signature:
                shape = _REASON_SHAPED
            else:
                shape = _LIFECYCLE_SHAPED if event_type in _LIFECYCLE_EVENT_TYPES else _REASON_SHAPED
                signature = _build_signature(run_id, str(status), event_type)
        if not signature:
            shape = _LIFECYCLE_SHAPED
            signature = str(status)

        tagged.append(
            (
                shape,
                event_type or str(status),
                _make_observation(
                    run_id=run_id,
                    run_kind=run_kind,
                    evidence_classification=evidence_classification,
                    node_id=node_id,
                    attempt_id=attempt_id,
                    phase=str(status),
                    classification=classification,
                    rule_id=rule_id,
                    signature=signature,
                    first_event_sequence=sequence if isinstance(sequence, int) else 0,
                    event_hashes=event_hashes,
                    resolution="unresolved_blocked",
                    artifact_refs=(),
                ),
            )
        )
    return tagged


def _dedup_run_observations(
    tagged: list[tuple[str, str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Collapse one run's echoes of the same incident. See module docstring.

    (a) cross-level echo -- a ``(signature, node_id)`` pair first minted by
        one source (event type or artifact protocol) and then restated by a
        *different* source is kept only once. Two records of the *same*
        source hitting the same cause are genuine repeats -- 37 separate
        command rejections in one campaign are 37 incidents, not one -- and
        both survive.
    (b) quoted echo -- a reason-shaped observation whose signature strictly
        contains a kept observation's signature is a restatement of it.
    (c) lifecycle echo -- lifecycle-shaped observations survive only when
        the run produced nothing else, and then only the earliest one.
    """

    ordered = sorted(
        tagged, key=lambda item: (item[2]["first_event_sequence"], item[2]["signature"])
    )

    # (a)
    first_source: dict[tuple[str, str | None], str] = {}
    unique: list[tuple[str, dict[str, Any]]] = []
    for shape, source, observation in ordered:
        key = (observation["signature"], observation["node_id"])
        owner = first_source.setdefault(key, source)
        if owner != source:
            continue
        unique.append((shape, observation))

    substantive = [item for item in unique if item[0] != _LIFECYCLE_SHAPED]

    # (b) -- only reason-shaped records restate; a resolved cause key is
    # already minimal and two distinct causes must never absorb each other.
    signatures = [observation["signature"] for _, observation in substantive]
    kept: list[dict[str, Any]] = []
    for shape, observation in substantive:
        signature = observation["signature"]
        if shape == _REASON_SHAPED and any(
            other != signature and other in signature for other in signatures
        ):
            continue
        kept.append(observation)

    # (c)
    if kept:
        return kept
    lifecycle = [observation for shape, observation in unique if shape == _LIFECYCLE_SHAPED]
    return lifecycle[:1]


def _load_json_artifact(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mine_review_ledger_artifact(
    run_id: str, run_kind: str, evidence_classification: str, run_dir: Path, artifact_path: Path, content: Mapping[str, Any]
) -> list[dict[str, Any]]:
    findings = content.get("findings")
    if not isinstance(findings, Mapping):
        return []
    artifact_ref = {
        "path": str(artifact_path.relative_to(run_dir)),
        "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    observations = []
    for key in sorted(findings):
        finding = findings[key]
        if not isinstance(finding, Mapping):
            continue
        reopened_count = finding.get("reopened_count")
        reopened = isinstance(reopened_count, int) and reopened_count > 0
        outcome = finding.get("outcome")
        escalated = outcome == "escalated"
        if not reopened and not escalated:
            continue
        category = str(finding.get("category", "")) or "unspecified"
        severity = str(finding.get("severity", "")) or "unspecified"
        # A review finding classifies itself: a declared contract violation
        # is a policy violation, and a behaviour-defect category is a
        # product blocker. Anything else stays indeterminate rather than
        # being guessed at from prose.
        if bool(finding.get("contract_violation")):
            classification = "policy_violation"
        elif category.lower() in _PRODUCT_FINDING_CATEGORIES:
            classification = "product"
        else:
            classification = "indeterminate"
        escalation_reason = finding.get("escalation_reason")
        escalation_reason = escalation_reason if isinstance(escalation_reason, str) else ""
        if escalated:
            phase = "review_escalated"
            reason_cause = _cause_from_reason_text(escalation_reason)
            signature = _build_signature(
                run_id,
                reason_cause.key
                if reason_cause
                else f"review_escalated:{_cause_slug(escalation_reason) or _cause_slug(category)}",
            )
            resolution = "unresolved_blocked"
        else:
            phase = "review_reopened"
            signature = _build_signature(run_id, f"review_reopened:{_cause_slug(category)}:{severity}")
            resolution = "repair_attempt" if outcome == "fixed" else "unresolved_blocked"
        observations.append(
            _make_observation(
                run_id=run_id,
                run_kind=run_kind,
                evidence_classification=evidence_classification,
                node_id=str(finding.get("origin_node") or "") or None,
                attempt_id=None,
                phase=phase,
                classification=classification,
                # The finding key is the stable, cause-shaped identifier the
                # review ledger already mints; carried as rule_id so the
                # coarse signature can cluster across runs without losing it.
                rule_id=normalize_signature_text(str(finding.get("key", key))) or None,
                signature=signature,
                first_event_sequence=0,
                event_hashes=(artifact_ref["sha256"],),
                resolution=resolution,
                artifact_refs=(artifact_ref,),
            )
        )
    return observations


def _mine_block_escalation_artifact(
    run_id: str,
    run_kind: str,
    evidence_classification: str,
    run_dir: Path,
    artifact_path: Path,
    content: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Mine ``plan-graph-block-escalation/1``: the node-level record of why a
    graph stopped.

    Its ``nodes[]`` entries carry the 6-value ``classification`` enum
    directly, which is a far stronger source than the graph-level
    ``plan_graph_block_escalated`` lifecycle event that merely points at
    this artifact.
    """

    nodes = content.get("nodes")
    if not isinstance(nodes, list):
        return []
    artifact_ref = {
        "path": str(artifact_path.relative_to(run_dir)),
        "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    observations: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        status = node.get("status")
        if status not in _FAILED_OR_BLOCKED:
            continue
        node_id = node.get("node_id")
        node_id = node_id if isinstance(node_id, str) and node_id else None
        reason = node.get("reason")
        reason = reason if isinstance(reason, str) else ""
        cause = _cause_from_reason_text(reason)
        classification = node.get("classification")
        if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
            classification = (cause.classification if cause else None) or "indeterminate"
        # A resolved cause key is emitted bare so an escalation record and a
        # node event naming the same cause land in one pattern; only the
        # unresolved fallback keeps the ``block_escalated:`` qualifier.
        signature_part = cause.key if cause else f"block_escalated:{_cause_slug(reason) or _cause_slug(str(status))}"
        observations.append(
            _make_observation(
                run_id=run_id,
                run_kind=run_kind,
                evidence_classification=evidence_classification,
                node_id=node_id,
                attempt_id=None,
                phase="block_escalated",
                classification=classification,
                rule_id=cause.rule_id if cause else None,
                signature=_build_signature(run_id, signature_part),
                first_event_sequence=0,
                event_hashes=(artifact_ref["sha256"],),
                resolution="unresolved_blocked",
                artifact_refs=(artifact_ref,),
            )
        )

    escalations = content.get("escalations")
    if isinstance(escalations, list):
        for escalation in escalations:
            if not isinstance(escalation, Mapping):
                continue
            finding_key = escalation.get("finding_key")
            if not isinstance(finding_key, str) or not finding_key:
                continue
            owner = escalation.get("owner_node")
            observations.append(
                _make_observation(
                    run_id=run_id,
                    run_kind=run_kind,
                    evidence_classification=evidence_classification,
                    node_id=owner if isinstance(owner, str) and owner else None,
                    attempt_id=None,
                    phase="finding_escalated",
                    classification="policy_violation",
                    rule_id=normalize_signature_text(finding_key) or None,
                    signature=_build_signature(
                        run_id, f"finding_escalated:{_cause_slug(finding_key)}"
                    ),
                    first_event_sequence=0,
                    event_hashes=(artifact_ref["sha256"],),
                    resolution="unresolved_blocked",
                    artifact_refs=(artifact_ref,),
                )
            )
    return observations


def _mine_retry_budget_artifact(
    run_id: str, run_kind: str, evidence_classification: str, run_dir: Path, artifact_path: Path, content: Mapping[str, Any]
) -> list[dict[str, Any]]:
    event = content.get("event")
    if event not in _RETRY_BUDGET_EVENTS:
        return []
    artifact_ref = {
        "path": str(artifact_path.relative_to(run_dir)),
        "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    reason = str(content.get("reason", ""))
    reason_cause = _cause_from_reason_text(reason)
    classification = content.get("classification")
    if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
        classification = (reason_cause.classification if reason_cause else None) or "indeterminate"
    node_id = content.get("node_id")
    node_id = node_id if isinstance(node_id, str) and node_id else None
    failure_keys = content.get("failure_keys")
    failure_keys = tuple(sorted(str(item) for item in failure_keys)) if isinstance(failure_keys, list) else ()
    resolution = "retry_renewed" if event == "extended" else "unresolved_blocked"
    # The ledger's own ``failure_keys`` are the stable cause identifiers the
    # budget machinery already agreed on; prefer them over the free-text
    # reason and over the classification word, which is a verdict, not a
    # cause. ``rule_id`` keeps the full key set.
    if failure_keys:
        detail = ",".join(failure_keys)
    elif reason_cause is not None:
        detail = reason_cause.key
    else:
        detail = _cause_slug(reason) or "unspecified"
    return [
        _make_observation(
            run_id=run_id,
            run_kind=run_kind,
            evidence_classification=evidence_classification,
            node_id=node_id,
            attempt_id=None,
            phase=f"retry_budget_{event}",
            classification=classification,
            rule_id=detail if failure_keys else (reason_cause.rule_id if reason_cause else None),
            signature=_build_signature(run_id, f"retry_budget_{event}:{detail}"),
            first_event_sequence=0,
            event_hashes=(artifact_ref["sha256"],),
            resolution=resolution,
            artifact_refs=(artifact_ref,),
        )
    ]


def _authenticated_artifacts(events: list[Any]) -> dict[str, str]:
    """Map each artifact path a chain-verified event actually declares to its
    hash-verified sha256.

    ``_verify_event_journal`` (core/audit.py) hash-checks every artifact
    named in every event's own ``artifacts`` list, unconditionally, whether
    or not the run is terminal or has a manifest. A file that merely sits in
    ``artifacts/`` without any event declaring it was never part of that
    check and is unauthenticated bytes, not evidence.
    """

    authenticated: dict[str, str] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        declared = event.get("artifacts")
        if not isinstance(declared, list):
            continue
        for artifact in declared:
            if not isinstance(artifact, Mapping):
                continue
            path = artifact.get("path")
            sha256 = artifact.get("sha256")
            if isinstance(path, str) and isinstance(sha256, str):
                authenticated[path] = sha256
    return authenticated


def _mine_artifacts(
    run_id: str,
    run_kind: str,
    evidence_classification: str,
    run_dir: Path,
    authenticated_artifacts: Mapping[str, str],
) -> list[tuple[str, dict[str, Any]]]:
    """``(source, observation)`` pairs, source being ``artifact:<protocol>``."""

    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return []
    observations: list[tuple[str, dict[str, Any]]] = []
    for artifact_path in sorted(artifacts_dir.glob("*.json")):
        relative_path = str(artifact_path.relative_to(run_dir))
        expected_sha256 = authenticated_artifacts.get(relative_path)
        if expected_sha256 is None:
            continue
        if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != expected_sha256:
            continue
        content = _load_json_artifact(artifact_path)
        if not isinstance(content, Mapping):
            continue
        protocol = content.get("protocol")
        if protocol == "review-ledger/1":
            minted = _mine_review_ledger_artifact(
                run_id, run_kind, evidence_classification, run_dir, artifact_path, content
            )
        elif protocol == "retry-budget-ledger/1":
            minted = _mine_retry_budget_artifact(
                run_id, run_kind, evidence_classification, run_dir, artifact_path, content
            )
        elif protocol == "plan-graph-block-escalation/1":
            minted = _mine_block_escalation_artifact(
                run_id, run_kind, evidence_classification, run_dir, artifact_path, content
            )
        else:
            continue
        source = f"artifact:{protocol}"
        observations.extend((source, observation) for observation in minted)
    return observations


def _mine_run(run_dir: Path, metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    run_id = str(metrics["run_id"])
    evidence_classification = str(metrics["evidence_classification"])
    run_kind = _run_kind(run_dir)
    events = metrics.get("events")
    events = events if isinstance(events, list) else []
    authenticated_artifacts = _authenticated_artifacts(events)
    artifact_observations = _mine_artifacts(
        run_id, run_kind, evidence_classification, run_dir, authenticated_artifacts
    )
    # Artifact-derived records are always cause-shaped: they come from a
    # node-level ledger or escalation, never from a lifecycle event, so they
    # participate in dedup rule (a) but can never be dropped as an echo.
    tagged = _mine_events(run_id, run_kind, evidence_classification, events)
    tagged.extend((_CAUSE_SHAPED, source, observation) for source, observation in artifact_observations)
    observations = _dedup_run_observations(tagged)
    observations.sort(
        key=lambda observation: (
            observation["first_event_sequence"],
            observation["phase"],
            observation["signature"],
        )
    )
    return observations


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def mine(runs_root: Path, *, state_root: Path) -> MiningResult:
    """Mine every run directory under ``runs_root`` whose verified chain head
    has advanced past its last watermark (or that has none yet).

    Read-only over ``runs_root``; the only writes are the watermark file
    under ``state_root``. A run whose audit chain fails to verify is
    reported in ``refused`` and never contributes an observation; a refusal
    is never watermarked, so a run caught mid-append (or genuinely
    corrupted) is retried on every call rather than blacklisted once it
    happens to complete cleanly. A run that verifies but is not
    ``production_lifecycle`` is parsed (its id lands in ``excluded_run_ids``)
    but contributes nothing to ``observations``. The watermark itself is
    keyed on each run's verified head hash and event count, not on
    directory name alone, so a non-terminal run whose journal has grown
    since it was last mined is revisited rather than sealed forever.
    """

    processed = _load_state(state_root)
    new_observations: list[dict[str, Any]] = []
    new_excluded: list[str] = []
    new_refused: list[Refusal] = []
    new_run_dirs: list[str] = []

    discovered, skipped = _iter_run_dirs(runs_root)
    for name, run_dir in discovered:
        try:
            metrics = project_run_metrics(run_dir)
        except AuditError as exc:
            new_run_dirs.append(name)
            new_refused.append(Refusal(run_dir=name, reason=str(exc)))
            continue

        head_hash = metrics["checkpoint"].get("head_hash")
        event_count = metrics["event_count"]
        watermark = processed.get(name)
        if (
            watermark is not None
            and watermark.get("head_hash") == head_hash
            and watermark.get("event_count") == event_count
        ):
            continue

        new_run_dirs.append(name)
        evidence_classification = str(metrics["evidence_classification"])
        if evidence_classification == AGGREGATE_EVIDENCE_CLASSIFICATION:
            observations = _mine_run(run_dir, metrics)
            new_observations.extend(observations)
            processed[name] = {
                "outcome": "mined",
                "run_id": str(metrics["run_id"]),
                "head_hash": head_hash,
                "event_count": event_count,
                "observation_count": len(observations),
            }
        else:
            new_excluded.append(str(metrics["run_id"]))
            processed[name] = {
                "outcome": "excluded",
                "run_id": str(metrics["run_id"]),
                "evidence_classification": evidence_classification,
                "head_hash": head_hash,
                "event_count": event_count,
            }

    _save_state(state_root, processed)

    new_observations.sort(
        key=lambda observation: (
            observation["run_id"],
            observation["first_event_sequence"],
            observation["phase"],
            observation["signature"],
        )
    )
    return MiningResult(
        observations=tuple(new_observations),
        excluded_run_ids=tuple(sorted(new_excluded)),
        refused=tuple(sorted(new_refused, key=lambda refusal: refusal.run_dir)),
        new_run_dirs=tuple(sorted(new_run_dirs)),
        skipped=tuple(sorted(skipped, key=lambda entry: entry.path)),
    )
