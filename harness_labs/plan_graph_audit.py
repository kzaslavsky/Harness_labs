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
from typing import Any, Callable, Mapping, Sequence

from .audit import AuditActor, AuditConflictError, AuditError, AuditJournal


_ACTOR = AuditActor("plan-graph", "plan_graph_controller")
_TERMINAL = frozenset({"succeeded", "failed", "blocked", "interrupted"})
# /1 did not require the immutable-attempt fields below.  It must remain
# explicitly incompatible rather than being reinterpreted during resume.
_AUDIT_STATE_PROTOCOL = "harness-plan-graph-audit/2"
_GIT_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_ARTIFACT_REF = re.compile(r"^artifact:sha256:[a-f0-9]{64}$")
_CHILD_LIVENESS_NAMES = ("plan-graph-liveness.json", "liveness.json")
_CHILD_SEAL_NAMES = ("plan-graph-seal-receipt.json", "seal-receipt.json")


class PlanGraphAudit:
    """Own the durable identity, descriptor, events, and checkpoint of a graph."""

    @staticmethod
    def repair_contract_digest(
        *, plan: str, base_commit: str, nodes: Mapping[str, Mapping[str, object]],
        functionality_tests: tuple[str, ...], plan_sections: Mapping[str, str],
        acceptance_criteria: Mapping[str, str],
    ) -> str:
        """Digest only immutable execution inputs used to authorize reuse."""
        plan_digest = _plan_digest(plan)
        if plan_digest is None:
            raise ValueError("durable PlanGraph requires a readable approved plan")
        return _plan_graph_digest(plan=plan, plan_digest=plan_digest, base_commit=base_commit,
                                  nodes=nodes, functionality_tests=functionality_tests,
                                  plan_sections=plan_sections,
                                  acceptance_criteria=acceptance_criteria)

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
        plan_sections: Mapping[str, str] | None = None,
        acceptance_criteria: Mapping[str, str] | None = None,
        logical_graph_id: str | None = None,
        graph_attempt_id: str | None = None,
        predecessor_attempt_id: str | None = None,
        resume_directive: object | None = None,
        predecessor_checkpoint: Mapping[str, object] | None = None,
    ) -> None:
        if (
            not graph_run_id
            or graph_run_id in {".", ".."}
            or "/" in graph_run_id
            or "\\" in graph_run_id
        ):
            raise ValueError("graph_run_id must be a non-empty path-safe name")
        self.graph_run_id = graph_run_id
        self.logical_graph_id = logical_graph_id or graph_run_id
        self.graph_attempt_id = graph_attempt_id or graph_run_id
        self.predecessor_attempt_id = predecessor_attempt_id
        self.resume_directive = resume_directive
        self.predecessor_checkpoint = dict(predecessor_checkpoint) if predecessor_checkpoint is not None else None
        self.run_dir = (run_root / graph_run_id).resolve()
        plan_digest = _plan_digest(plan)
        if plan_digest is None:
            raise ValueError("durable PlanGraph requires a readable approved plan")
        plan_graph_digest = _plan_graph_digest(
            plan=plan,
            plan_digest=plan_digest,
            base_commit=base_commit,
            nodes=nodes,
            functionality_tests=functionality_tests,
            plan_sections=plan_sections or {},
            acceptance_criteria=acceptance_criteria or {},
        )
        self._initial_state = {
            "audit_state_protocol": _AUDIT_STATE_PROTOCOL,
            "graph_run_id": graph_run_id,
            # A graph-run directory is one execution attempt.  Keep the
            # logical graph identity separate so a successor can prove which
            # immutable decomposition it continues without consulting
            # controller liveness or an ambient branch head.
            "logical_graph": {
                "protocol": "harness-plan-graph-logical-graph/1",
                "logical_graph_id": self.logical_graph_id,
                "plan_digest": plan_digest,
                "base_commit": base_commit,
            },
            "graph_attempt": {
                "graph_attempt_id": self.graph_attempt_id,
                "predecessor_attempt_id": self.predecessor_attempt_id,
            },
            "plan": plan,
            "plan_digest": plan_digest,
            "base_commit": base_commit,
            "plan_graph_digest": plan_graph_digest,
            "current_candidate_commit": base_commit,
            "ordered_node_ids": list(nodes),
            "nodes": {
                key: {
                    **dict(value),
                    "input_commit": None,
                    "integrated_commit": None,
                }
                for key, value in nodes.items()
            },
            "current_node_id": None,
            # These are controller-owned scheduling facts.  They deliberately
            # live beside the legacy sequential state so a later parallel
            # scheduler can use one audited graph identity without changing
            # existing PlanGraph callers.
            "active_node_ids": [],
            "successor_attempts": [],
            # Append-only records.  No liveness observation belongs in any of
            # these structures: liveness remains child-owned and ephemeral.
            "attempt_lineage": [],
            "integration_barriers": [],
            "retry_state": {
                "invalidations": [],
                "reuse": [],
            },
            "repair_resume": self._resume_state(),
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
            "logical_graph_id": self.logical_graph_id,
            "graph_attempt_id": self.graph_attempt_id,
            "predecessor_attempt_id": self.predecessor_attempt_id,
        }
        self.journal = self._open_or_create()

    @classmethod
    def open_repair_predecessor(
        cls, *, run_root: Path, graph_run_id: str, plan: str, base_commit: str,
        logical_graph_id: str, plan_graph_digest: str,
    ) -> "PlanGraphAudit":
        """Open a finalized failed attempt read-only after full journal verification."""
        run_dir = (run_root / graph_run_id).resolve()
        journal = AuditJournal.open_existing(run_dir, actor=_ACTOR)
        AuditJournal.verify(run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        descriptor = json.loads((run_dir / "descriptor.json").read_text(encoding="utf-8"))
        state = journal.checkpoint_state()
        if (manifest.get("status") not in {"failed", "blocked"}
                or state.get("terminal_graph_status") not in {"failed", "blocked"}
                or descriptor.get("logical_graph_id", graph_run_id) != logical_graph_id
                or descriptor.get("graph_attempt_id", graph_run_id) != graph_run_id
                or descriptor.get("approved_plan", {}).get("sha256") != _plan_digest(plan)
                or descriptor.get("repository", {}).get("base_commit") != base_commit
                or state.get("plan_graph_digest") != plan_graph_digest):
            raise AuditError("predecessor is not a matching failed or blocked attempt")
        instance = cls.__new__(cls)
        instance.graph_run_id = graph_run_id
        instance.logical_graph_id = logical_graph_id
        instance.graph_attempt_id = graph_run_id
        instance.predecessor_attempt_id = None
        instance.resume_directive = None
        instance.predecessor_checkpoint = None
        instance.run_dir = run_dir
        instance.descriptor = descriptor
        instance.journal = journal
        return instance

    def repair_selection(self, *, retry_frontier: Sequence[str], blocker_evidence_ref: str) -> dict[str, object]:
        """Return the retry closure and only custody-proven reusable predecessors."""
        if not _ARTIFACT_REF.fullmatch(blocker_evidence_ref):
            raise ValueError("repair resume requires a sha256 blocker evidence reference")
        if not self._has_recorded_artifact(blocker_evidence_ref):
            raise AuditError("repair blocker evidence is not recorded by the predecessor")
        state = self.state
        nodes = state.get("nodes")
        if not isinstance(nodes, dict) or not all(isinstance(key, str) and isinstance(node, dict) for key, node in nodes.items()):
            raise AuditError("predecessor checkpoint nodes are invalid")
        frontier = tuple(retry_frontier)
        if not frontier or len(frontier) != len(set(frontier)) or any(not isinstance(node_id, str) or node_id not in nodes for node_id in frontier):
            raise ValueError("repair resume requires an explicit retry frontier")
        invalidated = set(frontier)
        changed = True
        while changed:
            changed = False
            for node_id, node in nodes.items():
                dependencies = node.get("depends_on")
                if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
                    raise AuditError("predecessor node dependencies are invalid")
                if node_id not in invalidated and any(item in invalidated for item in dependencies):
                    invalidated.add(node_id)
                    changed = True
        reusable: dict[str, str] = {}
        barriers = state.get("integration_barriers")
        if not isinstance(barriers, list) or not all(isinstance(item, dict) for item in barriers):
            raise AuditError("predecessor integration custody evidence is invalid")
        for node_id, node in nodes.items():
            if node_id in invalidated or node.get("status") != "succeeded":
                continue
            candidate, dependencies = node.get("candidate_commit"), node.get("depends_on")
            # A successful node is reusable only when the predecessor's
            # controller-owned integration barrier binds that exact candidate
            # to its recorded input.  Child status alone is never sufficient.
            custody_matches = any(
                barrier.get("node_id") == node_id
                and barrier.get("integrated_commit") == candidate
                and _is_git_commit(barrier.get("input_commit"))
                for barrier in barriers
            )
            if _is_git_commit(candidate) and custody_matches and isinstance(dependencies, list) and all(dependency in reusable for dependency in dependencies):
                reusable[node_id] = candidate
        return {"retry_frontier": frontier, "invalidated_node_ids": tuple(node_id for node_id in nodes if node_id in invalidated), "reused_completed": reusable, "predecessor_checkpoint": json.loads(self.journal.checkpoint_path.read_text(encoding="utf-8"))}

    def _has_recorded_artifact(self, reference: str) -> bool:
        digest = reference.removeprefix("artifact:sha256:")
        try:
            return any(any(isinstance(artifact, dict) and artifact.get("sha256") == digest for artifact in event.get("artifacts", [])) for event in (json.loads(line) for line in self.journal.events_path.read_text(encoding="utf-8").splitlines()))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AuditError("predecessor artifact inventory is invalid") from exc

    @property
    def state(self) -> dict[str, Any]:
        return self.journal.checkpoint_state()

    @property
    def terminal(self) -> bool:
        return self.state.get("terminal_graph_status") in _TERMINAL

    def node_started(self, node_id: str) -> None:
        state = self.state
        nodes = state.get("nodes")
        if isinstance(nodes, dict) and isinstance(nodes.get(node_id), dict):
            # Legacy sequential callers have no explicit reservation.  Bind
            # their input before launch while retaining the same public API.
            input_commit = nodes[node_id].get("input_commit")
            if input_commit is None:
                input_commit = state.get("current_candidate_commit")
        else:
            input_commit = None
        self._transition(
            "plan_node_started",
            "running",
            node_id,
            {
                "status": "running",
                "started_at": _timestamp(),
                "input_commit": input_commit,
            },
        )

    def node_completed(self, node_id: str, candidate_commit: str) -> None:
        state = self.state
        nodes = state.get("nodes")
        if not isinstance(nodes, dict) or not isinstance(nodes.get(node_id), dict):
            raise AuditError(f"PlanGraph checkpoint has no node {node_id!r}")
        node = nodes[node_id]
        input_commit = node.get("input_commit")
        if input_commit is None:
            input_commit = state.get("current_candidate_commit")
        barrier = {
            "barrier_id": _barrier_id(node_id, node.get("allocation_id")),
            "node_id": node_id,
            "attempt_id": _attempt_id(self.graph_run_id, node.get("allocation_id")),
            "input_commit": input_commit,
            "integrated_commit": candidate_commit,
        }
        barriers = state.get("integration_barriers")
        if not isinstance(barriers, list) or not all(isinstance(item, dict) for item in barriers):
            raise AuditError("PlanGraph integration-barrier evidence is invalid")
        if not any(item.get("barrier_id") == barrier["barrier_id"] for item in barriers):
            barriers.append(barrier)
        # A serial node-completion record lacks the protected-ref CAS witness
        # and verified evidence reference required by the integration-receipt
        # contract.  It is therefore barrier context only, never a receipt.
        self._transition(
            "plan_node_completed",
            "succeeded",
            node_id,
            {
                "status": "succeeded",
                "finished_at": _timestamp(),
                "candidate_commit": candidate_commit,
                "input_commit": input_commit,
                "integrated_commit": candidate_commit,
            },
            current_candidate_commit=candidate_commit,
            integration_barriers=barriers,
        )

    def node_failed(self, node_id: str, status: str, evidence: object | None) -> None:
        artifact = self.journal.write_artifact(
            "plan-graph-node-failure-evidence",
            {"plan_node_id": node_id, "status": status, "evidence": evidence},
        )
        self._transition(
            "plan_node_failed",
            status,
            node_id,
            {"status": status, "finished_at": _timestamp(),
             "evidence": {"evidence_ref": f"artifact:sha256:{artifact.sha256}"}},
            artifacts=(artifact,),
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
        lineage = state.setdefault("attempt_lineage", [])
        if not isinstance(lineage, list) or not all(isinstance(item, dict) for item in lineage):
            raise AuditError("PlanGraph attempt lineage is invalid")
        barriers = state.setdefault("integration_barriers", [])
        if not isinstance(barriers, list) or not all(isinstance(item, dict) for item in barriers):
            raise AuditError("PlanGraph integration-barrier evidence is invalid")
        retry_state = state.setdefault("retry_state", {"invalidations": [], "reuse": []})
        if (
            not isinstance(retry_state, dict)
            or set(retry_state) != {"invalidations", "reuse"}
            or not all(isinstance(retry_state[key], list) for key in retry_state)
            or not all(isinstance(item, dict) for key in retry_state for item in retry_state[key])
        ):
            raise AuditError("PlanGraph retry state is invalid")
        if any(item.get("allocation_id") in allocation_ids for item in attempts):
            raise AuditError("PlanGraph allocation_id was already reserved")
        invalidated_attempt_ids = {
            item.get("attempt_id") for item in retry_state["invalidations"]
        }
        outstanding_nodes = {
            item.get("node_id")
            for item in lineage
            if item.get("attempt_id") not in invalidated_attempt_ids
        }
        if any(node_id in outstanding_nodes for node_id in node_ids):
            raise AuditError("PlanGraph node already has a live successor attempt")
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
            attempt_id = _attempt_id(self.graph_run_id, allocation_id)
            predecessors = [
                item for item in lineage
                if item.get("node_id") == node_id
                and item.get("attempt_id") in invalidated_attempt_ids
            ]
            predecessor_attempt_id = (
                predecessors[-1]["attempt_id"] if predecessors else None
            )
            lineage_record = {
                "attempt_id": attempt_id,
                "node_id": node_id,
                "logical_attempt": logical_attempt,
                "allocation_id": allocation_id,
                "input_commit": parent_candidate_commit,
                "predecessor_attempt_id": predecessor_attempt_id,
            }
            evidence.append(attempt)
            attempts.append(attempt)
            lineage.append(lineage_record)
            barriers.append(
                {
                    "barrier_id": _barrier_id(node_id, allocation_id),
                    "node_id": node_id,
                    "attempt_id": attempt_id,
                    "input_commit": parent_candidate_commit,
                    "expected_staging_head": expected_staging_head,
                }
            )
            if predecessor_attempt_id is not None:
                retry_state["reuse"].append(
                    {
                        "node_id": node_id,
                        "reused_from_attempt_id": predecessor_attempt_id,
                        "replacement_attempt_id": attempt_id,
                    }
                )
            active.append(node_id)
            node = nodes[node_id]
            node["status"] = "reserved"
            node["parent_candidate_commit"] = parent_candidate_commit
            node["allocation_id"] = allocation_id
            node["logical_attempt"] = logical_attempt
            node["input_commit"] = parent_candidate_commit
            node["integrated_commit"] = None

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
                "attempt_lineage": [
                    item for item in lineage if item.get("allocation_id") in allocation_ids
                ],
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

    def reconcile_interrupted_attempts(
        self,
        *,
        process_probe: Callable[[int], str | None],
        force_records: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, str]:
        """Reconcile active allocations from child-owned evidence only.

        The graph checkpoint records the reservation, never child liveness.  A
        matching live PID/token retains the allocation.  A dead child can be
        sealed only through its verified terminal manifest, the current closed
        child-request descriptor, and its exact allocation-bound seal receipt.
        All other observations are blocked rather than redispatched.
        """

        if not callable(process_probe):
            raise ValueError("process_probe must be callable")
        state = self.state
        nodes, attempts, active = (
            state.get("nodes"), state.get("successor_attempts"), state.get("active_node_ids")
        )
        if not isinstance(nodes, dict) or not isinstance(attempts, list) or not isinstance(active, list):
            raise AuditError("PlanGraph recovery checkpoint is invalid")
        if not all(isinstance(item, dict) for item in attempts) or not all(isinstance(node_id, str) for node_id in active):
            raise AuditError("PlanGraph recovery checkpoint has invalid allocations")
        forced = self._force_records(force_records, attempts, nodes, active)
        outcomes: dict[str, str] = {}
        for attempt in attempts:
            node_id = attempt.get("node_id")
            if not isinstance(node_id, str) or node_id not in active:
                continue
            node = nodes.get(node_id)
            if not isinstance(node, dict) or node.get("status") not in {"reserved", "running"}:
                raise AuditError(f"PlanGraph recovery node {node_id!r} is not active")
            liveness = self._liveness_disposition(self._child_liveness(attempt, node), process_probe)
            if liveness == "running":
                node["status"] = "running"
                outcomes[node_id] = "running"
                continue
            proof = self._child_seal_proof(attempt, node)
            force = forced.get(
                (node_id, attempt.get("logical_attempt"), attempt.get("allocation_id"))
            )
            if force is not None:
                if force["disposition"] == "sealed" and proof is not None:
                    self._adopt_seal(state, node_id, proof, force["evidence_ref"], forced=True)
                    outcomes[node_id] = "sealed"
                else:
                    if proof is not None:
                        self._quarantine_late_manifest(node_id, proof, force["evidence_ref"])
                    self._block_recovery_node(state, node_id, "force_reconcile", force["evidence_ref"])
                    outcomes[node_id] = "blocked"
            elif liveness == "dead" and proof is not None:
                self._adopt_seal(state, node_id, proof, None, forced=False)
                outcomes[node_id] = "sealed"
            else:
                self._block_recovery_node(state, node_id, "ambiguous_child_identity", None)
                outcomes[node_id] = "blocked"
        if outcomes:
            self.journal.append("plan_graph_interrupted_attempts_reconciled", status="running", payload={"outcomes": outcomes}, actor=_ACTOR)
            self.journal.checkpoint("running", state)
        return outcomes

    @staticmethod
    def _liveness_disposition(liveness: Mapping[str, object] | None, process_probe: Callable[[int], str | None]) -> str:
        if liveness is None:
            return "ambiguous"
        try:
            token = process_probe(liveness["pid"])
        except Exception:
            return "ambiguous"
        matches = token == liveness["process_start_token"]
        if liveness["state"] == "live" and matches:
            return "running"
        if liveness["state"] == "dead" and not matches:
            return "dead"
        return "ambiguous"

    def _force_records(
        self,
        records: Sequence[Mapping[str, object]],
        attempts: Sequence[Mapping[str, object]],
        nodes: Mapping[str, object],
        active: Sequence[str],
    ) -> dict[tuple[str, object, object], dict[str, str]]:
        """Accept only evidence-backed records for the allocation still active now."""

        active_allocations = {
            (node_id, attempt.get("logical_attempt"), attempt.get("allocation_id"))
            for attempt in attempts
            if isinstance((node_id := attempt.get("node_id")), str)
            and node_id in active
            and isinstance((node := nodes.get(node_id)), Mapping)
            and node.get("status") in {"reserved", "running"}
            and node.get("logical_attempt") == attempt.get("logical_attempt")
            and node.get("allocation_id") == attempt.get("allocation_id")
        }
        result: dict[tuple[str, object, object], dict[str, str]] = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("force-reconcile record must be an object")
            node_id = record.get("node_id")
            if (set(record) != {"protocol", "graph_id", "node_id", "logical_attempt", "allocation_id", "disposition", "evidence_ref"}
                or record.get("protocol") != "harness-plan-graph-parallel-force-reconcile/1"
                or record.get("graph_id") != self.graph_run_id
                or (node_id, record.get("logical_attempt"), record.get("allocation_id")) not in active_allocations
                or not isinstance(node_id, str) or record.get("disposition") not in {"blocked", "sealed"}
                or not isinstance(record.get("evidence_ref"), str) or not _ARTIFACT_REF.fullmatch(record["evidence_ref"])
                or not self._force_evidence_is_durable(record["evidence_ref"])
                or (node_id, record.get("logical_attempt"), record.get("allocation_id")) in result):
                raise ValueError("force-reconcile record does not match an active allocation")
            result[(node_id, record["logical_attempt"], record["allocation_id"])] = {
                "disposition": record["disposition"],
                "evidence_ref": record["evidence_ref"],
            }
        return result

    def _force_evidence_is_durable(self, evidence_ref: str) -> bool:
        """Verify that force evidence is an artifact already bound to this journal."""

        try:
            AuditJournal.verify(self.run_dir)
            for line in self.journal.events_path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                artifacts = event.get("artifacts") if isinstance(event, dict) else None
                if not isinstance(artifacts, list):
                    return False
                if any(
                    isinstance(artifact, dict)
                    and f"artifact:sha256:{artifact.get('sha256')}" == evidence_ref
                    for artifact in artifacts
                ):
                    return True
        except (AuditError, OSError, ValueError, json.JSONDecodeError):
            return False
        return False

    def _child_liveness(self, attempt: Mapping[str, object], node: Mapping[str, object]) -> dict[str, object] | None:
        raw = self._child_object(node, _CHILD_LIVENESS_NAMES)
        required = {"protocol", "graph_id", "node_id", "logical_attempt", "allocation_id", "pid", "process_start_token", "state"}
        if (raw is None or set(raw) != required or raw.get("protocol") != "harness-plan-graph-parallel-liveness/1"
            or any(raw.get(key) != attempt.get(key) for key in ("graph_id", "node_id", "logical_attempt", "allocation_id"))
            or type(raw.get("pid")) is not int or raw["pid"] < 1
            or not isinstance(raw.get("process_start_token"), str) or not raw["process_start_token"]
            or raw.get("state") not in {"live", "dead", "unavailable", "ambiguous"}):
            return None
        return raw

    def _child_seal_proof(self, attempt: Mapping[str, object], node: Mapping[str, object]) -> dict[str, object] | None:
        run_dir = self._child_run_dir(node)
        if run_dir is None:
            return None
        try:
            AuditJournal.verify(run_dir)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            receipt = self._child_object(node, _CHILD_SEAL_NAMES)
            evidence = self._child_evidence(run_dir)
        except (AuditError, OSError, ValueError, json.JSONDecodeError):
            return None
        required = {"protocol", "status", "graph_id", "node_id", "logical_attempt", "allocation_id", "parent_candidate_commit", "candidate_commit", "canonical_manifest_ref", "descriptor_ref", "verification_evidence_ref", "candidate_receipt_ref", "terminal_journal_event_ref"}
        manifest_hash = manifest.get("manifest_hash") if isinstance(manifest, dict) else None
        if (not isinstance(receipt, dict) or (set(receipt) != required and set(receipt) != required | {"stdout_artifact_ref"})
            or not isinstance(manifest, dict) or manifest.get("status") != "succeeded" or manifest.get("run_id") != node.get("feature_run_id")
            or not isinstance(manifest_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", manifest_hash)
            or receipt.get("protocol") != "harness-plan-graph-parallel-seal-receipt/1" or receipt.get("status") != "sealed"
            or any(receipt.get(key) != attempt.get(key) for key in ("graph_id", "node_id", "logical_attempt", "allocation_id", "parent_candidate_commit"))
            or not _is_git_commit(receipt.get("candidate_commit"))
            or receipt.get("canonical_manifest_ref") != f"artifact:sha256:{manifest_hash}"
            or not self._seal_evidence_matches(receipt, attempt, evidence, manifest.get("head_hash"))):
            return None
        return {"receipt": receipt, "manifest_hash": manifest_hash}

    @staticmethod
    def _child_evidence(run_dir: Path) -> dict[str, bytes] | None:
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            inventory = manifest.get("artifacts") if isinstance(manifest, dict) else None
            if not isinstance(inventory, list):
                return None
            evidence: dict[str, bytes] = {}
            for item in inventory:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                    return None
                path = (run_dir / item["path"]).resolve()
                path.relative_to((run_dir / "artifacts").resolve())
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    return None
                evidence[f"artifact:sha256:{item['sha256']}"] = raw
            return evidence
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _seal_evidence_matches(receipt: Mapping[str, object], attempt: Mapping[str, object], evidence: Mapping[str, bytes] | None, terminal_event_hash: object) -> bool:
        if evidence is None or not isinstance(terminal_event_hash, str):
            return False
        refs = ("descriptor_ref", "verification_evidence_ref", "candidate_receipt_ref")
        if any(not isinstance(receipt.get(key), str) or receipt[key] not in evidence for key in refs) or receipt.get("terminal_journal_event_ref") != f"artifact:sha256:{terminal_event_hash}":
            return False
        try:
            descriptor = json.loads(evidence[receipt["descriptor_ref"]])
            verification = json.loads(evidence[receipt["verification_evidence_ref"]])
            candidate = json.loads(evidence[receipt["candidate_receipt_ref"]])
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        allocation = descriptor.get("allocation") if isinstance(descriptor, dict) else None
        dependencies = descriptor.get("dependency_candidates") if isinstance(descriptor, dict) else None
        lane = descriptor.get("lane") if isinstance(descriptor, dict) else None
        return (isinstance(descriptor, dict) and set(descriptor) == {"protocol", "graph_id", "node_id", "allocation", "parent_candidate_commit", "dependency_candidates", "lane", "writable_paths"}
            and descriptor.get("protocol") == "harness-plan-graph-parallel-child-request/1"
            and all(descriptor.get(key) == attempt.get(key) for key in ("graph_id", "node_id", "parent_candidate_commit"))
            and isinstance(allocation, dict) and set(allocation) == {"batch_id", "logical_attempt", "allocation_id", "checkpoint_revision", "expected_staging_head"}
            and isinstance(allocation.get("batch_id"), str) and bool(allocation["batch_id"])
            and all(allocation.get(key) == attempt.get(key) for key in ("logical_attempt", "allocation_id", "checkpoint_revision", "expected_staging_head"))
            and isinstance(dependencies, list) and len({item.get("node_id") for item in dependencies if isinstance(item, dict)}) == len(dependencies)
            and all(isinstance(item, dict) and set(item) == {"node_id", "candidate_commit", "seal_receipt_ref"} and isinstance(item.get("node_id"), str) and bool(item["node_id"]) and _is_git_commit(item.get("candidate_commit")) and isinstance(item.get("seal_receipt_ref"), str) and bool(_ARTIFACT_REF.fullmatch(item["seal_receipt_ref"])) for item in dependencies)
            and isinstance(lane, dict) and set(lane) == {"branch", "worktree", "may_advance_staging"} and isinstance(lane.get("branch"), str) and bool(lane["branch"]) and isinstance(lane.get("worktree"), str) and bool(lane["worktree"]) and lane.get("may_advance_staging") is False
            and isinstance(descriptor.get("writable_paths"), list) and bool(descriptor["writable_paths"]) and len(descriptor["writable_paths"]) == len(set(descriptor["writable_paths"])) and all(isinstance(path, str) and path for path in descriptor["writable_paths"])
            and isinstance(verification, dict) and verification.get("exit_code") == 0
            and isinstance(candidate, dict) and candidate.get("operation") == "commit" and candidate.get("candidate_commit") == receipt.get("candidate_commit"))

    @staticmethod
    def _child_run_dir(node: Mapping[str, object]) -> Path | None:
        value = node.get("run_dir")
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        return path.resolve() if path.is_dir() and not path.is_symlink() else None

    def _child_object(self, node: Mapping[str, object], names: Sequence[str]) -> dict[str, object] | None:
        run_dir = self._child_run_dir(node)
        if run_dir is None:
            return None
        present = [run_dir / name for name in names if (run_dir / name).exists()]
        if len(present) != 1 or present[0].is_symlink() or not present[0].is_file():
            return None
        try:
            value = json.loads(present[0].read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _adopt_seal(self, state: dict[str, Any], node_id: str, proof: Mapping[str, object], force_evidence_ref: str | None, *, forced: bool) -> None:
        receipt = proof["receipt"]
        assert isinstance(receipt, Mapping)
        node = state["nodes"][node_id]
        assert isinstance(node, dict)
        # A child seal proves its lane candidate.  It never advances the graph
        # staging head; only the join integration barrier owns that custody.
        node.update({"status": "succeeded", "candidate_commit": receipt["candidate_commit"], "finished_at": _timestamp(), "integrated_commit": None})
        state["active_node_ids"].remove(node_id)
        self.journal.append("plan_graph_child_seal_adopted", status="succeeded", payload={"plan_node_id": node_id, "seal_receipt": dict(receipt), "force_evidence_ref": force_evidence_ref, "forced": forced}, actor=_ACTOR)

    def _block_recovery_node(self, state: dict[str, Any], node_id: str, reason: str, evidence_ref: str | None) -> None:
        node = state["nodes"][node_id]
        assert isinstance(node, dict)
        node.update({"status": "blocked", "finished_at": _timestamp(), "evidence": {"reason": reason, "evidence_ref": evidence_ref}})
        state["active_node_ids"].remove(node_id)
        self.journal.append("plan_graph_child_recovery_blocked", status="blocked", payload={"plan_node_id": node_id, "reason": reason, "evidence_ref": evidence_ref}, actor=_ACTOR)

    def _quarantine_late_manifest(self, node_id: str, proof: Mapping[str, object], force_evidence_ref: str) -> None:
        artifact = self.journal.write_artifact("late-plan-graph-child-manifest", {"node_id": node_id, "manifest_hash": proof["manifest_hash"], "seal_receipt": proof["receipt"]})
        self.journal.append("plan_graph_late_manifest_quarantined", status="blocked", payload={"plan_node_id": node_id, "force_evidence_ref": force_evidence_ref}, actor=_ACTOR, artifacts=(artifact,))

    def invalidate_successor_attempt(
        self,
        *,
        allocation_id: str,
        reason: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Record a retry decision without reusing or rewriting an attempt.

        The controller must explicitly invalidate a stopped allocation before
        the node can receive a replacement allocation.  This method does not
        inspect child liveness and therefore cannot turn liveness into durable
        scheduling authority.
        """

        if not isinstance(allocation_id, str) or not allocation_id:
            raise ValueError("allocation_id must be non-empty")
        if not isinstance(reason, str) or not reason:
            raise ValueError("invalidation reason must be non-empty")
        expected_head_hash = self._checkpoint_head(expected_revision)
        state = self.state
        lineage = state.get("attempt_lineage")
        retry_state = state.get("retry_state")
        nodes = state.get("nodes")
        active = state.get("active_node_ids")
        if (
            not isinstance(lineage, list)
            or not isinstance(retry_state, dict)
            or not isinstance(retry_state.get("invalidations"), list)
            or not isinstance(nodes, dict)
            or not isinstance(active, list)
        ):
            raise AuditError("PlanGraph retry checkpoint is invalid")
        matches = [item for item in lineage if item.get("allocation_id") == allocation_id]
        if len(matches) != 1 or not isinstance(matches[0].get("attempt_id"), str):
            raise AuditError("PlanGraph allocation has no immutable lineage record")
        attempt = matches[0]
        attempt_id = attempt["attempt_id"]
        if any(item.get("attempt_id") == attempt_id for item in retry_state["invalidations"]):
            raise AuditError("PlanGraph attempt was already invalidated")
        node_id = attempt.get("node_id")
        if not isinstance(node_id, str) or not isinstance(nodes.get(node_id), dict):
            raise AuditError("PlanGraph invalidation node is invalid")
        node = nodes[node_id]
        if node.get("allocation_id") != allocation_id:
            raise AuditError("PlanGraph allocation is no longer active for its node")
        if node.get("status") == "succeeded":
            raise AuditError("PlanGraph succeeded attempt cannot be invalidated")
        invalidation = {
            "attempt_id": attempt_id,
            "node_id": node_id,
            "allocation_id": allocation_id,
            "reason": reason,
            "invalidated_at": _timestamp(),
        }
        retry_state["invalidations"].append(invalidation)
        active[:] = [item for item in active if item != node_id]
        nodes[node_id].update(
            {
                "status": "queued",
                "allocation_id": None,
                "logical_attempt": None,
                "input_commit": None,
                "integrated_commit": None,
            }
        )
        committed = self.journal.compare_and_swap_checkpoint(
            expected_revision=expected_revision,
            expected_head_hash=expected_head_hash,
            status="running",
            state=state,
            event_type="plan_graph_successor_attempt_invalidated",
            event_status="invalidated",
            payload=dict(invalidation),
            actor=_ACTOR,
        )
        return {
            **invalidation,
            "successor_checkpoint_revision": committed["checkpoint"]["revision"],
            "event_hash": committed["event"]["event_hash"],
        }

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
        artifact = self.journal.write_artifact(
            "plan-graph-functionality-failure-evidence",
            {"command": command, "candidate_commit": candidate_commit, "error": error},
        )
        state["functionality_test"] = {
            "state": "failed",
            "command": command,
            "candidate_commit": candidate_commit,
            "error": error,
            "evidence_ref": f"artifact:sha256:{artifact.sha256}",
            "finished_at": _timestamp(),
        }
        self.journal.append(
            "functionality_test_completed",
            status="failed",
            payload=dict(state["functionality_test"]),
            actor=_ACTOR,
            artifacts=(artifact,),
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
        if self.predecessor_attempt_id is not None:
            resume = self._resume_state()
            checkpoint = self.predecessor_checkpoint
            if not isinstance(resume, dict) or not isinstance(checkpoint, dict):
                raise AuditError("repair successor is missing verified predecessor custody")
            checkpoint_artifact = journal.write_artifact("plan-graph-repair-predecessor-checkpoint", checkpoint)
            logical_attempt = {"protocol": "harness-plan-graph-parallel-logical-attempt/1", "graph_id": self.logical_graph_id, "logical_attempt": resume["logical_attempt"], "base_commit": self._initial_state["base_commit"], "allocator_revision": checkpoint["revision"]}
            resume_authority = {"protocol": "harness-plan-graph-parallel-resume/1", "graph_id": self.logical_graph_id, "logical_attempt": resume["logical_attempt"], "checkpoint_revision": checkpoint["revision"], "checkpoint_ref": f"artifact:sha256:{checkpoint_artifact.sha256}", "reason": "repair"}
            _validate_repair_contracts(logical_attempt, resume_authority)
            logical_artifact = journal.write_artifact("plan-graph-logical-attempt", logical_attempt)
            authority_artifact = journal.write_artifact("plan-graph-resume-authority", resume_authority)
            journal.append("plan_graph_repair_successor_allocated", status="running", payload={**resume, "logical_attempt_ref": f"artifact:sha256:{logical_artifact.sha256}", "resume_authority_ref": f"artifact:sha256:{authority_artifact.sha256}"}, actor=_ACTOR, artifacts=(checkpoint_artifact, logical_artifact, authority_artifact))
            for node_id, node in self._initial_state["nodes"].items():
                if node.get("reused_from_attempt") is None:
                    continue
                receipt = journal.write_artifact("plan-graph-node-reuse-receipt", {"logical_graph_id": self.logical_graph_id, "predecessor_attempt_id": self.predecessor_attempt_id, "node_id": node_id, "candidate_commit": node["candidate_commit"], "blocker_evidence_ref": resume["blocker_evidence_ref"]})
                journal.append("plan_graph_node_reused", status="succeeded", payload={"plan_node_id": node_id, "reuse_receipt_ref": f"artifact:sha256:{receipt.sha256}"}, actor=_ACTOR, artifacts=(receipt,))
            journal.checkpoint("running", self._initial_state)
        return journal

    def _resume_state(self) -> dict[str, object] | None:
        if self.predecessor_attempt_id is None or self.resume_directive is None:
            return None
        attempt_id = self.graph_attempt_id
        _, marker, ordinal = attempt_id.rpartition("-attempt-")
        if not marker or not ordinal.isdigit() or int(ordinal) < 1:
            raise AuditError("repair graph attempt id has no positive ordinal")
        return {"logical_graph_id": self.logical_graph_id, "predecessor_attempt_id": self.predecessor_attempt_id, "retry_frontier": list(getattr(self.resume_directive, "retry_frontier", ())), "blocker_evidence_ref": getattr(self.resume_directive, "blocker_evidence_ref", None), "logical_attempt": int(ordinal)}

    def _transition(
        self,
        event_type: str,
        status: str,
        node_id: str,
        updates: Mapping[str, object],
        artifacts: tuple[object, ...] = (),
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
            artifacts=artifacts,
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
    plan_sections: Mapping[str, str],
    acceptance_criteria: Mapping[str, str],
) -> str:
    """Bind a checkpoint to the complete supplied decomposition."""

    contract_keys = ("objective", "plan_sections", "criteria", "depends_on", "verification_argv")
    payload = {
        "plan": plan,
        "plan_digest": plan_digest,
        "base_commit": base_commit,
        "nodes": {key: {field: value.get(field) for field in contract_keys} for key, value in nodes.items()},
        "functionality_tests": list(functionality_tests),
        "plan_sections": dict(plan_sections),
        "acceptance_criteria": dict(acceptance_criteria),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_repair_contracts(
    logical_attempt: Mapping[str, object], resume_authority: Mapping[str, object]
) -> None:
    """Validate the frozen PG-00 successor records before writing them."""
    if (
        set(logical_attempt) != {"protocol", "graph_id", "logical_attempt", "base_commit", "allocator_revision"}
        or logical_attempt.get("protocol") != "harness-plan-graph-parallel-logical-attempt/1"
        or not isinstance(logical_attempt.get("graph_id"), str) or not logical_attempt["graph_id"]
        or not isinstance(logical_attempt.get("logical_attempt"), int) or logical_attempt["logical_attempt"] < 1
        or not _is_git_commit(logical_attempt.get("base_commit"))
        or not isinstance(logical_attempt.get("allocator_revision"), int) or logical_attempt["allocator_revision"] < 1
        or set(resume_authority) != {"protocol", "graph_id", "logical_attempt", "checkpoint_revision", "checkpoint_ref", "reason"}
        or resume_authority.get("protocol") != "harness-plan-graph-parallel-resume/1"
        or resume_authority.get("graph_id") != logical_attempt.get("graph_id")
        or resume_authority.get("logical_attempt") != logical_attempt.get("logical_attempt")
        or not isinstance(resume_authority.get("checkpoint_revision"), int) or resume_authority["checkpoint_revision"] < 1
        or not _ARTIFACT_REF.fullmatch(str(resume_authority.get("checkpoint_ref")))
        or resume_authority.get("reason") != "repair"
    ):
        raise AuditError("repair successor records do not satisfy frozen contracts")


def _is_git_commit(value: object) -> bool:
    return isinstance(value, str) and bool(_GIT_COMMIT.fullmatch(value))


def _attempt_id(graph_run_id: str, allocation_id: object) -> str:
    """Return a stable audit identity without treating it as a child identity."""

    suffix = allocation_id if isinstance(allocation_id, str) and allocation_id else "serial"
    return f"{graph_run_id}:attempt:{suffix}"


def _barrier_id(node_id: str, allocation_id: object) -> str:
    suffix = allocation_id if isinstance(allocation_id, str) and allocation_id else "serial"
    return f"{node_id}:integration:{suffix}"


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
