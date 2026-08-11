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

from .audit import AuditActor, AuditConflictError, AuditError, AuditJournal


_ACTOR = AuditActor("plan-graph", "plan_graph_controller")
_TERMINAL = frozenset({"succeeded", "failed", "blocked", "interrupted"})
_AUDIT_STATE_PROTOCOL = "harness-plan-graph-audit/1"
_GIT_COMMIT = re.compile(r"^[a-f0-9]{40}$")


class PlanGraphAudit:
    """Own the durable identity, descriptor, events, and checkpoint of a graph."""

    def __init__(
        self,
        *,
        run_root: Path,
        graph_run_id: str,
        plan: str,
        base_commit: str,
        objective: str,
        nodes: Mapping[str, Mapping[str, object]],
        functionality_tests: tuple[str, ...],
    ) -> None:
        if (
            not graph_run_id
            or graph_run_id in {".", ".."}
            or "/" in graph_run_id
            or "\\" in graph_run_id
        ):
            raise ValueError("graph_run_id must be a non-empty path-safe name")
        self.graph_run_id = graph_run_id
        self.run_dir = (run_root / graph_run_id).resolve()
        plan_digest = _plan_digest(plan)
        if plan_digest is None:
            raise ValueError("durable PlanGraph requires a readable approved plan")
        self._initial_state = {
            "audit_state_protocol": _AUDIT_STATE_PROTOCOL,
            "graph_run_id": graph_run_id,
            "plan": plan,
            "plan_digest": plan_digest,
            "base_commit": base_commit,
            "plan_graph_digest": _plan_graph_digest(
                plan=plan,
                plan_digest=plan_digest,
                base_commit=base_commit,
                nodes=nodes,
                functionality_tests=functionality_tests,
            ),
            "current_candidate_commit": base_commit,
            "ordered_node_ids": list(nodes),
            "nodes": {key: dict(value) for key, value in nodes.items()},
            "current_node_id": None,
            # These are controller-owned scheduling facts.  They deliberately
            # live beside the legacy sequential state so a later parallel
            # scheduler can use one audited graph identity without changing
            # existing PlanGraph callers.
            "active_node_ids": [],
            "successor_attempts": [],
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
                "path": str(Path.cwd().resolve()),
                "base_branch": "unavailable",
                "base_commit": base_commit,
            },
            "approved_plan": {"path": plan, "sha256": plan_digest},
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

    def reserve_successor_attempt(
        self,
        *,
        node_id: str,
        logical_attempt: int,
        allocation_id: str,
        parent_candidate_commit: str,
        expected_revision: int,
        expected_staging_head: str,
    ) -> dict[str, Any]:
        """Reserve one immutable child allocation by checkpoint CAS.

        This compatibility-sized wrapper deliberately has the same semantics as
        :meth:`reserve_successor_attempt_batch`.  Parallel schedulers must use
        the batch API so every sibling sharing a logical attempt is accepted by
        one CAS transition.
        """

        return self.reserve_successor_attempt_batch(
            allocations=({"node_id": node_id, "allocation_id": allocation_id},),
            logical_attempt=logical_attempt,
            parent_candidate_commit=parent_candidate_commit,
            expected_revision=expected_revision,
            expected_staging_head=expected_staging_head,
        )[0]

    def reserve_successor_attempt_batch(
        self,
        *,
        allocations: tuple[Mapping[str, object], ...],
        logical_attempt: int,
        parent_candidate_commit: str,
        expected_revision: int,
        expected_staging_head: str,
    ) -> list[dict[str, Any]]:
        """Atomically reserve sibling allocations for one logical attempt.

        Every allocation in a batch binds the same revision, staging parent,
        and attempt number.  A stale contender produces no durable event.
        """

        if not allocations:
            raise ValueError("successor-attempt batch must not be empty")
        if logical_attempt < 1:
            raise ValueError("logical_attempt must be positive")
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if not _is_git_commit(parent_candidate_commit):
            raise ValueError("parent_candidate_commit must be a full lowercase Git commit")
        if not _is_git_commit(expected_staging_head):
            raise ValueError("expected_staging_head must be a full lowercase Git commit")
        if parent_candidate_commit != expected_staging_head:
            raise ValueError("parent_candidate_commit must equal expected_staging_head")
        # Reject a stale contender before inspecting mutable graph details.  The
        # same head is checked again inside the journal's interprocess CAS.
        expected_head_hash = self._checkpoint_head(expected_revision)

        normalized = []
        for item in allocations:
            node_id = item.get("node_id") if isinstance(item, Mapping) else None
            allocation_id = (
                item.get("allocation_id") if isinstance(item, Mapping) else None
            )
            if not isinstance(node_id, str) or not node_id:
                raise ValueError("successor-attempt node_id must be non-empty")
            if not isinstance(allocation_id, str) or not allocation_id:
                raise ValueError("successor-attempt allocation_id must be non-empty")
            normalized.append((node_id, allocation_id))
        node_ids = [node_id for node_id, _ in normalized]
        allocation_ids = [allocation_id for _, allocation_id in normalized]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("successor-attempt batch repeats a node_id")
        if len(allocation_ids) != len(set(allocation_ids)):
            raise ValueError("successor-attempt batch repeats an allocation_id")

        state = self.state
        if state.get("current_candidate_commit") != expected_staging_head:
            raise AuditConflictError("PlanGraph staging head changed")
        nodes = state.get("nodes")
        if not isinstance(nodes, dict):
            raise AuditError("PlanGraph checkpoint nodes are invalid")
        for node_id in node_ids:
            node = nodes.get(node_id)
            if not isinstance(node, dict):
                raise AuditError(f"PlanGraph checkpoint has no node {node_id!r}")
            if node.get("status") not in {None, "queued"}:
                raise AuditError(f"PlanGraph node {node_id!r} is not queued")

        attempts = state.setdefault("successor_attempts", [])
        if not isinstance(attempts, list) or not all(
            isinstance(item, dict) for item in attempts
        ):
            raise AuditError("PlanGraph successor-attempt evidence is invalid")
        if any(item.get("allocation_id") in allocation_ids for item in attempts):
            raise AuditError("PlanGraph allocation_id was already reserved")
        if any(item.get("node_id") in node_ids for item in attempts):
            raise AuditError("PlanGraph node already has a successor attempt")
        prior_attempts = [item.get("logical_attempt") for item in attempts]
        if any(not isinstance(item, int) for item in prior_attempts):
            raise AuditError("PlanGraph successor-attempt number is invalid")
        if prior_attempts and logical_attempt != max(prior_attempts) + 1:
            raise AuditError("PlanGraph logical_attempt must advance by one batch")
        active = state.setdefault("active_node_ids", [])
        if not isinstance(active, list) or not all(isinstance(item, str) for item in active):
            raise AuditError("PlanGraph active node state is invalid")
        if any(node_id in active for node_id in node_ids):
            raise AuditError("PlanGraph node is already active")

        evidence = []
        for node_id, allocation_id in normalized:
            allocation = {
                "protocol": "harness-plan-graph-parallel-allocation/1",
                "graph_id": self.graph_run_id,
                "node_id": node_id,
                "logical_attempt": logical_attempt,
                "allocation_id": allocation_id,
                "checkpoint_revision": expected_revision,
                "expected_staging_head": expected_staging_head,
            }
            attempt = {**allocation, "parent_candidate_commit": parent_candidate_commit}
            evidence.append(attempt)
            attempts.append(attempt)
            active.append(node_id)
            node = nodes[node_id]
            node["status"] = "reserved"
            node["parent_candidate_commit"] = parent_candidate_commit
            node["allocation_id"] = allocation_id
            node["logical_attempt"] = logical_attempt

        committed = self.journal.compare_and_swap_checkpoint(
            expected_revision=expected_revision,
            expected_head_hash=expected_head_hash,
            status="running",
            state=state,
            event_type="plan_graph_successor_attempts_reserved",
            event_status="reserved",
            payload={
                "logical_attempt": logical_attempt,
                "parent_candidate_commit": parent_candidate_commit,
                "allocations": evidence,
            },
            actor=_ACTOR,
        )
        return [
            {
                **attempt,
                "successor_checkpoint_revision": committed["checkpoint"]["revision"],
                "event_hash": committed["event"]["event_hash"],
            }
            for attempt in evidence
        ]

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
            if state.get("audit_state_protocol") != _AUDIT_STATE_PROTOCOL:
                raise AuditError(
                    "existing PlanGraph checkpoint is legacy-incompatible; "
                    "create a versioned migration record before resuming"
                )
            if state.get("graph_run_id") != self.graph_run_id:
                raise AuditError("existing audit directory is not this PlanGraph")
            if state.get("plan_graph_digest") != self._initial_state["plan_graph_digest"]:
                raise AuditError(
                    "existing PlanGraph checkpoint does not match the supplied plan"
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

    def _checkpoint_head(self, expected_revision: int) -> str | None:
        """Read the head coupled to the caller's expected checkpoint revision."""

        checkpoint = json.loads(self.journal.checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("revision") != expected_revision:
            raise AuditConflictError("audit checkpoint revision changed")
        head = checkpoint.get("head_hash")
        if head is not None and not isinstance(head, str):
            raise AuditError("audit checkpoint head is invalid")
        return head


def _plan_digest(plan: str) -> str | None:
    path = Path(plan)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _plan_graph_digest(
    *,
    plan: str,
    plan_digest: str,
    base_commit: str,
    nodes: Mapping[str, Mapping[str, object]],
    functionality_tests: tuple[str, ...],
) -> str:
    """Bind a checkpoint to the complete supplied decomposition."""

    payload = {
        "plan": plan,
        "plan_digest": plan_digest,
        "base_commit": base_commit,
        "nodes": {key: dict(value) for key, value in nodes.items()},
        "functionality_tests": list(functionality_tests),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _is_git_commit(value: object) -> bool:
    return isinstance(value, str) and bool(_GIT_COMMIT.fullmatch(value))


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
