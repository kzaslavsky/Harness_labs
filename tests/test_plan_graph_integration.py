"""Deterministic custody tests for PlanGraph join integration."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_labs.audit import AuditJournal
from harness_labs.plan_graph_audit import PlanGraphAudit
from harness_labs.plan_graph_integration import PlanGraphIntegrationBarrier, PlanGraphIntegrationError


ARTIFACT = "artifact:sha256:" + "a" * 64


def _registration_binding(graph_run_id: str) -> dict[str, str]:
    return {"logical_graph_id": graph_run_id, "registration_protocol": "plan-graph-registration/1",
            "registration_digest": "0" * 64, "graph_attempt_id": graph_run_id}


def git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repository, text=True, capture_output=True, check=True).stdout.strip()


class PlanGraphIntegrationBarrierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.repo = Path(self.temp.name)
        git(self.repo, "init", "-q"); git(self.repo, "config", "user.email", "test@example.invalid"); git(self.repo, "config", "user.name", "Test")
        (self.repo / "base").write_text("base\n"); git(self.repo, "add", "base"); git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.protected_ref = PlanGraphIntegrationBarrier.ref_for_graph("graph-1")
        git(self.repo, "branch", self.protected_ref.removeprefix("refs/heads/"))
        self.first = self._candidate("FR-10", "first"); git(self.repo, "switch", "-q", "-C", "lane-two", self.base)
        self.second = self._candidate("FR-11", "second"); git(self.repo, "switch", "-q", "master")
        self.audit = self._audit()
        self.barrier = PlanGraphIntegrationBarrier(self.repo, graph_id="graph-1", protected_ref=self.protected_ref, audit=self.audit)

    def tearDown(self) -> None: self.temp.cleanup()

    def _candidate(self, node: str, name: str) -> str:
        (self.repo / name).write_text(node); git(self.repo, "add", name); git(self.repo, "commit", "-qm", node)
        return git(self.repo, "rev-parse", "HEAD")

    def _audit(self) -> PlanGraphAudit:
        plan = self.repo / "approved-plan.md"; plan.write_text("approved\n", encoding="utf-8")
        return PlanGraphAudit(repository=self.repo, run_root=self.repo / "runs", graph_run_id="graph-1", plan=str(plan),
                              plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(), base_commit=self.base,
                              registration_binding=_registration_binding("graph-1"),
                              objective="join", nodes={"FR-20": {"status": "reserved"}}, functionality_tests=())

    def _request(self, dependencies: list[tuple[str, str]] | None = None) -> dict[str, object]:
        dependencies = dependencies or [("FR-10", self.first), ("FR-11", self.second)]
        return {"protocol": "harness-plan-graph-parallel-child-request/1", "graph_id": "graph-1", "node_id": "FR-20",
                "allocation": {"batch_id": "batch-3", "logical_attempt": 3, "allocation_id": "alloc-join", "checkpoint_revision": 7, "expected_staging_head": self.base},
                "parent_candidate_commit": self.base,
                "dependency_candidates": [{"node_id": node, "candidate_commit": commit, "seal_receipt_ref": ARTIFACT[:-1] + suffix} for (node, commit), suffix in zip(dependencies, "bc")],
                "lane": {"may_advance_staging": False}}

    def _checkpoint(self) -> dict[str, object]:
        return {"protocol": "harness-plan-graph-parallel-checkpoint/1", "graph_id": "graph-1", "revision": 7, "logical_attempt": 3, "staging_head": self.base,
                "ready": [], "reserved": [], "running": [], "blocked": [],
                "sealed": [{"node_id": "FR-10", "candidate_commit": self.first, "seal_receipt_ref": ARTIFACT[:-1] + "b"}, {"node_id": "FR-11", "candidate_commit": self.second, "seal_receipt_ref": ARTIFACT[:-1] + "c"}],
                "allocations": [{"node_id": "FR-20", "logical_attempt": 3, "allocation_id": "alloc-join", "checkpoint_revision": 7, "expected_staging_head": self.base}, {"node_id": "FR-10", "logical_attempt": 3, "allocation_id": "alloc-first", "checkpoint_revision": 7, "expected_staging_head": self.base}, {"node_id": "FR-11", "logical_attempt": 3, "allocation_id": "alloc-second", "checkpoint_revision": 7, "expected_staging_head": self.base}],
                "integration_lease": {"node_id": "FR-20", "lease_id": "lease-1", "expected_staging_head": self.base}}

    def _receipts(self) -> dict[str, dict[str, object]]:
        result = {}
        for node, candidate, allocation, suffix in (("FR-10", self.first, "alloc-first", "b"), ("FR-11", self.second, "alloc-second", "c")):
            result[ARTIFACT[:-1] + suffix] = {"protocol": "harness-plan-graph-parallel-seal-receipt/1", "status": "sealed", "graph_id": "graph-1", "node_id": node, "logical_attempt": 3, "allocation_id": allocation, "parent_candidate_commit": self.base, "candidate_commit": candidate, "canonical_manifest_ref": ARTIFACT, "descriptor_ref": ARTIFACT, "verification_evidence_ref": ARTIFACT, "candidate_receipt_ref": ARTIFACT, "terminal_journal_event_ref": ARTIFACT}
        return result

    def _prepared(self):
        return self.barrier.prepare_join(self._request(), dependency_order=("FR-10", "FR-11"), checkpoint=self._checkpoint(), sealed_receipts_by_ref=self._receipts(), lease_id="lease-1")

    def test_complete_stable_sealed_dependencies_are_required(self) -> None:
        self.assertEqual([item.node_id for item in self._prepared().dependencies], ["FR-10", "FR-11"])
        with self.assertRaisesRegex(PlanGraphIntegrationError, "stable order"):
            self.barrier.prepare_join(self._request([("FR-11", self.second), ("FR-10", self.first)]), dependency_order=("FR-10", "FR-11"), checkpoint=self._checkpoint(), sealed_receipts_by_ref=self._receipts(), lease_id="lease-1")
        with self.assertRaisesRegex(PlanGraphIntegrationError, "partial"):
            self.barrier.prepare_join(self._request([("FR-10", self.first)]), dependency_order=("FR-10", "FR-11"), checkpoint=self._checkpoint(), sealed_receipts_by_ref=self._receipts(), lease_id="lease-1")

    def test_verified_ordered_merges_are_cas_published(self) -> None:
        receipt = self.barrier.integrate(self._prepared(), verification_runner=lambda worktree, head: (self.assertTrue((worktree / "first").exists()), self.assertTrue((worktree / "second").exists()), ARTIFACT)[2])
        self.assertEqual(git(self.repo, "rev-parse", self.protected_ref), receipt["integrated_head"])
        self.assertEqual(git(self.repo, "show", "-s", "--format=%P", receipt["integrated_head"]).split()[1], self.second)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", self.first, receipt["integrated_head"]), "")

    def test_ref_change_or_verification_failure_never_advances_staging(self) -> None:
        prepared = self._prepared(); git(self.repo, "update-ref", self.protected_ref, self.first, self.base)
        with self.assertRaisesRegex(PlanGraphIntegrationError, "changed after"):
            self.barrier.integrate(prepared, verification_runner=lambda _worktree, _head: ARTIFACT)
        self.assertEqual(git(self.repo, "rev-parse", self.protected_ref), self.first)

    def test_receipt_and_conflict_are_persisted_in_plan_graph_audit(self) -> None:
        audit = self._audit()
        barrier = PlanGraphIntegrationBarrier(self.repo, graph_id="graph-1", protected_ref=self.protected_ref, audit=audit)
        prepared = barrier.prepare_join(self._request(), dependency_order=("FR-10", "FR-11"), checkpoint=self._checkpoint(), sealed_receipts_by_ref=self._receipts(), lease_id="lease-1")
        receipt = barrier.integrate(prepared, verification_runner=lambda _worktree, _head: ARTIFACT)
        self.assertEqual(audit.state["integration_receipts"], [receipt])
        self.assertEqual(audit.state["current_candidate_commit"], receipt["integrated_head"])
        self.assertIsNone(audit.state["integration_lease"])
        events = [line for line in (audit.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if "plan_graph_staging_advanced" in line]
        self.assertEqual(len(events), 1)
        AuditJournal.verify(audit.run_dir)

    def test_audit_and_graph_owned_ref_are_mandatory(self) -> None:
        with self.assertRaisesRegex(PlanGraphIntegrationError, "requires a PlanGraph audit"):
            PlanGraphIntegrationBarrier(self.repo, graph_id="graph-1", protected_ref=self.protected_ref, audit=None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(PlanGraphIntegrationError, "graph-owned staging ref"):
            PlanGraphIntegrationBarrier(self.repo, graph_id="graph-1", protected_ref="refs/heads/master", audit=self.audit)

    def test_publish_intent_is_durable_before_a_post_cas_interruption(self) -> None:
        prepared = self._prepared()
        original_cas = self.barrier._cas
        def crash_after_cas(head: str, expected: str) -> None:
            original_cas(head, expected)
            raise KeyboardInterrupt("simulated interruption")
        self.barrier._cas = crash_after_cas  # type: ignore[method-assign]
        with self.assertRaises(KeyboardInterrupt):
            self.barrier.integrate(prepared, verification_runner=lambda _worktree, _head: ARTIFACT)
        records = self.audit.state["integration_barriers"]
        intents = [record for record in records if record["action"] == "staging_publish_intent"]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["receipt"]["integrated_head"], git(self.repo, "rev-parse", self.protected_ref))
        self.assertNotIn("integration_receipts", self.audit.state)
        resumed = PlanGraphIntegrationBarrier(self.repo, graph_id="graph-1", protected_ref=self.protected_ref, audit=self.audit)
        receipt = resumed.recover_interrupted_publish()
        self.assertEqual(receipt, intents[0]["receipt"])
        self.assertEqual(self.audit.state["integration_receipts"], [receipt])
        AuditJournal.verify(self.audit.run_dir)


if __name__ == "__main__": unittest.main()
