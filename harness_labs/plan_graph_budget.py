"""Append-only, per-lineage retry budget ledger for PlanGraph.

Reservations are durably recorded before a child launch.  Thus an interrupted
controller can spend an allowance, but cannot create an unaccounted launch.
All mutations are serialized by an advisory lock and fsynced before return.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class BudgetConfig:
    node_gate_limit: int = 5
    finding_key_limit: int = 3
    infra_limit: int = 3
    config_policy_limit: int = 1
    structural_decision_limit: int = 2

    def __post_init__(self) -> None:
        if any(not isinstance(value, int) or value < 1 for value in self.__dict__.values()):
            raise BudgetError("budget limits must be positive integers")


def gate_digest(argv: tuple[str, ...]) -> str:
    """Return the explicitly incomplete v1 identity for a node's gate."""
    return hashlib.sha256(json.dumps(list(argv), separators=(",", ":")).encode()).hexdigest()


_CLASS_LIMITS = {
    "product": "node_gate_limit",
    "indeterminate": "node_gate_limit",
    "infrastructure_transient": "infra_limit",
    "harness_or_configuration": "config_policy_limit",
    "policy_violation": "config_policy_limit",
    "structural_decision": "structural_decision_limit",
}

# These are deliberately distinct from failure classifications.  RB-02 owns
# launch reservations; later stages may import gate and repair evidence, but a
# launch-level reservation must never be mistaken for either of those counts.
_ATTEMPT_COUNTERS = (
    "graph_launches",
    "gate_invocations",
    "repair_dispatches",
    "structural_decisions",
)


class RetryBudgetLedger:
    protocol = "retry-budget-ledger/1"

    def __init__(self, run_root: Path, lineage_id: str, config: BudgetConfig | None = None) -> None:
        if not isinstance(lineage_id, str) or not lineage_id or any(c in lineage_id for c in "/\\"):
            raise BudgetError("lineage_id must be a non-empty path-safe name")
        self.config = config or BudgetConfig()
        self.path = run_root.resolve() / ".plan-graph-budgets" / f"{lineage_id}.jsonl"
        self.lineage_id = lineage_id

    def register(self, *, plan_sha256: str, gates: Mapping[str, str]) -> None:
        """Bind a plan version, requiring digest-bound operator relief for changes."""
        self._validate_registration(plan_sha256, gates)
        with self._locked() as handle:
            state = self._fold(handle)
            versions = state["plan_sha256"]
            changed_plan = bool(versions and plan_sha256 not in versions)
            if changed_plan and state["accepted_plan_sha256"] != plan_sha256:
                raise BudgetError(
                    "changed-plan lineage registration is blocked; "
                    "operator relief required for this plan digest"
                )
            if not versions:
                self._append(handle, {"event": "registered", "plan_sha256": plan_sha256, "gates": dict(gates)})
                return
            # Validate the full re-registration before consuming any operator
            # authorization.  Otherwise an approved change earlier in the
            # mapping could be recorded before a later, unapproved new node
            # rejects the registration.
            for node_id, gate in gates.items():
                self._assert_gate_change_authorized(
                    state, node_id, gate, allow_new_node=True
                )
            for node_id, gate in gates.items():
                self._record_gate_change_if_authorized(
                    handle, state, node_id, gate, allow_new_node=True
                )
            if changed_plan:
                # This append records the revised plan and consumes precisely
                # the authorization bound to its digest.
                self._append(handle, {
                    "event": "registered", "plan_sha256": plan_sha256,
                    "gates": dict(gates), "consumes_plan_change_authorization": True,
                })

    def reserve(
        self,
        *,
        node_id: str,
        gate: str,
        failure_keys: Sequence[str] = (),
        classification: str = "indeterminate",
        failure_reason: str | None = None,
        graph_attempt_id: str | None = None,
    ) -> str:
        """Atomically consume the applicable retry allowance and reserve a launch.

        Until structured child classification is available, callers use the
        conservative ``indeterminate`` class.  A supplied failure reason gets
        a stable key so retries cannot evade the per-finding counter.
        """
        keys = self._failure_keys(failure_keys, failure_reason)
        if classification not in _CLASS_LIMITS:
            raise BudgetError("invalid retry classification")
        if graph_attempt_id is not None and (not isinstance(graph_attempt_id, str) or not graph_attempt_id):
            raise BudgetError("graph_attempt_id must be a non-empty string when supplied")
        with self._locked() as handle:
            state = self._fold(handle)
            self._record_gate_change_if_authorized(handle, state, node_id, gate)
            node = self._node(state, node_id)
            if node.get("blocked"):
                raise BudgetError(f"retry budget for node {node_id!r} is blocked; operator relief required")
            self._assert_capacity(state, node_id, node, keys, classification)
            reservation_id = f"budget-{uuid4().hex}"
            self._append(handle, {
                "event": "reserved", "reservation_id": reservation_id,
                "node_id": node_id, "gate": gate, "failure_keys": list(keys),
                "classification": classification, "graph_attempt_id": graph_attempt_id,
                "attempt_counters": self._reservation_attempt_counters(classification),
            })
            return reservation_id

    def started(self, reservation_id: str) -> None:
        self._transition(reservation_id, "started")

    def completed(self, reservation_id: str, status: str, *, tokens_total: int | None = None) -> None:
        if status not in {"succeeded", "failed", "blocked"}:
            raise BudgetError("invalid reservation completion status")
        if tokens_total is not None and (not isinstance(tokens_total, int) or tokens_total < 0):
            raise BudgetError("tokens_total must be a non-negative integer or null")
        self._transition(reservation_id, "completed", status=status, tokens_total=tokens_total)

    def import_child_evidence(self, *, node_id: str, evidence: Mapping[str, object]) -> tuple[str, ...]:
        """Import structured child invocation facts once, keyed by invocation id.

        This deliberately accepts no prose-derived counts.  Replaying a child
        result is safe because identifiers already folded into the ledger are
        ignored rather than charged a second time.
        """
        verification = evidence.get("verification")
        if not isinstance(verification, Mapping):
            return ()
        commands = verification.get("command_attempts", ())
        repair_ids = verification.get("repair_invocation_ids", ())
        repairs = verification.get("repair_invocations", ())
        if not isinstance(commands, (list, tuple)) or not isinstance(repair_ids, (list, tuple)) or not isinstance(repairs, (list, tuple)):
            raise BudgetError("structured child verification evidence is invalid")
        if repair_ids and not repairs:
            raise BudgetError("structured child repair evidence lacks classifications")
        facts: list[dict[str, object]] = []
        for command in commands:
            if not isinstance(command, Mapping):
                raise BudgetError("structured child command evidence is invalid")
            invocation_id = command.get("invocation_id")
            failure = command.get("failure")
            if not isinstance(invocation_id, str) or not invocation_id:
                raise BudgetError("structured child command evidence lacks invocation_id")
            classification = failure.get("classification") if isinstance(failure, Mapping) else None
            if classification is not None and classification not in _CLASS_LIMITS:
                raise BudgetError("structured child command has invalid classification")
            fact: dict[str, object] = {"invocation_id": invocation_id, "kind": "gate_invocation"}
            if classification is not None:
                fact["classification"] = classification
            facts.append(fact)
        structured_repair_ids: list[str] = []
        for repair in repairs:
            if not isinstance(repair, Mapping):
                raise BudgetError("structured child repair evidence is invalid")
            invocation_id = repair.get("invocation_id")
            classification = repair.get("classification")
            if not isinstance(invocation_id, str) or not invocation_id or classification not in _CLASS_LIMITS:
                raise BudgetError("structured child repair evidence is invalid")
            structured_repair_ids.append(invocation_id)
            facts.append({"invocation_id": invocation_id, "kind": "repair_dispatch", "classification": classification})
        if tuple(repair_ids) != tuple(structured_repair_ids):
            raise BudgetError("structured child repair evidence ids do not match dispatches")
        with self._locked() as handle:
            state = self._fold(handle)
            imported: list[str] = []
            for fact in facts:
                if fact["invocation_id"] in state["imported_invocations"]:
                    continue
                self._append(handle, {"event": "evidence_imported", "node_id": node_id, **fact})
                state["imported_invocations"].add(fact["invocation_id"])
                imported.append(fact["invocation_id"])
            return tuple(imported)

    def abandon(self, *, node_id: str, disposition: str, reason: str, graph_attempt_id: str | None = None) -> tuple[str, ...]:
        """Terminalize interrupted reservations after an audit reconciliation."""
        if disposition not in {"sealed", "blocked", "abandoned"} or not reason:
            raise BudgetError("abandon requires an audit disposition and reason")
        with self._locked() as handle:
            state = self._fold(handle)
            abandoned = tuple(
                reservation_id for reservation_id, reservation in state["reservations"].items()
                if reservation["node_id"] == node_id and reservation["state"] in {"reserved", "started"}
                and (graph_attempt_id is None or reservation.get("graph_attempt_id") == graph_attempt_id)
            )
            for reservation_id in abandoned:
                reservation = state["reservations"][reservation_id]
                self._append(handle, {
                    "event": "abandoned", "reservation_id": reservation_id,
                    "node_id": node_id, "disposition": disposition, "reason": reason,
                    "graph_attempt_id": reservation.get("graph_attempt_id"),
                })
            return abandoned

    def reconcile_attempt(
        self,
        *,
        graph_attempt_id: str,
        disposition: str,
        reason: str,
        live_node_ids: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Terminalize every interrupted reservation owned by a dead attempt.

        A successor must call this before admission.  The durable disposition
        preserves why a reservation stopped while the reservation itself always
        reaches the terminal ``abandoned`` state.
        """
        if not isinstance(graph_attempt_id, str) or not graph_attempt_id:
            raise BudgetError("reconcile_attempt requires a graph_attempt_id")
        if disposition not in {"sealed", "blocked", "abandoned"} or not isinstance(reason, str) or not reason:
            raise BudgetError("reconcile_attempt requires an audit disposition and reason")
        if (
            not isinstance(live_node_ids, Sequence)
            or isinstance(live_node_ids, (str, bytes))
            or not all(isinstance(node_id, str) and node_id for node_id in live_node_ids)
        ):
            raise BudgetError("live_node_ids must be a sequence of non-empty strings")
        live = frozenset(live_node_ids)
        with self._locked() as handle:
            state = self._fold(handle)
            abandoned = tuple(
                reservation_id for reservation_id, reservation in state["reservations"].items()
                if reservation.get("graph_attempt_id") == graph_attempt_id
                and reservation["state"] in {"reserved", "started"}
                and reservation["node_id"] not in live
            )
            for reservation_id in abandoned:
                reservation = state["reservations"][reservation_id]
                self._append(handle, {
                    "event": "abandoned", "reservation_id": reservation_id,
                    "node_id": reservation["node_id"], "graph_attempt_id": graph_attempt_id,
                    "disposition": disposition, "reason": reason,
                })
            return abandoned

    def extend(self, *, node_id: str, launches: int, reason: str) -> None:
        if not isinstance(launches, int) or launches < 1 or not isinstance(reason, str) or not reason:
            raise BudgetError("extend requires positive launches and a reason")
        self._operator_event({"event": "extended", "node_id": node_id, "launches": launches, "reason": reason})

    def reset(self, *, node_id: str, reason: str, accept_gate_change: bool = False,
              accept_plan_sha256: str | None = None, carryover: str = "full") -> None:
        if (
            carryover not in {"full", "reset"}
            or not isinstance(reason, str)
            or not reason
            or not isinstance(accept_gate_change, bool)
            or (accept_plan_sha256 is not None and (
                not isinstance(accept_plan_sha256, str)
                or len(accept_plan_sha256) != 64
                or any(character not in "0123456789abcdef" for character in accept_plan_sha256)
            ))
        ):
            raise BudgetError("reset requires a reason, valid optional plan digest, boolean gate-change authorization, and carryover full or reset")
        self._operator_event({"event": "reset", "node_id": node_id, "reason": reason,
                              "accept_gate_change": accept_gate_change,
                              "accept_plan_sha256": accept_plan_sha256,
                              "carryover": carryover})

    def verdict(self, *, node_id: str, gate: str) -> None:
        """Read-only admission verdict used by resume before it allocates work."""
        with self._locked(shared=True) as handle:
            state = self._fold(handle)
            node = self._node(state, node_id)
            if state["gates"].get(node_id) != gate and not node.get("accept_gate_change"):
                self._assert_gate(state, node_id, gate)
            self._assert_capacity(state, node_id, node, (), "indeterminate")

    def _record_gate_change_if_authorized(
        self,
        handle,
        state: dict[str, Any],
        node_id: str,
        gate: str,
        *,
        allow_new_node: bool = False,
    ) -> None:
        if not self._assert_gate_change_authorized(
            state, node_id, gate, allow_new_node=allow_new_node
        ):
            return
        node = self._node(state, node_id)
        self._append(handle, {"event": "gate_changed", "node_id": node_id, "gate": gate,
                              "identity": "gate_identity_v1_incomplete", "authorized": True})
        state["gates"][node_id] = gate
        node["accept_gate_change"] = False  # Authorization is consumed by this exact change.

    def _assert_gate_change_authorized(
        self,
        state: dict[str, Any],
        node_id: str,
        gate: str,
        *,
        allow_new_node: bool = False,
    ) -> bool:
        """Return whether a gate change is needed after checking its authority."""
        known = state["gates"].get(node_id)
        node = self._node(state, node_id)
        if known is None:
            # A same-lineage re-registration may introduce a newly planned
            # node only after an operator explicitly authorizes its gate.
            # ``reset`` is durable and the authorization is consumed below,
            # so an unreviewed addition remains fail-closed.
            if not allow_new_node or not node.get("accept_gate_change"):
                raise BudgetError(f"node {node_id!r} is not registered in retry lineage; operator relief required")
        elif known == gate:
            return False
        if not node.get("accept_gate_change"):
            self._assert_gate(state, node_id, gate)
        return True

    def _assert_capacity(self, state: Mapping[str, Any], node_id: str, node: Mapping[str, Any], keys: Sequence[str], classification: str) -> None:
        extra = int(node.get("extra", 0))
        if int(node.get("launches", 0)) >= self.config.node_gate_limit + extra:
            raise BudgetError(f"retry budget exhausted for node {node_id!r}; operator relief required")
        limit = getattr(self.config, _CLASS_LIMITS[classification]) + extra
        counter = int(node.get("counters", {}).get(classification, 0))
        if counter >= limit:
            raise BudgetError(f"retry budget exhausted for node {node_id!r}; operator relief required")
        finding_counts = state["finding_keys"]
        if any(
            int(finding_counts.get(key, 0)) >= self.config.finding_key_limit for key in keys
        ):
            raise BudgetError(f"retry finding budget exhausted for node {node_id!r}; operator relief required")

    @staticmethod
    def _failure_keys(keys: Sequence[str], failure_reason: str | None) -> tuple[str, ...]:
        if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)) or not all(isinstance(key, str) and key for key in keys):
            raise BudgetError("failure_keys must be a sequence of non-empty strings")
        normalized = tuple(sorted(set(keys)))
        if normalized:
            return normalized
        if failure_reason is not None:
            if not isinstance(failure_reason, str) or not failure_reason:
                raise BudgetError("failure_reason must be a non-empty string when supplied")
            return ("reason:" + hashlib.sha256(failure_reason.encode("utf-8")).hexdigest(),)
        return ()

    @staticmethod
    def _reservation_attempt_counters(classification: str) -> dict[str, int]:
        """Return the non-inferred counters consumed by a launch reservation.

        A reservation is exactly one graph launch.  It is not evidence of a
        gate invocation or a repair dispatch; those counters remain zero until
        a later structured-evidence import can prove them.  A structural
        decision is explicit in the requested classification and is therefore
        the one additional counter this stage may record.
        """
        return {
            "graph_launches": 1,
            "gate_invocations": 0,
            "repair_dispatches": 0,
            "structural_decisions": int(classification == "structural_decision"),
        }

    def _operator_event(self, event: Mapping[str, Any]) -> None:
        with self._locked() as handle:
            self._fold(handle)
            self._append(handle, dict(event, actor="operator"))

    def _transition(self, reservation_id: str, event: str, **extra: Any) -> None:
        with self._locked() as handle:
            state = self._fold(handle)
            reservation = state["reservations"].get(reservation_id)
            if reservation is None:
                raise BudgetError("unknown retry reservation")
            if event == "started" and reservation["state"] != "reserved":
                raise BudgetError("retry reservation is not reserved")
            if event == "completed" and reservation["state"] not in {"reserved", "started"}:
                raise BudgetError("retry reservation is already terminal")
            self._append(handle, {"event": event, "reservation_id": reservation_id, **extra})

    def _assert_gate(self, state: Mapping[str, Any], node_id: str, gate: str) -> None:
        known = state["gates"].get(node_id)
        if known is None:
            raise BudgetError(f"node {node_id!r} is not registered in retry lineage")
        if known != gate:
            raise BudgetError("gate-change block (gate_identity_v1_incomplete); operator relief required")

    @staticmethod
    def _validate_registration(plan_sha256: str, gates: Mapping[str, str]) -> None:
        if not isinstance(plan_sha256, str) or not plan_sha256 or not all(isinstance(k, str) and isinstance(v, str) for k, v in gates.items()):
            raise BudgetError("invalid lineage registration")

    @staticmethod
    def _node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
        return state["nodes"].setdefault(
            node_id,
            {"launches": 0, "extra": 0, "reservations": {}, "counters": {}, "finding_keys": {}},
        )

    def _locked(self, shared: bool = False):
        budget_directory = self.path.parent
        # A new ledger directory must be published in its parent before an
        # appended event can be considered crash-durable.  The append path
        # below separately syncs the ledger directory to publish its file.
        directory_was_missing = not budget_directory.exists()
        budget_directory.mkdir(parents=True, exist_ok=True)
        if directory_was_missing:
            self._fsync_directory(budget_directory.parent)
        # Capture this before opening with ``a+`` because opening creates the
        # lineage file.  The first append must subsequently publish that new
        # directory entry after the file contents reach stable storage.
        ledger_was_missing = not self.path.exists()
        handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        return _Lock(handle, ledger_was_missing=ledger_was_missing)

    def _fold(self, handle) -> dict[str, Any]:
        state: dict[str, Any] = {"plan_sha256": set(), "gates": {}, "nodes": {}, "reservations": {}, "finding_keys": {}, "accepted_plan_sha256": None, "imported_invocations": set()}
        handle.seek(0)
        for line in handle:
            try:
                event = json.loads(line)
                if (
                    not isinstance(event, dict)
                    or event.get("protocol") != self.protocol
                    or event.get("lineage_id") != self.lineage_id
                ):
                    raise ValueError
                kind = event["event"]
                if kind == "registered":
                    changed_plan = bool(state["plan_sha256"] and event["plan_sha256"] not in state["plan_sha256"])
                    if changed_plan:
                        if (
                            not event.get("consumes_plan_change_authorization")
                            or state["accepted_plan_sha256"] != event["plan_sha256"]
                        ):
                            raise ValueError
                        state["accepted_plan_sha256"] = None
                    elif event.get("consumes_plan_change_authorization"):
                        raise ValueError
                    state["plan_sha256"].add(event["plan_sha256"]); state["gates"].update(event["gates"])
                elif kind == "gate_changed":
                    state["gates"][event["node_id"]] = event["gate"]
                    self._node(state, event["node_id"])["accept_gate_change"] = False
                elif kind == "reserved":
                    node = self._node(state, event["node_id"]); node["launches"] += 1
                    classification = event.get("classification", "indeterminate")
                    if classification not in _CLASS_LIMITS: raise ValueError
                    attempt_counters = event.get("attempt_counters")
                    if attempt_counters is None:
                        # Pre-counter RB-02 events remain readable, but every
                        # newly appended reservation persists its taxonomy.
                        attempt_counters = self._reservation_attempt_counters(classification)
                    if (
                        not isinstance(attempt_counters, dict)
                        or set(attempt_counters) != set(_ATTEMPT_COUNTERS)
                        or not all(type(value) is int and value >= 0 for value in attempt_counters.values())
                        or attempt_counters != self._reservation_attempt_counters(classification)
                    ):
                        raise ValueError
                    node_attempt_counters = node.setdefault(
                        "attempt_counters", {name: 0 for name in _ATTEMPT_COUNTERS}
                    )
                    for name, value in attempt_counters.items():
                        node_attempt_counters[name] += value
                    node["counters"][classification] = node["counters"].get(classification, 0) + 1
                    for key in event.get("failure_keys", []):
                        state["finding_keys"][key] = state["finding_keys"].get(key, 0) + 1
                        node["finding_keys"][key] = node["finding_keys"].get(key, 0) + 1
                    node["reservations"][event["reservation_id"]] = "reserved"
                    state["reservations"][event["reservation_id"]] = {"state": "reserved", "node_id": event["node_id"], "graph_attempt_id": event.get("graph_attempt_id"), "classification": classification, "failure_keys": tuple(event.get("failure_keys", ())) }
                elif kind == "started":
                    reservation = state["reservations"][event["reservation_id"]]
                    if reservation["state"] != "reserved": raise ValueError
                    reservation["state"] = "started"
                elif kind == "completed":
                    reservation = state["reservations"][event["reservation_id"]]
                    if reservation["state"] not in {"reserved", "started"}: raise ValueError
                    reservation["state"] = "completed"
                elif kind == "abandoned":
                    reservation = state["reservations"][event["reservation_id"]]
                    if reservation["state"] not in {"reserved", "started"}: raise ValueError
                    if event.get("node_id") != reservation["node_id"] or event.get("graph_attempt_id") != reservation.get("graph_attempt_id"):
                        raise ValueError
                    if event.get("disposition") not in {"sealed", "blocked", "abandoned"} or not event.get("reason"):
                        raise ValueError
                    reservation["state"] = "abandoned"
                elif kind == "evidence_imported":
                    invocation_id = event["invocation_id"]
                    classification = event.get("classification")
                    if (not isinstance(invocation_id, str) or not invocation_id
                        or invocation_id in state["imported_invocations"]
                        or (classification is not None and classification not in _CLASS_LIMITS)
                        or (event.get("kind") == "repair_dispatch" and classification is None)
                        or event.get("kind") not in {"gate_invocation", "repair_dispatch"}):
                        raise ValueError
                    node = self._node(state, event["node_id"])
                    counters = node.setdefault("attempt_counters", {name: 0 for name in _ATTEMPT_COUNTERS})
                    counters["gate_invocations" if event["kind"] == "gate_invocation" else "repair_dispatches"] += 1
                    if classification is not None:
                        node["counters"][classification] = node["counters"].get(classification, 0) + 1
                    state["imported_invocations"].add(invocation_id)
                elif kind == "blocked": self._node(state, event["node_id"])["blocked"] = True
                elif kind == "extended":
                    node = self._node(state, event["node_id"]); node["extra"] += event["launches"]; node["blocked"] = False
                elif kind == "reset":
                    node = self._node(state, event["node_id"]); node["blocked"] = False
                    node["accept_gate_change"] = bool(event.get("accept_gate_change"))
                    accepted_plan_sha256 = event.get("accept_plan_sha256")
                    if accepted_plan_sha256 is not None and (
                        not isinstance(accepted_plan_sha256, str)
                        or len(accepted_plan_sha256) != 64
                        or any(character not in "0123456789abcdef" for character in accepted_plan_sha256)
                    ):
                        raise ValueError
                    state["accepted_plan_sha256"] = accepted_plan_sha256
                    if event["carryover"] == "reset":
                        node["launches"] = 0; node["counters"] = {}
                        node["attempt_counters"] = {name: 0 for name in _ATTEMPT_COUNTERS}
                        for key, count in node["finding_keys"].items():
                            remaining = state["finding_keys"].get(key, 0) - count
                            if remaining > 0:
                                state["finding_keys"][key] = remaining
                            else:
                                state["finding_keys"].pop(key, None)
                        node["finding_keys"] = {}
                else: raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise BudgetError("retry budget ledger is corrupt; operator intervention required") from exc
        return state

    def _append(self, handle, event: dict[str, Any]) -> None:
        event = {"protocol": self.protocol, "lineage_id": self.lineage_id, **event}
        handle.seek(0, os.SEEK_END); handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"); handle.flush(); os.fsync(handle.fileno())
        if handle.ledger_was_missing:
            # The file fsync makes the event durable; syncing the containing
            # directory then makes its initial lineage-file entry durable.
            self._fsync_directory(self.path.parent)
            handle.ledger_was_missing = False

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class _Lock:
    def __init__(self, handle, *, ledger_was_missing: bool):
        self.handle = handle
        self.ledger_was_missing = ledger_was_missing

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __iter__(self):
        return iter(self.handle)

    def __enter__(self): return self
    def __exit__(self, *_):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN); self.handle.close()
