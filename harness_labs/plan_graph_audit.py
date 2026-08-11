"""Durable audit state for one :mod:`harness_labs.plan_graph` execution.

This adapter keeps PlanGraph's scheduling state in the repository's canonical
``AuditJournal``.  It intentionally contains no liveness information: a
checkpoint is durable historical evidence, not proof that a controller lives.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .audit import AuditActor, AuditError, AuditJournal


_ACTOR = AuditActor("plan-graph", "plan_graph_controller")
_TERMINAL = frozenset({"succeeded", "failed", "blocked", "interrupted"})
_GRAPH_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_IMMUTABLE_NODE_FIELDS = (
    "objective",
    "plan_sections",
    "criteria",
    "depends_on",
    "verification_argv",
)


def validate_plan_graph_id(value: str) -> None:
    """Require one normalized filename-safe graph or attempt identity."""

    if not isinstance(value, str) or _GRAPH_ID.fullmatch(value) is None:
        raise ValueError(
            "PlanGraph ID must match ^[a-z0-9][a-z0-9-]{0,127}$"
        )


class PlanGraphAudit:
    """Own the durable identity, descriptor, events, and checkpoint of a graph."""

    def __init__(
        self,
        *,
        repository: Path,
        run_root: Path,
        graph_run_id: str,
        plan: str,
        plan_sha256: str,
        base_commit: str,
        registration_binding: Mapping[str, str],
        objective: str,
        nodes: Mapping[str, Mapping[str, object]],
        functionality_tests: tuple[str, ...],
    ) -> None:
        validate_plan_graph_id(graph_run_id)
        self.graph_run_id = graph_run_id
        self.run_dir = (run_root / graph_run_id).resolve()
        expected_binding = {
            "logical_graph_id",
            "registration_protocol",
            "registration_digest",
            "graph_attempt_id",
        }
        if set(registration_binding) != expected_binding or not all(
            isinstance(value, str) and value
            for value in registration_binding.values()
        ):
            raise ValueError("registration binding is invalid")
        if registration_binding["graph_attempt_id"] != graph_run_id:
            raise ValueError("registration binding does not name this attempt")
        self._initial_state = {
            "graph_run_id": graph_run_id,
            "registration_binding": dict(registration_binding),
            "plan": plan,
            "plan_digest": plan_sha256,
            "base_commit": base_commit,
            "plan_graph_digest": registration_binding["registration_digest"],
            "current_candidate_commit": base_commit,
            "ordered_node_ids": list(nodes),
            "nodes": {key: dict(value) for key, value in nodes.items()},
            "registered_functionality_tests": list(functionality_tests),
            "current_node_id": None,
            "functionality_test": {"state": "unavailable", "reason": "not_run"},
            "terminal_graph_status": None,
        }
        self.descriptor = {
            "protocol": "harness-run-descriptor/1",
            "run_kind": "plan_graph",
            "run_id": graph_run_id,
            "created_at": _timestamp(),
            "objective": objective,
            "evidence_classification": "production_lifecycle",
            "repository": {
                "path": str(repository.resolve()),
                "base_branch": "unavailable",
                "base_commit": base_commit,
            },
            "approved_plan": {"path": plan, "sha256": plan_sha256},
            "parent_correlation": None,
        }
        self.journal = self._open_or_create()

    @property
    def state(self) -> dict[str, Any]:
        return self.journal.checkpoint_state()

    @property
    def terminal(self) -> bool:
        return self.state.get("terminal_graph_status") in _TERMINAL

    def node_started(self, node_id: str) -> None:
        self._transition(
            "plan_node_started",
            "running",
            node_id,
            {"status": "running", "started_at": _timestamp()},
        )

    def node_completed(self, node_id: str, candidate_commit: str) -> None:
        self._transition(
            "plan_node_completed",
            "succeeded",
            node_id,
            {
                "status": "succeeded",
                "finished_at": _timestamp(),
                "candidate_commit": candidate_commit,
            },
            current_candidate_commit=candidate_commit,
        )

    def node_failed(self, node_id: str, status: str, evidence: object | None) -> None:
        self._transition(
            "plan_node_failed",
            status,
            node_id,
            {"status": status, "finished_at": _timestamp(), "evidence": evidence},
        )

    def functionality_completed(self, command: str, candidate_commit: str) -> None:
        state = self.state
        state["functionality_test"] = {
            "state": "succeeded",
            "command": command,
            "candidate_commit": candidate_commit,
            "finished_at": _timestamp(),
        }
        self.journal.append(
            "functionality_test_completed",
            status="succeeded",
            payload=dict(state["functionality_test"]),
            actor=_ACTOR,
        )
        self.journal.checkpoint("running", state)

    def functionality_failed(self, command: str, candidate_commit: str, error: str) -> None:
        state = self.state
        state["functionality_test"] = {
            "state": "failed",
            "command": command,
            "candidate_commit": candidate_commit,
            "error": error,
            "finished_at": _timestamp(),
        }
        self.journal.append(
            "functionality_test_completed",
            status="failed",
            payload=dict(state["functionality_test"]),
            actor=_ACTOR,
        )
        self.journal.checkpoint("running", state)

    def finalize(self, status: str, result: Mapping[str, object]) -> None:
        state = self.state
        state["current_node_id"] = None
        state["terminal_graph_status"] = status
        self.journal.append(
            "plan_graph_completed",
            status=status,
            payload={"terminal_graph_status": status},
            actor=_ACTOR,
        )
        self.journal.finalize(status, result=dict(result), state=state)

    def _open_or_create(self) -> AuditJournal:
        if self.run_dir.exists():
            journal = AuditJournal.open_existing(self.run_dir, actor=_ACTOR)
            state = journal.checkpoint_state()
            if state.get("graph_run_id") != self.graph_run_id:
                raise AuditError("existing audit directory is not this PlanGraph")
            if state.get("registration_binding") != self._initial_state["registration_binding"]:
                raise AuditError("existing PlanGraph registration binding does not match")
            if state.get("plan_graph_digest") != self._initial_state["plan_graph_digest"]:
                raise AuditError("existing PlanGraph registration digest does not match")
            if state.get("ordered_node_ids") != self._initial_state["ordered_node_ids"]:
                raise AuditError("existing PlanGraph checkpoint node order does not match")
            if state.get("registered_functionality_tests") != self._initial_state["registered_functionality_tests"]:
                raise AuditError("existing PlanGraph functionality tests do not match")
            actual_nodes = state.get("nodes")
            expected_nodes = self._initial_state["nodes"]
            if not isinstance(actual_nodes, Mapping) or set(actual_nodes) != set(expected_nodes):
                raise AuditError("existing PlanGraph checkpoint node set does not match")
            for node_id, expected in expected_nodes.items():
                actual = actual_nodes.get(node_id)
                if not isinstance(actual, Mapping) or any(
                    actual.get(field) != expected.get(field)
                    for field in _IMMUTABLE_NODE_FIELDS
                ):
                    raise AuditError(
                        f"existing PlanGraph checkpoint node {node_id!r} does not match"
                    )
            return journal
        self.run_dir.parent.mkdir(parents=True, exist_ok=True)
        # AuditJournal creates its own directory; write the descriptor only after
        # that succeeds, then bind its digest in the first graph event.
        journal = AuditJournal(self.run_dir, self.graph_run_id, actor=_ACTOR)
        descriptor_path = self.run_dir / "descriptor.json"
        descriptor_raw = (
            json.dumps(self.descriptor, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        _atomic_write(descriptor_path, descriptor_raw, 0o600)
        journal.append(
            "plan_graph_initialized",
            status="running",
            payload={
                "graph_run_id": self.graph_run_id,
                "descriptor_sha256": hashlib.sha256(descriptor_raw).hexdigest(),
                "plan": self._initial_state["plan"],
                "plan_digest": self._initial_state["plan_digest"],
                "base_commit": self._initial_state["base_commit"],
                "registration_binding": self._initial_state["registration_binding"],
                "ordered_node_ids": self._initial_state["ordered_node_ids"],
            },
            actor=_ACTOR,
        )
        journal.checkpoint("running", self._initial_state)
        return journal

    def _transition(
        self,
        event_type: str,
        status: str,
        node_id: str,
        updates: Mapping[str, object],
        **state_updates: object,
    ) -> None:
        state = self.state
        nodes = state.get("nodes")
        if not isinstance(nodes, dict) or node_id not in nodes:
            raise AuditError(f"PlanGraph checkpoint has no node {node_id!r}")
        node = nodes[node_id]
        if not isinstance(node, dict):
            raise AuditError(f"PlanGraph checkpoint node {node_id!r} is invalid")
        node.update(updates)
        state["current_node_id"] = node_id if status == "running" else None
        state.update(state_updates)
        self.journal.append(
            event_type,
            status=status,
            payload={"plan_node_id": node_id, **dict(updates)},
            actor=_ACTOR,
        )
        self.journal.checkpoint("running", state)


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        temporary.replace(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
