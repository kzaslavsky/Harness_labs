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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
class MiningResult:
    """The delta produced by one ``mine()`` call over a run-directory corpus."""

    observations: tuple[dict[str, Any], ...]
    excluded_run_ids: tuple[str, ...]
    refused: tuple[Refusal, ...]
    new_run_dirs: tuple[str, ...]


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


def _iter_run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.is_dir():
        return []
    return sorted(
        (entry for entry in runs_root.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
    )


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


def _mine_events(run_id: str, run_kind: str, evidence_classification: str, events: list[Any]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
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
            observations.append(
                _make_observation(
                    run_id=run_id,
                    run_kind=run_kind,
                    evidence_classification=evidence_classification,
                    node_id=node_id,
                    attempt_id=attempt_id,
                    phase="retry",
                    classification="indeterminate",
                    rule_id=None,
                    signature=_build_signature(run_id, "retry", reason_text),
                    first_event_sequence=sequence if isinstance(sequence, int) else 0,
                    event_hashes=event_hashes,
                    resolution="retry_renewed",
                    artifact_refs=(),
                )
            )

        status = event.get("status")
        if status in _FAILED_OR_BLOCKED:
            payload = event.get("payload")
            classification = "indeterminate"
            rule_id = None
            if isinstance(payload, Mapping):
                candidate = payload.get("classification")
                if isinstance(candidate, str) and candidate in CLASSIFICATIONS:
                    classification = candidate
                # A deterministic-verification failure's stable classifier
                # output (feature_run.classify_verification_failure) lives
                # nested under payload["failure"], not at the payload's top
                # level; prefer it over the top-level fields it supersedes.
                failure = payload.get("failure")
                if isinstance(failure, Mapping):
                    failure_classification = failure.get("classification")
                    if isinstance(failure_classification, str) and failure_classification in CLASSIFICATIONS:
                        classification = failure_classification
                    failure_rule_id = failure.get("rule_id")
                    if isinstance(failure_rule_id, str) and failure_rule_id:
                        rule_id = failure_rule_id
            if rule_id:
                signature = _build_signature(run_id, str(status), rule_id)
            else:
                signature = _build_signature(run_id, str(status), str(event.get("event_type", "")), reason_text)
            observations.append(
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
                )
            )
    return observations


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
        if not isinstance(reopened_count, int) or reopened_count <= 0:
            continue
        outcome = finding.get("outcome")
        resolution = "repair_attempt" if outcome == "fixed" else "unresolved_blocked"
        observations.append(
            _make_observation(
                run_id=run_id,
                run_kind=run_kind,
                evidence_classification=evidence_classification,
                node_id=None,
                attempt_id=None,
                phase="review_reopened",
                classification="indeterminate",
                rule_id=None,
                signature=_build_signature(
                    run_id,
                    "review_reopened",
                    str(finding.get("category", "")),
                    str(finding.get("severity", "")),
                    str(finding.get("subject", "")),
                ),
                first_event_sequence=0,
                event_hashes=(artifact_ref["sha256"],),
                resolution=resolution,
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
    classification = content.get("classification")
    if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
        classification = "indeterminate"
    node_id = content.get("node_id")
    node_id = node_id if isinstance(node_id, str) and node_id else None
    failure_keys = content.get("failure_keys")
    failure_keys = tuple(sorted(str(item) for item in failure_keys)) if isinstance(failure_keys, list) else ()
    resolution = "retry_renewed" if event == "extended" else "unresolved_blocked"
    return [
        _make_observation(
            run_id=run_id,
            run_kind=run_kind,
            evidence_classification=evidence_classification,
            node_id=node_id,
            attempt_id=None,
            phase=f"retry_budget_{event}",
            classification=classification,
            rule_id=None,
            signature=_build_signature(
                run_id,
                f"retry_budget_{event}",
                classification,
                str(content.get("reason", "")),
                *failure_keys,
            ),
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
) -> list[dict[str, Any]]:
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return []
    observations: list[dict[str, Any]] = []
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
            observations.extend(
                _mine_review_ledger_artifact(run_id, run_kind, evidence_classification, run_dir, artifact_path, content)
            )
        elif protocol == "retry-budget-ledger/1":
            observations.extend(
                _mine_retry_budget_artifact(run_id, run_kind, evidence_classification, run_dir, artifact_path, content)
            )
    return observations


def _mine_run(run_dir: Path, metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    run_id = str(metrics["run_id"])
    evidence_classification = str(metrics["evidence_classification"])
    run_kind = _run_kind(run_dir)
    events = metrics.get("events")
    events = events if isinstance(events, list) else []
    observations = _mine_events(run_id, run_kind, evidence_classification, events)
    authenticated_artifacts = _authenticated_artifacts(events)
    observations.extend(
        _mine_artifacts(run_id, run_kind, evidence_classification, run_dir, authenticated_artifacts)
    )
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

    for run_dir in _iter_run_dirs(runs_root):
        name = run_dir.name
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
    )
