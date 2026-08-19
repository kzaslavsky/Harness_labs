#!/usr/bin/env python3
"""Watch one PlanGraph attempt lineage and relaunch its successors, bounded.

An operator who leaves a campaign running overnight wants exactly three
guarantees, and this driver exists to provide them without knowing anything
about which campaign it is watching:

* it never starts a successor beside a predecessor whose processes are still
  alive (:class:`QuiescenceMonitor`);
* the successor retries every node the predecessor actually terminalized, not
  only the one the escalation template happens to name
  (:func:`reconcile_frontier`);
* it stops relaunching once the campaign stops making progress, instead of
  burning budget on an identical escalation forever
  (:class:`NoProgressGuard`).

Everything else -- run root, the command that launches an attempt, poll
interval, ceilings -- is an input.  This program deliberately does not import
``PlanGraph.resume``: like ``plan_graph_recover.py`` it starts a fresh
top-level process and reports that process's own terminal result rather than
manufacturing one.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The lineage's own process-identity probe, reused rather than reimplemented so
# this driver's notion of "still running" is byte-identical to the one
# ``reclaim_orphaned_successor_attempt`` applies to the same marker files.
from harness_labs.plangraph.plan_graph_audit import _process_start_token


_ESCALATION_PROTOCOL = "plan-graph-block-escalation/1"
_ADMISSION_LIVENESS_NAME = "plan-graph-admission-liveness.json"
_ADMISSION_LIVENESS_PROTOCOL = "harness-plan-graph-admission-liveness/1"
# ``liveness.json`` was once accepted here as an alias.  It no longer is:
# that filename is the controller liveness lease
# (``harness-controller-liveness/1``, written by
# ``core.controller_liveness`` for every running FeatureRun), and both
# readers of this tuple refuse to look when more than one candidate name is
# present.  Leaving the alias in place would mean a child that wrote a real
# ``plan-graph-liveness.json`` beside its own controller lease became
# unobservable -- two names present, so neither is read.  One filename, one
# protocol.
_CHILD_LIVENESS_NAMES = ("plan-graph-liveness.json",)
_CHILD_LIVENESS_PROTOCOL = "harness-plan-graph-parallel-liveness/1"
_TERMINAL_NODE_STATUSES = frozenset({"failed", "blocked"})
_RESUMABLE_ATTEMPT_STATUSES = frozenset({"failed", "blocked"})

#: Exit codes.  ``0``/``1`` follow ``plan_graph_recover.py``; ``2`` and ``3``
#: are the two distinct "stop and fetch a human" outcomes an unattended driver
#: has to be able to signal apart from an ordinary failure.
EXIT_SUCCEEDED = 0
EXIT_BLOCKED = 1
EXIT_NO_PROGRESS = 2
EXIT_CEILING = 3


class AutoresumeError(ValueError):
    """The lineage cannot safely be resumed by this driver."""


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_events(path: Path) -> tuple[dict[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    events: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A partially flushed final line is normal while an attempt is
            # still writing; the records before it are still trustworthy.
            break
        if isinstance(event, dict):
            events.append(event)
    return tuple(events)


# ---------------------------------------------------------------------------
# Lineage discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """One attempt directory under the run root, as far as it can be read."""

    attempt_id: str
    directory: Path
    descriptor: Mapping[str, object] | None
    manifest: Mapping[str, object] | None

    @property
    def finalized(self) -> bool:
        return self.manifest is not None

    @property
    def status(self) -> str | None:
        return self.manifest.get("status") if isinstance(self.manifest, Mapping) else None

    @property
    def predecessor_attempt_id(self) -> str | None:
        if not isinstance(self.descriptor, Mapping):
            return None
        value = self.descriptor.get("predecessor_attempt_id")
        return value if isinstance(value, str) and value else None


def scan_attempts(run_root: Path) -> dict[str, Attempt]:
    """Read every attempt directory under ``run_root``.

    Dot-prefixed entries are the lineage's lock and budget stores, which
    ``PlanGraph`` keeps beside the attempts and the run catalog likewise
    excludes.
    """
    attempts: dict[str, Attempt] = {}
    try:
        entries = sorted(run_root.iterdir())
    except OSError as exc:
        raise AutoresumeError(f"run root is unreadable: {run_root}") from exc
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        attempts[entry.name] = Attempt(
            entry.name, entry,
            _read_json(entry / "descriptor.json"),
            _read_json(entry / "manifest.json"),
        )
    return attempts


def lineage_attempts(attempts: Mapping[str, Attempt], seed_attempt_id: str) -> tuple[Attempt, ...]:
    """Return the seed attempt and every attempt descended from it.

    Descent is read from each attempt's own ``descriptor.json``
    ``predecessor_attempt_id`` -- the same durable binding
    ``open_repair_predecessor`` checks -- rather than from a naming
    convention.  A driver that matched attempt directory names against a
    regex would be campaign-specific by construction.
    """
    if seed_attempt_id not in attempts:
        raise AutoresumeError(f"no attempt directory named {seed_attempt_id!r} under the run root")
    lineage = {seed_attempt_id: attempts[seed_attempt_id]}
    while True:
        discovered = {
            attempt_id: attempt
            for attempt_id, attempt in attempts.items()
            if attempt_id not in lineage and attempt.predecessor_attempt_id in lineage
        }
        if not discovered:
            return tuple(lineage.values())
        lineage.update(discovered)


@dataclass(frozen=True)
class Predecessor:
    """A finalized, resumable lineage leaf plus the artifacts describing it."""

    attempt_id: str
    directory: Path
    status: str
    escalation: Mapping[str, object]
    blocker_evidence_ref: str
    events: tuple[Mapping[str, object], ...]


def find_predecessor(run_root: Path, seed_attempt_id: str) -> Predecessor:
    """Select the finalized lineage leaf a successor may legally open against.

    Three conditions, all of them the library's, not this driver's: the
    attempt must be finalized (``manifest.json`` exists) with status
    ``failed``/``blocked`` -- ``PlanGraphAudit.open_repair_predecessor``
    refuses anything else -- it must carry an escalation, and nothing may
    already descend from it.  Resuming an attempt that already has a
    successor would fork the lineage.
    """
    lineage = lineage_attempts(scan_attempts(run_root), seed_attempt_id)
    superseded = {
        attempt.predecessor_attempt_id for attempt in lineage
        if attempt.predecessor_attempt_id is not None
    }
    leaves = [attempt for attempt in lineage if attempt.attempt_id not in superseded]
    resumable = [
        attempt for attempt in leaves
        if attempt.finalized and attempt.status in _RESUMABLE_ATTEMPT_STATUSES
    ]
    if not resumable:
        finalized_leaf = next((leaf for leaf in leaves if leaf.finalized), None)
        if finalized_leaf is not None:
            raise AutoresumeError(
                f"lineage leaf {finalized_leaf.attempt_id!r} finalized as "
                f"{finalized_leaf.status!r}, which is not resumable"
            )
        raise AutoresumeError("no finalized lineage leaf is available to resume")
    if len(resumable) > 1:
        raise AutoresumeError(
            "lineage has forked: "
            + ", ".join(sorted(attempt.attempt_id for attempt in resumable))
        )
    attempt = resumable[0]
    escalation_path = attempt.directory / "escalation.json"
    escalation = _read_json(escalation_path)
    if escalation is None or escalation.get("protocol") != _ESCALATION_PROTOCOL:
        raise AutoresumeError(
            f"attempt {attempt.attempt_id!r} has no readable block escalation"
        )
    # ``record_block_escalation`` returns exactly this reference and
    # ``PlanGraph.resume`` re-derives it from the same bytes, so hashing the
    # artifact on disk is the whole of the blocker-evidence contract.
    reference = "artifact:sha256:" + hashlib.sha256(escalation_path.read_bytes()).hexdigest()
    return Predecessor(
        attempt.attempt_id, attempt.directory, str(attempt.status), escalation, reference,
        _read_events(attempt.directory / "events.jsonl"),
    )


# ---------------------------------------------------------------------------
# Frontier reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontierReconciliation:
    """The declared retry frontier checked against what the attempt recorded.

    ``escalation.json``'s ``resume_directive_template.retry_frontier`` is a
    published contract, but with ``continue_independent_after_block`` off --
    the default -- it is a single element naming only the primary blocker,
    and a drained attempt can finish with more than one terminal node.  In
    that case the template under-reports and a successor built from it alone
    re-blocks immediately on the node it left behind.  See
    ``docs/development/plan-graph-sibling-independent-node-relaunch.md``.

    Neither source is silently preferred.  The template's order is kept
    (the primary blocker stays first, as the contract promises), event-derived
    terminal nodes it omits are appended, and both directions of disagreement
    are reported so an operator sees which one drifted.
    """

    template: tuple[str, ...]
    observed: tuple[str, ...]
    frontier: tuple[str, ...]
    missing_from_template: tuple[str, ...]
    missing_from_events: tuple[str, ...]
    recovered: tuple[str, ...]

    @property
    def discrepancies(self) -> int:
        return len(self.missing_from_template) + len(self.missing_from_events)

    def as_mapping(self) -> dict[str, object]:
        return {
            "template": list(self.template),
            "observed": list(self.observed),
            "frontier": list(self.frontier),
            "missing_from_template": list(self.missing_from_template),
            "missing_from_events": list(self.missing_from_events),
            "recovered": list(self.recovered),
            "discrepancies": self.discrepancies,
        }


def _failed_event_nodes(events: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    """Node ids named by ``plan_node_failed`` events, in the order recorded."""
    ordered: list[str] = []
    for event in events:
        if event.get("event_type") != "plan_node_failed":
            continue
        payload = event.get("payload")
        node_id = payload.get("plan_node_id") if isinstance(payload, Mapping) else None
        if isinstance(node_id, str) and node_id and node_id not in ordered:
            ordered.append(node_id)
    return tuple(ordered)


def reconcile_frontier(
    escalation: Mapping[str, object], events: Iterable[Mapping[str, object]]
) -> FrontierReconciliation:
    """Cross-check the escalation's declared frontier against its audit events."""
    directive = escalation.get("resume_directive_template")
    if not isinstance(directive, Mapping):
        raise AutoresumeError("block escalation has no resume directive template")
    raw = directive.get("retry_frontier")
    template = tuple(
        value for value in (raw if isinstance(raw, list) else ()) if isinstance(value, str) and value
    )
    final_status = {
        node["node_id"]: node.get("status")
        for node in (escalation.get("nodes") or ())
        if isinstance(node, Mapping) and isinstance(node.get("node_id"), str)
    }
    # A node can fail and then be repaired inside the same attempt, so an event
    # alone does not make it part of the retry set: the escalation's per-node
    # final status decides, and the event supplies the evidence that it ran.
    failed_events = _failed_event_nodes(events)
    observed = tuple(
        node_id for node_id in failed_events
        if final_status.get(node_id) in _TERMINAL_NODE_STATUSES
    )
    recovered = tuple(node_id for node_id in failed_events if node_id not in observed)
    missing_from_template = tuple(sorted(set(observed) - set(template)))
    missing_from_events = tuple(node_id for node_id in template if node_id not in failed_events)
    if not template and not observed:
        raise AutoresumeError("neither the escalation template nor its events name a retry frontier")
    return FrontierReconciliation(
        template=template,
        observed=observed,
        # Template order first: the contract puts the primary blocker at index
        # zero and consumers rely on that.  Appended nodes are sorted so an
        # identical escalation always produces an identical argv.
        frontier=template + missing_from_template,
        missing_from_template=missing_from_template,
        missing_from_events=missing_from_events,
        recovered=recovered,
    )


# ---------------------------------------------------------------------------
# Quiescence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivenessObservation:
    attempt_id: str
    kind: str
    state: str
    detail: str
    pid: int | None = None

    def as_mapping(self) -> dict[str, object]:
        return {"attempt_id": self.attempt_id, "kind": self.kind, "state": self.state,
                "detail": self.detail, "pid": self.pid}


class QuiescenceMonitor:
    """Decide whether any process belonging to this lineage is still running.

    The evidence is the lineage's own on-disk liveness markers, not the name
    of an operating-system process:

    * every attempt directory gets ``plan-graph-admission-liveness.json``
      (``pid`` + ``process_start_token``) written before anything else, so a
      graph controller is observable from the moment it exists;
    * each dispatched node's child run directory carries
      ``plan-graph-liveness.json``, so a controller that died while its
      children kept running is still observable.

    A pid is judged live only when the host reports the *same* process-start
    token that was recorded, which is what makes the check safe against pid
    reuse -- and is the identical rule
    ``PlanGraphAudit.reclaim_orphaned_successor_attempt`` and
    ``_liveness_disposition`` already apply.

    This is deliberately not ``pgrep -f <runner script>``.  Matching a process
    name requires the driver to know the campaign's runner filename, cannot
    tell one campaign's runner from another's on a shared host, cannot see a
    child agent whose parent has exited, and treats a recycled pid as live.
    The ``controller-liveness.schema.json`` lease that the run catalog reads
    is now written by every running controller (``core.controller_liveness``),
    but this check deliberately still reads the lineage's own markers.  The
    lease answers "is this run's controller alive", one run at a time and
    keyed by run id; quiescence is a question about a whole lineage including
    the children of a controller that has already exited, which is exactly
    what the admission and child markers describe.  The two agree on the rule
    that matters -- pid plus process-start token -- because both apply
    ``_process_start_token``.
    """

    def __init__(self, run_root: Path, *, process_probe: Callable[[int], str | None] | None = None) -> None:
        self.run_root = run_root
        self.process_probe = process_probe or _process_start_token

    def observe(self, seed_attempt_id: str) -> tuple[LivenessObservation, ...]:
        attempts = scan_attempts(self.run_root)
        lineage = {attempt.attempt_id for attempt in lineage_attempts(attempts, seed_attempt_id)}
        observations: list[LivenessObservation] = []
        for attempt in attempts.values():
            if attempt.finalized:
                # A finalized attempt's controller has already drained its
                # children and exited; its manifest is the proof.
                continue
            # An attempt whose descriptor is not on disk yet cannot be
            # attributed to a lineage, and ``_open_or_create`` writes the
            # admission marker first -- so an unattributable live directory is
            # treated as possibly ours rather than assumed to be someone's.
            if attempt.attempt_id not in lineage and attempt.descriptor is not None:
                continue
            observations.extend(self._attempt_observations(attempt))
        return tuple(observations)

    def _attempt_observations(self, attempt: Attempt) -> list[LivenessObservation]:
        observations = [self._admission_observation(attempt)]
        checkpoint = _read_json(attempt.directory / "checkpoint.json") or {}
        state = checkpoint.get("state")
        nodes = state.get("nodes") if isinstance(state, Mapping) else None
        if isinstance(nodes, Mapping):
            for node_id, node in nodes.items():
                if isinstance(node_id, str) and isinstance(node, Mapping):
                    child = self._child_observation(attempt, node_id, node)
                    if child is not None:
                        observations.append(child)
        return observations

    def _admission_observation(self, attempt: Attempt) -> LivenessObservation:
        marker = _read_json(attempt.directory / _ADMISSION_LIVENESS_NAME)
        if marker is None or marker.get("protocol") != _ADMISSION_LIVENESS_PROTOCOL:
            return LivenessObservation(
                attempt.attempt_id, "graph_admission", "ambiguous",
                "unfinalized attempt has no readable admission marker",
            )
        pid, token = marker.get("pid"), marker.get("process_start_token")
        if type(pid) is not int or pid < 1 or not isinstance(token, str) or not token:
            return LivenessObservation(
                attempt.attempt_id, "graph_admission", "ambiguous",
                "admission marker has no usable process identity",
            )
        return self._probe(attempt.attempt_id, "graph_admission", pid, token)

    def _child_observation(
        self, attempt: Attempt, node_id: str, node: Mapping[str, object]
    ) -> LivenessObservation | None:
        directory = node.get("run_dir")
        if not isinstance(directory, str) or not directory:
            return None
        run_dir = Path(directory)
        present = [run_dir / name for name in _CHILD_LIVENESS_NAMES if (run_dir / name).is_file()]
        if len(present) != 1:
            return None
        marker = _read_json(present[0])
        if marker is None or marker.get("protocol") != _CHILD_LIVENESS_PROTOCOL:
            return None
        pid, token = marker.get("pid"), marker.get("process_start_token")
        if marker.get("state") != "live" or type(pid) is not int or pid < 1 or not isinstance(token, str):
            return None
        return self._probe(attempt.attempt_id, f"child:{node_id}", pid, token)

    def _probe(self, attempt_id: str, kind: str, pid: int, token: str) -> LivenessObservation:
        try:
            observed = self.process_probe(pid)
        except Exception:  # a probe failure proves nothing either way
            return LivenessObservation(attempt_id, kind, "ambiguous", "process probe failed", pid)
        if observed == token:
            return LivenessObservation(attempt_id, kind, "live", "recorded process is still running", pid)
        return LivenessObservation(
            attempt_id, kind, "dead",
            "recorded process is gone" if observed is None else "pid was reused by another process",
            pid,
        )


def blocking_observations(
    observations: Sequence[LivenessObservation],
) -> tuple[LivenessObservation, ...]:
    """Observations that must clear before a successor may be launched."""
    return tuple(item for item in observations if item.state in {"live", "ambiguous"})


# ---------------------------------------------------------------------------
# No-progress guard
# ---------------------------------------------------------------------------


@dataclass
class NoProgressGuard:
    """Refuse to relaunch once consecutive escalations stop differing.

    The signature deliberately excludes the attempt id, which changes every
    time by construction, and includes the retry frontier and the blocking
    reason, which are what an operator would read to decide whether anything
    moved.
    """

    threshold: int = 3
    signature: tuple[object, ...] | None = None
    repeats: int = 0

    def observe(self, escalation: Mapping[str, object], frontier: Sequence[str]) -> bool:
        """Record one escalation; return ``True`` when the driver must stop."""
        if self.threshold < 1:
            raise AutoresumeError("no-progress threshold must be positive")
        signature = (
            str(escalation.get("reason")),
            escalation.get("blocked_node_id"),
            tuple(frontier),
        )
        self.repeats = self.repeats + 1 if signature == self.signature else 1
        self.signature = signature
        return self.repeats >= self.threshold


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoresumeResult:
    status: str
    reason: str
    iterations: int = 0
    launches: tuple[Mapping[str, object], ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {"status": self.status, "reason": self.reason, "iterations": self.iterations,
                "launches": [dict(launch) for launch in self.launches]}

    @property
    def exit_code(self) -> int:
        return {
            "succeeded": EXIT_SUCCEEDED,
            "dry_run": EXIT_SUCCEEDED,
            "no_progress": EXIT_NO_PROGRESS,
            "ceiling_reached": EXIT_CEILING,
        }.get(self.status, EXIT_BLOCKED)


@dataclass
class AutoresumeDriver:
    run_root: Path
    seed_attempt_id: str
    resume_command: tuple[str, ...]
    max_attempts: int = 2
    no_progress_threshold: int = 3
    poll_interval: float = 30.0
    quiescence_timeout: float = 3600.0
    dry_run: bool = False
    attempt_id_template: str = "{predecessor}-autoresume-{iteration}"
    launcher_cwd: Path | None = None
    process_probe: Callable[[int], str | None] | None = None
    runner: Callable[[Sequence[str]], int] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    emit: Callable[[Mapping[str, object]], None] | None = None
    guard: NoProgressGuard = field(init=False)
    monitor: QuiescenceMonitor = field(init=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise AutoresumeError("attempt ceiling must be positive")
        if not self.resume_command:
            raise AutoresumeError("a resume command is required")
        self.run_root = Path(self.run_root).resolve()
        self.resume_command = tuple(self.resume_command)
        self.guard = NoProgressGuard(self.no_progress_threshold)
        self.monitor = QuiescenceMonitor(self.run_root, process_probe=self.process_probe)

    # -- logging ------------------------------------------------------------

    def _log(self, record: Mapping[str, object]) -> None:
        if self.emit is not None:
            self.emit(record)
        else:
            print(json.dumps(record, sort_keys=True), flush=True)

    # -- steps --------------------------------------------------------------

    def wait_for_quiescence(self) -> tuple[LivenessObservation, ...]:
        """Block until nothing in the lineage is running, or give up loudly."""
        deadline = self.clock() + self.quiescence_timeout
        announced = False
        while True:
            observations = self.monitor.observe(self.seed_attempt_id)
            blocking = blocking_observations(observations)
            if not blocking:
                if announced:
                    self._log({"event": "quiescent"})
                return observations
            if not announced:
                self._log({
                    "event": "waiting_for_quiescence",
                    "blocking": [item.as_mapping() for item in blocking],
                })
                announced = True
            if self.clock() >= deadline:
                raise AutoresumeError(
                    "lineage did not become quiescent within "
                    f"{self.quiescence_timeout:g}s: "
                    + ", ".join(f"{item.attempt_id}/{item.kind}={item.state}" for item in blocking)
                )
            self.sleep(self.poll_interval)

    def resume_argv(
        self, predecessor: Predecessor, frontier: Sequence[str], iteration: int
    ) -> tuple[str, ...]:
        """Append the resume directive to the operator-supplied command.

        The flag spelling is ``run_plan_graph.py``'s, which is the repository's
        one resume entry point; any campaign runner that accepts the same
        directive works unchanged.  ``--graph-attempt-id`` is passed because
        that CLI requires it, though ``PlanGraph.resume`` reserves the
        successor's real identity itself.
        """
        directive = predecessor.escalation.get("resume_directive_template")
        logical = directive.get("logical_graph_id") if isinstance(directive, Mapping) else None
        if not isinstance(logical, str) or not logical:
            raise AutoresumeError("resume directive template has no logical graph id")
        argv = [
            *self.resume_command,
            "--graph-attempt-id",
            self.attempt_id_template.format(
                predecessor=predecessor.attempt_id, iteration=iteration,
                logical_graph_id=logical,
            ),
            "--run-root", str(self.run_root),
            "--resume",
            "--logical-graph-id", logical,
            "--predecessor-attempt-id", predecessor.attempt_id,
            "--blocker-evidence-ref", predecessor.blocker_evidence_ref,
        ]
        for node_id in frontier:
            argv += ["--retry-frontier", node_id]
        return tuple(argv)

    def _launch(self, argv: Sequence[str]) -> int:
        if self.runner is not None:
            return self.runner(argv)
        return subprocess.run(
            list(argv), cwd=str(self.launcher_cwd) if self.launcher_cwd else None, check=False
        ).returncode

    # -- loop ---------------------------------------------------------------

    def run(self) -> AutoresumeResult:
        launches: list[Mapping[str, object]] = []
        for iteration in range(1, self.max_attempts + 1):
            try:
                self.wait_for_quiescence()
                predecessor = find_predecessor(self.run_root, self.seed_attempt_id)
                reconciliation = reconcile_frontier(predecessor.escalation, predecessor.events)
            except AutoresumeError as exc:
                return AutoresumeResult("externally_blocked", str(exc), iteration - 1, tuple(launches))
            if reconciliation.discrepancies:
                # Never silently prefer one source over the other: the
                # operator needs to see that the published contract and the
                # audit trail disagreed, and in which direction.
                self._log({
                    "event": "frontier_discrepancy",
                    "predecessor": predecessor.attempt_id,
                    **reconciliation.as_mapping(),
                })
            frontier = reconciliation.frontier
            if self.guard.observe(predecessor.escalation, frontier):
                result = AutoresumeResult(
                    "no_progress",
                    f"{self.guard.repeats} consecutive identical escalations; operator review required",
                    iteration - 1, tuple(launches),
                )
                self._log({"event": "stop", **result.as_mapping(),
                           "frontier": list(frontier),
                           "predecessor": predecessor.attempt_id})
                return result
            argv = self.resume_argv(predecessor, frontier, iteration)
            record: dict[str, object] = {
                "iteration": iteration,
                "predecessor": predecessor.attempt_id,
                "frontier": list(frontier),
                "reconciliation": reconciliation.as_mapping(),
                "argv": list(argv),
            }
            if self.dry_run:
                self._log({"event": "would_launch", **record})
                return AutoresumeResult(
                    "dry_run", "reported the successor that would be launched",
                    iteration, (record,),
                )
            self._log({"event": "launching", **record})
            returncode = self._launch(argv)
            record["returncode"] = returncode
            launches.append(record)
            self._log({"event": "launched", "iteration": iteration, "returncode": returncode})
            if returncode == 0:
                return AutoresumeResult(
                    "succeeded", "successor attempt exited successfully", iteration, tuple(launches),
                )
        return AutoresumeResult(
            "ceiling_reached", f"attempt ceiling of {self.max_attempts} reached without success",
            self.max_attempts, tuple(launches),
        )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument(
        "--attempt-id", required=True,
        help="any attempt id in the lineage to watch; its descendants are discovered",
    )
    parser.add_argument(
        "--resume-command", nargs="+", required=True,
        help="command prefix that launches one attempt; the resume directive is appended",
    )
    parser.add_argument("--launcher-cwd", type=Path)
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("--quiescence-timeout", type=float, default=3600.0)
    parser.add_argument(
        "--max-attempts", type=int, default=2,
        help="successor attempts this driver may launch (default: 2)",
    )
    parser.add_argument("--no-progress-threshold", type=int, default=3)
    parser.add_argument("--attempt-id-template", default="{predecessor}-autoresume-{iteration}")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        driver = AutoresumeDriver(
            run_root=arguments.run_root,
            seed_attempt_id=arguments.attempt_id,
            resume_command=tuple(arguments.resume_command),
            max_attempts=arguments.max_attempts,
            no_progress_threshold=arguments.no_progress_threshold,
            poll_interval=arguments.poll_interval,
            quiescence_timeout=arguments.quiescence_timeout,
            dry_run=arguments.dry_run,
            attempt_id_template=arguments.attempt_id_template,
            launcher_cwd=arguments.launcher_cwd,
        )
        result = driver.run()
    except (AutoresumeError, OSError) as exc:
        result = AutoresumeResult("externally_blocked", str(exc))
    print(json.dumps({"event": "result", **result.as_mapping()}, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
