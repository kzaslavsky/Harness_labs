"""Controller-owned deterministic custody for a sealed PlanGraph join."""

from __future__ import annotations

import re
import shutil
import tempfile
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .audit import AuditActor
from .git_transaction import GitTransactionError, git_output
from .plan_graph_audit import PlanGraphAudit


class PlanGraphIntegrationError(RuntimeError):
    """Raised when an integration barrier cannot prove its custody inputs."""


_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_ARTIFACT = re.compile(r"^artifact:sha256:[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VerificationRunner = Callable[[Path, str], str]
_ACTOR = AuditActor("plan-graph", "plan_graph_controller")


def _commit(value: object, label: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise PlanGraphIntegrationError(f"{label} must be a full lowercase Git commit")
    return value


def _artifact(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT.fullmatch(value):
        raise PlanGraphIntegrationError(f"{label} must be a SHA-256 artifact reference")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PlanGraphIntegrationError(f"{label} must be a non-empty identifier")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PlanGraphIntegrationError(f"{label} must be an object")
    return value


@dataclass(frozen=True)
class DependencyCandidate:
    node_id: str
    candidate_commit: str
    seal_receipt_ref: str


@dataclass(frozen=True)
class ProtectedRef:
    graph_id: str
    ref_name: str
    head_commit: str

    def receipt(self) -> dict[str, str]:
        return {"protocol": "harness-plan-graph-parallel-protected-ref/1", "graph_id": self.graph_id,
                "ref_name": self.ref_name, "head_commit": self.head_commit, "owner": "controller"}


@dataclass(frozen=True)
class JoinPreparation:
    graph_id: str
    node_id: str
    logical_attempt: int
    allocation_id: str
    checkpoint_revision: int
    lease_id: str
    protected_ref: ProtectedRef
    parent_candidate_commit: str
    dependencies: tuple[DependencyCandidate, ...]


class PlanGraphIntegrationBarrier:
    """Integrate exact sealed dependencies and CAS a controller-owned ref.

    The barrier requires the graph's :class:`PlanGraphAudit` and can advance
    only that graph's deterministic staging ref.  It persists a publish intent
    containing the complete receipt before the Git CAS.  Thus an interruption
    after ref movement has durable evidence from which recovery can determine
    whether the intended CAS completed.
    """

    def __init__(self, repository: Path, *, graph_id: str, protected_ref: str,
                 audit: PlanGraphAudit) -> None:
        self.repository = repository.resolve(strict=True)
        self.graph_id = _identifier(graph_id, "graph_id")
        if not isinstance(audit, PlanGraphAudit):
            raise PlanGraphIntegrationError("integration requires a PlanGraph audit")
        if audit.graph_run_id != self.graph_id:
            raise PlanGraphIntegrationError("audit does not belong to this graph")
        expected_ref = self.ref_for_graph(self.graph_id)
        if protected_ref != expected_ref:
            raise PlanGraphIntegrationError("protected_ref must be the graph-owned staging ref")
        audit_head = _commit(audit.state.get("current_candidate_commit"), "audit current_candidate_commit")
        observed_head = self._ref_head_for(expected_ref)
        if observed_head != audit_head and not self._has_recoverable_publish_intent(
            audit.state, expected_ref, audit_head, observed_head
        ):
            raise PlanGraphIntegrationError("graph audit staging head does not match its protected ref")
        self.ref_name, self.audit = protected_ref, audit

    @staticmethod
    def ref_for_graph(graph_id: str) -> str:
        """Return the sole controller-owned staging ref for ``graph_id``."""
        return f"refs/heads/plangraph/{_identifier(graph_id, 'graph_id')}/staging"

    def protected_ref(self) -> ProtectedRef:
        return ProtectedRef(self.graph_id, self.ref_name, self._ref_head())

    def prepare_join(self, request: Mapping[str, object], *, dependency_order: Sequence[str],
                     checkpoint: Mapping[str, object], sealed_receipts_by_ref: Mapping[str, Mapping[str, object]],
                     lease_id: str) -> JoinPreparation:
        """Verify that a join has every exact, sealed dependency input."""
        request = _mapping(request, "join request")
        if request.get("protocol") != "harness-plan-graph-parallel-child-request/1" or request.get("graph_id") != self.graph_id:
            raise PlanGraphIntegrationError("join request does not belong to this graph")
        node = _identifier(request.get("node_id"), "join request node_id")
        allocation = _mapping(request.get("allocation"), "join allocation")
        logical = self._positive(allocation.get("logical_attempt"), "join logical_attempt")
        allocation_id = _identifier(allocation.get("allocation_id"), "join allocation_id")
        revision = self._positive(allocation.get("checkpoint_revision"), "join checkpoint_revision")
        _identifier(allocation.get("batch_id"), "join batch_id")
        parent = _commit(request.get("parent_candidate_commit"), "parent_candidate_commit")
        if _commit(allocation.get("expected_staging_head"), "expected_staging_head") != parent:
            raise PlanGraphIntegrationError("join expected_staging_head must equal parent_candidate_commit")
        lane = _mapping(request.get("lane"), "join lane")
        if lane.get("may_advance_staging") is not False:
            raise PlanGraphIntegrationError("join child request may not advance the protected staging ref")
        self._checkpoint(checkpoint, node, logical, allocation_id, revision, parent, lease_id)
        order = tuple(dependency_order)
        if not order or len(order) != len(set(order)) or any(not isinstance(x, str) or not x for x in order):
            raise PlanGraphIntegrationError("dependency_order must contain unique non-empty node ids")
        dependencies = request.get("dependency_candidates")
        if not isinstance(dependencies, Sequence) or isinstance(dependencies, (str, bytes)) or len(dependencies) != len(order):
            raise PlanGraphIntegrationError("join request has a partial or unexpected dependency set")
        sealed = self._seals(checkpoint)
        verified: list[DependencyCandidate] = []
        for expected, raw in zip(order, dependencies):
            entry = _mapping(raw, "dependency candidate")
            if entry.get("node_id") != expected:
                raise PlanGraphIntegrationError("dependency candidates are not in declared stable order")
            candidate, reference = _commit(entry.get("candidate_commit"), "dependency candidate_commit"), _artifact(entry.get("seal_receipt_ref"), "dependency seal_receipt_ref")
            if sealed.get(expected) != (candidate, reference):
                raise PlanGraphIntegrationError(f"dependency {expected} is not the checkpoint's exact sealed input")
            receipt = sealed_receipts_by_ref.get(reference)
            self._seal(receipt, expected, candidate, parent, reference, checkpoint)
            self._ancestor(parent, candidate, f"dependency {expected}")
            verified.append(DependencyCandidate(expected, candidate, reference))
        protected = self.protected_ref()
        if protected.head_commit != parent:
            raise PlanGraphIntegrationError("protected ref changed from the expected parent")
        return JoinPreparation(self.graph_id, node, logical, allocation_id, revision, _identifier(lease_id, "lease_id"), protected, parent, tuple(verified))

    def integrate(self, prepared: JoinPreparation, *, verification_runner: VerificationRunner) -> dict[str, object]:
        """Construct, verify, then atomically publish the ordered integration."""
        if not isinstance(prepared, JoinPreparation) or prepared.graph_id != self.graph_id or prepared.protected_ref.ref_name != self.ref_name:
            raise PlanGraphIntegrationError("prepared join does not belong to this integration barrier")
        if not callable(verification_runner):
            raise PlanGraphIntegrationError("integration requires a controller-owned verification runner")
        self._persist("lease_acquired", prepared, {"expected_head": prepared.protected_ref.head_commit})
        try:
            if self._ref_head() != prepared.protected_ref.head_commit:
                raise PlanGraphIntegrationError("protected ref changed after join preparation")
            head, merges = prepared.parent_candidate_commit, []
            for dependency in prepared.dependencies:
                previous = head
                head = self._merge(previous, dependency, prepared.node_id)
                merge = {"dependency_node_id": dependency.node_id, "parent_head": previous,
                         "dependency_candidate": dependency.candidate_commit, "merge_commit": head}
                merges.append(merge)
                self._persist("dependency_integrated", prepared, merge)
            evidence = self._verify(head, verification_runner)
            self._persist("join_verified", prepared, {"candidate_head": head, "verification_evidence_ref": evidence})
            receipt = {"protocol": "harness-plan-graph-parallel-integration-receipt/1", "graph_id": self.graph_id,
                       "node_id": prepared.node_id, "logical_attempt": prepared.logical_attempt,
                       "protected_ref": self.ref_name, "expected_head": prepared.protected_ref.head_commit,
                       "integrated_head": head, "verification_evidence_ref": evidence}
            # This must precede the Git CAS: a crash after update-ref can then
            # be recovered by comparing the graph-owned ref to integrated_head.
            self._persist("staging_publish_intent", prepared, {"receipt": receipt, "merge_chain": merges})
            self._cas(head, prepared.protected_ref.head_commit)
            self._persist("staging_advanced", prepared, {"receipt": receipt, "merge_chain": merges})
            return receipt
        except Exception as exc:
            self._persist("integration_conflict", prepared, {"reason": str(exc)})
            raise
        finally:
            self._persist("lease_released", prepared, {})

    def recover_interrupted_publish(self) -> dict[str, object] | None:
        """Finish the one durably intended publish left by an interrupted join.

        The intent is written only after verification and before the Git CAS.
        Recovery may therefore either acknowledge an already-moved ref or
        perform the still-pending CAS.  Any third head is evidence of a
        conflicting writer and is deliberately not reconciled implicitly.
        """
        records = self.audit.state.get("integration_barriers")
        if not isinstance(records, list):
            raise PlanGraphIntegrationError("PlanGraph audit integration-barrier state is invalid")
        advanced = {
            self._receipt_identity(record.get("receipt"))
            for record in records
            if isinstance(record, Mapping) and record.get("action") == "staging_advanced"
        }
        intents = [
            record for record in records
            if isinstance(record, Mapping)
            and record.get("action") == "staging_publish_intent"
            and self._receipt_identity(record.get("receipt")) not in advanced
        ]
        if not intents:
            return None
        if len(intents) != 1:
            raise PlanGraphIntegrationError("multiple unreconciled staging publish intents")
        prepared, receipt = self._prepared_from_intent(intents[0])
        observed = self._ref_head()
        if observed == receipt["expected_head"]:
            self._cas(receipt["integrated_head"], receipt["expected_head"])
        elif observed != receipt["integrated_head"]:
            self._persist("integration_conflict", prepared, {"reason": "protected ref conflicts with publish intent"})
            raise PlanGraphIntegrationError("protected ref conflicts with publish intent")
        self._persist("staging_advanced", prepared, {"receipt": receipt, "recovered": True})
        self._persist("lease_released", prepared, {})
        return receipt

    def _checkpoint(self, checkpoint: Mapping[str, object], node: str, logical: int, allocation_id: str, revision: int, parent: str, lease_id: str) -> None:
        checkpoint = _mapping(checkpoint, "controller checkpoint")
        if checkpoint.get("protocol") != "harness-plan-graph-parallel-checkpoint/1" or checkpoint.get("graph_id") != self.graph_id:
            raise PlanGraphIntegrationError("controller checkpoint does not belong to this graph")
        if self._positive(checkpoint.get("revision"), "checkpoint revision") != revision or self._positive(checkpoint.get("logical_attempt"), "checkpoint logical_attempt") != logical or _commit(checkpoint.get("staging_head"), "checkpoint staging_head") != parent:
            raise PlanGraphIntegrationError("join allocation does not match the controller checkpoint")
        allocations = checkpoint.get("allocations")
        if not isinstance(allocations, Sequence) or isinstance(allocations, (str, bytes)):
            raise PlanGraphIntegrationError("checkpoint allocations must be an array")
        matches = [x for x in allocations if isinstance(x, Mapping) and x.get("node_id") == node]
        if len(matches) != 1 or matches[0].get("logical_attempt") != logical or matches[0].get("allocation_id") != allocation_id or matches[0].get("checkpoint_revision") != revision or matches[0].get("expected_staging_head") != parent:
            raise PlanGraphIntegrationError("join allocation is not the checkpoint's exact allocation")
        lease = _mapping(checkpoint.get("integration_lease"), "checkpoint integration_lease")
        if lease.get("node_id") != node or lease.get("lease_id") != lease_id or lease.get("expected_staging_head") != parent:
            raise PlanGraphIntegrationError("join does not hold the checkpoint's integration lease")

    def _seals(self, checkpoint: Mapping[str, object]) -> dict[str, tuple[str, str]]:
        raw = checkpoint.get("sealed")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise PlanGraphIntegrationError("checkpoint sealed must be an array")
        result = {}
        for item in raw:
            item = _mapping(item, "checkpoint sealed dependency")
            node = _identifier(item.get("node_id"), "checkpoint sealed node_id")
            if node in result:
                raise PlanGraphIntegrationError("checkpoint repeats a sealed dependency")
            result[node] = (_commit(item.get("candidate_commit"), "checkpoint candidate"), _artifact(item.get("seal_receipt_ref"), "checkpoint seal receipt"))
        return result

    def _seal(self, raw: object, node: str, candidate: str, parent: str, reference: str, checkpoint: Mapping[str, object]) -> None:
        seal = _mapping(raw, f"full sealed receipt for {node}")
        allocation = [x for x in checkpoint["allocations"] if isinstance(x, Mapping) and x.get("node_id") == node]
        if len(allocation) != 1 or seal.get("protocol") != "harness-plan-graph-parallel-seal-receipt/1" or seal.get("status") != "sealed" or seal.get("graph_id") != self.graph_id or seal.get("node_id") != node or seal.get("candidate_commit") != candidate or seal.get("parent_candidate_commit") != parent or seal.get("logical_attempt") != allocation[0].get("logical_attempt") or seal.get("allocation_id") != allocation[0].get("allocation_id"):
            raise PlanGraphIntegrationError(f"dependency {node} full seal receipt does not bind its candidate and allocation")
        for key in ("canonical_manifest_ref", "descriptor_ref", "verification_evidence_ref", "candidate_receipt_ref", "terminal_journal_event_ref"):
            _artifact(seal.get(key), f"dependency {node} {key}")
        _artifact(reference, f"dependency {node} seal_receipt_ref")

    def _persist(self, action: str, prepared: JoinPreparation, payload: Mapping[str, object]) -> None:
        state = self.audit.state
        records = state.setdefault("integration_barriers", [])
        if not isinstance(records, list):
            raise PlanGraphIntegrationError("PlanGraph audit integration-barrier state is invalid")
        record = {"node_id": prepared.node_id, "attempt_id": f"{self.graph_id}:attempt:{prepared.allocation_id}", "allocation_id": prepared.allocation_id, "logical_attempt": prepared.logical_attempt, "checkpoint_revision": prepared.checkpoint_revision, "lease_id": prepared.lease_id, "action": action, **dict(payload)}
        if action == "lease_acquired" and state.get("integration_lease") is not None:
            raise PlanGraphIntegrationError("a graph integration lease is already held")
        records.append(record)
        if action == "lease_acquired":
            state["integration_lease"] = {"node_id": prepared.node_id, "lease_id": prepared.lease_id, "expected_staging_head": prepared.parent_candidate_commit}
        elif action == "lease_released":
            state["integration_lease"] = None
        elif action == "staging_advanced":
            state.setdefault("integration_receipts", []).append(payload["receipt"])
            state["current_candidate_commit"] = payload["receipt"]["integrated_head"]  # type: ignore[index]
        elif action == "integration_conflict":
            state.setdefault("integration_conflicts", []).append(record)
        event_type = "plan_graph_" + action
        status = "blocked" if action == "integration_conflict" else "running"
        # The repaired PG-01 journal supplies the interprocess CAS boundary.
        # Acquiring the lease through it prevents two controller processes from
        # both accepting the same otherwise-valid join preparation.
        checkpoint = json.loads(self.audit.journal.checkpoint_path.read_text(encoding="utf-8"))
        try:
            self.audit.journal.compare_and_swap_checkpoint(
                expected_revision=checkpoint["revision"],
                expected_head_hash=checkpoint["head_hash"],
                status=status,
                state=state,
                event_type=event_type,
                event_status=status,
                payload=record,
                actor=_ACTOR,
            )
        except Exception as exc:
            raise PlanGraphIntegrationError("PlanGraph integration audit CAS failed") from exc

    def _ref_head(self) -> str:
        return self._ref_head_for(self.ref_name)

    def _ref_head_for(self, ref_name: str) -> str:
        try:
            return _commit(git_output(self.repository, "rev-parse", f"{ref_name}^{{commit}}"), "protected ref head")
        except GitTransactionError as exc:
            raise PlanGraphIntegrationError(f"protected ref is unreadable: {exc}") from exc

    def _ancestor(self, parent: str, candidate: str, label: str) -> None:
        try: git_output(self.repository, "merge-base", "--is-ancestor", parent, candidate)
        except GitTransactionError as exc: raise PlanGraphIntegrationError(f"{label} is not descended from parent_candidate_commit") from exc

    def _merge(self, current: str, dependency: DependencyCandidate, node: str) -> str:
        try:
            tree = git_output(self.repository, "merge-tree", "--write-tree", current, dependency.candidate_commit)
            return _commit(git_output(self.repository, "commit-tree", tree, "-p", current, "-p", dependency.candidate_commit, "-m", f"PlanGraph {node}: integrate {dependency.node_id}"), "merge commit")
        except GitTransactionError as exc: raise PlanGraphIntegrationError(f"integration conflict or Git failure for dependency {dependency.node_id}: {exc}") from exc

    def _cas(self, head: str, expected: str) -> None:
        try: git_output(self.repository, "update-ref", self.ref_name, head, expected)
        except GitTransactionError as exc: raise PlanGraphIntegrationError("protected ref compare-and-swap failed") from exc

    def _verify(self, head: str, runner: VerificationRunner) -> str:
        parent = Path(tempfile.mkdtemp(prefix="plangraph-verify-")); worktree = parent / "worktree"
        try:
            git_output(self.repository, "worktree", "add", "--detach", str(worktree), head)
            evidence = _artifact(runner(worktree, head), "verification runner evidence")
            if self._ref_at(worktree) != head: raise PlanGraphIntegrationError("verification runner changed the constructed integration head")
            return evidence
        finally:
            try: git_output(self.repository, "worktree", "remove", "--force", str(worktree))
            except GitTransactionError: pass
            shutil.rmtree(parent, ignore_errors=True)

    @staticmethod
    def _positive(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1: raise PlanGraphIntegrationError(f"{label} must be positive")
        return value

    @staticmethod
    def _ref_at(path: Path) -> str: return _commit(git_output(path, "rev-parse", "HEAD"), "verification worktree head")

    def _prepared_from_intent(self, intent: Mapping[str, object]) -> tuple[JoinPreparation, dict[str, object]]:
        receipt = intent.get("receipt")
        if not isinstance(receipt, Mapping) or receipt.get("protocol") != "harness-plan-graph-parallel-integration-receipt/1" or receipt.get("graph_id") != self.graph_id or receipt.get("protected_ref") != self.ref_name:
            raise PlanGraphIntegrationError("staging publish intent has an invalid receipt")
        node = _identifier(receipt.get("node_id"), "publish intent node_id")
        logical = self._positive(receipt.get("logical_attempt"), "publish intent logical_attempt")
        expected = _commit(receipt.get("expected_head"), "publish intent expected_head")
        integrated = _commit(receipt.get("integrated_head"), "publish intent integrated_head")
        _artifact(receipt.get("verification_evidence_ref"), "publish intent verification_evidence_ref")
        allocation_id = _identifier(intent.get("allocation_id"), "publish intent allocation_id")
        if intent.get("node_id") != node or self._positive(intent.get("logical_attempt"), "publish intent record logical_attempt") != logical:
            raise PlanGraphIntegrationError("staging publish intent does not bind its node and attempt")
        prepared = JoinPreparation(self.graph_id, node, logical, allocation_id,
                                   self._positive(intent.get("checkpoint_revision"), "publish intent checkpoint_revision"),
                                   _identifier(intent.get("lease_id"), "publish intent lease_id"),
                                   ProtectedRef(self.graph_id, self.ref_name, expected), expected, ())
        return prepared, dict(receipt)

    @classmethod
    def _has_recoverable_publish_intent(cls, state: Mapping[str, object], ref_name: str,
                                        expected_head: str, observed_head: str) -> bool:
        records = state.get("integration_barriers")
        if not isinstance(records, list):
            return False
        advanced = {
            cls._receipt_identity(record.get("receipt"))
            for record in records
            if isinstance(record, Mapping) and record.get("action") == "staging_advanced"
        }
        return any(
            isinstance(record, Mapping)
            and record.get("action") == "staging_publish_intent"
            and cls._receipt_identity(record.get("receipt")) not in advanced
            and isinstance(record.get("receipt"), Mapping)
            and record["receipt"].get("protected_ref") == ref_name
            and record["receipt"].get("expected_head") == expected_head
            and record["receipt"].get("integrated_head") == observed_head
            for record in records
        )

    @staticmethod
    def _receipt_identity(raw: object) -> tuple[str, str] | None:
        if not isinstance(raw, Mapping):
            return None
        expected, integrated = raw.get("expected_head"), raw.get("integrated_head")
        if not isinstance(expected, str) or not isinstance(integrated, str):
            return None
        return expected, integrated


__all__ = ["DependencyCandidate", "JoinPreparation", "PlanGraphIntegrationBarrier", "PlanGraphIntegrationError", "ProtectedRef", "VerificationRunner"]
