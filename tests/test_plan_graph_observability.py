"""Acceptance tests for registered PlanGraph audit and resume boundaries."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.core.audit import AuditActor, AuditConflictError, AuditError, AuditJournal
from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    RepairResumeDirective,
    register_plan_graph,
)
from harness_labs.plan_graph_budget import RetryBudgetLedger, gate_digest
from scripts.run_plan_graph import _approval_lineage_id


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def _success(request, commit: str) -> FeatureRunOutcome:
    return success(request, commit)


def success(request, commit: str) -> FeatureRunOutcome:
    return FeatureRunOutcome(
        "succeeded",
        commit,
        plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id,
        feature_run_id=request.feature_run_id,
        run_dir=str(request.run_dir),
    )


class PlanGraphObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "tests@example.com")
        git(self.repository, "config", "user.name", "Tests")
        plan = self.repository / "docs" / "plan.md"
        plan.parent.mkdir()
        plan.write_text("First AC-1\nSecond AC-2\n", encoding="utf-8")
        git(self.repository, "add", "docs/plan.md")
        git(self.repository, "commit", "-m", "plan")
        self.base = git(self.repository, "rev-parse", "HEAD")
        self.decomposition = {
            "plan": "docs/plan.md",
            "base_commit": self.base,
            "runs": [
                {"id": "first", "objective": "First", "plan_sections": ["1"], "criteria": ["AC-1"]},
                {"id": "second", "objective": "Second", "plan_sections": ["2"], "criteria": ["AC-2"], "depends_on": ["first"]},
            ],
            "plan_sections": {"1": "First AC-1", "2": "Second AC-2"},
            "acceptance_criteria": {"AC-1": "AC-1", "AC-2": "AC-2"},
        }
        self.registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="observed-graph",
            decomposition=self.decomposition,
        )
        self.run_root = self.root / "logs" / "runs"

    def register_variant(self, logical_graph_id: str, **overrides):
        decomposition = {**{key: value for key, value in self.decomposition.items()}, **overrides}
        return register_plan_graph(
            repository=self.repository,
            logical_graph_id=logical_graph_id,
            decomposition=decomposition,
        )

    def test_changed_plan_reregistration_reuses_lineage_and_fails_closed(self) -> None:
        # Establish the original registration's budget ledger before a later
        # approval changes the plan contents and commit.
        PlanGraph(
            self.repository, self.registration, lambda request: FeatureRunOutcome("failed"),
            run_root=self.run_root, graph_run_id="original",
        )
        plan = self.repository / "docs" / "plan.md"
        plan.write_text("First AC-1 revised\nSecond AC-2\n", encoding="utf-8")
        git(self.repository, "add", "docs/plan.md")
        git(self.repository, "commit", "-m", "revised plan")
        revised_base = git(self.repository, "rev-parse", "HEAD")
        revised = register_plan_graph(
            repository=self.repository,
            logical_graph_id="observed-graph",
            decomposition={**self.decomposition, "base_commit": revised_base},
        )

        self.assertEqual(revised.plan_lineage_id, self.registration.plan_lineage_id)
        with self.assertRaisesRegex(PlanGraphError, "changed-plan"):
            PlanGraph(
                self.repository, revised, lambda request: FeatureRunOutcome("failed"),
                run_root=self.run_root, graph_run_id="revised",
            )

    def test_repair_successor_preserves_predecessor_and_reuses_only_outside_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predecessor = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "c" * 40) if request.run.id == "first"
                else FeatureRunOutcome("failed", evidence={"error": "repair me"}),
                run_root=root / "runs", graph_run_id="logical",
            )
            self.assertEqual(predecessor.run().status, "failed")
            before = (root / "runs" / "logical" / "manifest.json").read_bytes()
            blocker_ref = predecessor._audit_for_run().state["nodes"]["second"]["evidence"]["evidence_ref"]
            requests = []
            successor = PlanGraph.resume(
                self.repository, self.registration,
                lambda request: requests.append(request) or _success(request, "d" * 40),
                run_root=root / "runs",
                directive=RepairResumeDirective("logical", "logical", ("second",), blocker_ref),
            )
            self.assertEqual(successor.run().status, "succeeded")
            self.assertEqual([request.run.id for request in requests], ["second"])
            self.assertEqual(requests[0].base_commit, "c" * 40)
            successor_dir = root / "runs" / "logical-attempt-1"
            self.assertEqual((root / "runs" / "logical" / "manifest.json").read_bytes(), before)
            state = successor._audit_for_run().state
            self.assertEqual(state["nodes"]["first"]["reused_from_attempt"], "logical")
            self.assertEqual(state["nodes"]["second"]["reused_from_attempt"], None)
            events = (successor_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("plan_graph_repair_successor_allocated", events)
            self.assertIn("plan_graph_node_reused", events)
            AuditJournal.verify(successor_dir)

    def test_repair_ignores_exhaustion_for_reused_nodes_outside_selected_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predecessor = PlanGraph(
                self.repository, self.registration,
                lambda request: _success(request, "c" * 40) if request.run.id == "first"
                else FeatureRunOutcome("failed", evidence={"error": "repair me"}),
                run_root=root / "runs", graph_run_id="logical",
            )
            self.assertEqual(predecessor.run().status, "failed")
            ledger = RetryBudgetLedger(root / "runs", self.registration.plan_lineage_id)
            for _ in range(4):
                ledger.reserve(node_id="first", gate=gate_digest(()))
            blocker = predecessor._audit_for_run().state["nodes"]["second"]["evidence"]["evidence_ref"]
            successor = PlanGraph.resume(
                self.repository, self.registration, lambda request: _success(request, "d" * 40),
                run_root=root / "runs",
                directive=RepairResumeDirective("logical", "logical", ("second",), blocker),
            )
            self.assertEqual(successor.run().status, "succeeded")

    def test_block_escalation_is_recorded_and_authorizes_resume(self) -> None:
        graph = PlanGraph(
            self.repository, self.registration,
            lambda request: FeatureRunOutcome("blocked", evidence={"error": "operator review needed"}),
            run_root=self.run_root, graph_run_id="blocked-graph",
        )
        self.assertEqual(graph.run().status, "blocked")
        audit = graph._audit_for_run()
        blocker = audit.state["block_escalation_ref"]
        escalation = json.loads((self.run_root / "blocked-graph" / "escalation.json").read_text(encoding="utf-8"))
        self.assertEqual(escalation["protocol"], "plan-graph-block-escalation/1")
        self.assertEqual(escalation["status_flags"], {"complete": True, "success": False, "resumable": True, "deviated": True})
        self.assertEqual(escalation["nodes"][0]["tier"], "tier_2")
        self.assertEqual(escalation["nodes"][0]["classification"], "indeterminate")
        self.assertTrue(escalation["decision_request"]["required"])
        self.assertEqual(escalation["paths"]["decision_log"], ".plan-graph-budgets/" + self.registration.plan_lineage_id + ".jsonl")
        successor = PlanGraph.resume(
            self.repository, self.registration,
            lambda request: _success(request, "e" * 40),
            run_root=self.run_root,
            directive=RepairResumeDirective("blocked-graph", "blocked-graph", ("first",), blocker),
        )
        self.assertEqual(successor.run().status, "succeeded")

    def test_oversized_block_escalation_externalizes_node_detail(self) -> None:
        graph = PlanGraph(
            self.repository, self.registration,
            lambda request: FeatureRunOutcome("blocked"),
            run_root=self.run_root, graph_run_id="large-blocked-graph",
        )
        audit = graph._audit_for_run()
        reference = audit.record_block_escalation({
            "protocol": "plan-graph-block-escalation/1",
            "graph_run_id": graph.graph_run_id,
            "logical_graph_id": graph.logical_graph_id,
            "predecessor_attempt_id": None,
            "blocked_node_id": "first",
            "reason": "operator review needed",
            "status_flags": {"complete": True, "success": False, "resumable": True, "deviated": True},
            "nodes": [{"node_id": "first", "status": "blocked", "reason": "operator review needed", "tier": "tier_2", "classification": "indeterminate", "open_obligations": [], "candidate_commit": None, "evidence_ref": None, "detail": "x" * (4 * 1024 * 1024)}],
            "budget_state": {},
            "significance_guidance": {},
            "decision_request": {"required": True, "requested_action": "operator_review", "rationale": "operator review needed", "candidate_actions": ["resume"]},
            "paths": {"escalation": "escalation.json", "budget_ledger": ".plan-graph-budgets/example.jsonl", "decision_log": ".plan-graph-budgets/example.jsonl"},
            "resume_directive_template": {"logical_graph_id": graph.logical_graph_id, "predecessor_attempt_id": graph.graph_run_id, "retry_frontier": ["first"], "blocker_evidence_ref": "$this_escalation_artifact"},
        })
        escalation = json.loads((self.run_root / "large-blocked-graph" / "escalation.json").read_text(encoding="utf-8"))
        self.assertTrue(escalation["node_detail_externalized"])
        self.assertTrue(escalation["nodes"][0]["detail_ref"].startswith("artifact:sha256:"))
        self.assertEqual(audit.state["block_escalation_ref"], reference)

    def test_repeated_ordinary_repair_failure_consumes_finding_budget(self) -> None:
        """A retry carries its predecessor failure artifact into the ledger."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = PlanGraph(
                self.repository, self.registration,
                lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}),
                run_root=root / "runs", graph_run_id="logical",
            )
            self.assertEqual(current.run().status, "failed")
            for _ in range(3):
                blocker = current._audit_for_run().state["nodes"]["first"]["evidence"]["evidence_ref"]
                current = PlanGraph.resume(
                    self.repository, self.registration,
                    lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}),
                    run_root=root / "runs",
                    directive=RepairResumeDirective("logical", current.graph_run_id, ("first",), blocker),
                )
                self.assertEqual(current.run().status, "failed")
            blocker = current._audit_for_run().state["nodes"]["first"]["evidence"]["evidence_ref"]
            exhausted = PlanGraph.resume(
                self.repository, self.registration,
                lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}),
                run_root=root / "runs",
                directive=RepairResumeDirective("logical", current.graph_run_id, ("first",), blocker),
            )
            self.assertEqual(exhausted.run().status, "blocked")
            self.assertTrue((root / "runs" / exhausted.graph_run_id / "escalation.json").is_file())
            ledger_path = root / "runs" / ".plan-graph-budgets" / f"{self.registration.plan_lineage_id}.jsonl"
            reserved = [
                json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("event") == "reserved"
            ]
            self.assertEqual(
                reserved[-1]["failure_keys"],
                ["reason:" + hashlib.sha256(blocker.encode("utf-8")).hexdigest()],
            )

    def test_approval_lineage_uses_stable_registration_slot(self) -> None:
        first = _approval_lineage_id("repository-1", "plans/feature.json")
        self.assertEqual(
            first,
            _approval_lineage_id("repository-1", "plans/feature.json"),
        )
        self.assertNotEqual(
            first,
            _approval_lineage_id("repository-1", "plans/other.json"),
        )
        self.assertNotEqual(
            first,
            _approval_lineage_id("repository-2", "plans/feature.json"),
        )

    def test_repair_reconciles_interrupted_predecessor_reservations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predecessor = PlanGraph(
                self.repository, self.registration,
                lambda request: _success(request, "c" * 40) if request.run.id == "first"
                else FeatureRunOutcome("failed", evidence={"error": "repair me"}),
                run_root=root / "runs", graph_run_id="logical",
            )
            self.assertEqual(predecessor.run().status, "failed")
            ledger = RetryBudgetLedger(root / "runs", self.registration.plan_lineage_id)
            stale = ledger.reserve(
                node_id="second", gate=gate_digest(()), graph_attempt_id="logical",
                classification="infrastructure_transient", failure_keys=("worker-lost",),
            )
            ledger.started(stale)
            blocker = predecessor._audit_for_run().state["nodes"]["second"]["evidence"]["evidence_ref"]
            self.assertEqual(
                PlanGraph.resume(
                    self.repository, self.registration, lambda request: _success(request, "d" * 40),
                    run_root=root / "runs",
                    directive=RepairResumeDirective("logical", "logical", ("second",), blocker),
                ).run().status,
                "succeeded",
            )
            events = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
            reconciliation = next(event for event in events if event.get("reservation_id") == stale and event["event"] == "abandoned")
            self.assertEqual(reconciliation["graph_attempt_id"], "logical")
            self.assertEqual(reconciliation["disposition"], "abandoned")

    def test_run_reconciles_reservation_without_an_audit_node(self) -> None:
        """A crash before audit.node_started must not strand the reservation."""
        graph = PlanGraph(
            self.repository, self.registration,
            lambda request: _success(request, "c" * 40),
            run_root=self.run_root, graph_run_id="reserve-before-audit",
        )
        stale = graph.budget.reserve(
            node_id="first", gate=gate_digest(()), graph_attempt_id="reserve-before-audit",
        )
        graph.budget.started(stale)
        self.assertEqual(graph.run().status, "succeeded")
        events = [json.loads(line) for line in graph.budget.path.read_text(encoding="utf-8").splitlines()]
        abandoned = next(event for event in events if event.get("reservation_id") == stale and event["event"] == "abandoned")
        self.assertEqual(abandoned["disposition"], "abandoned")

    def test_repair_rejects_plan_contract_drift_and_unrecorded_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predecessor = PlanGraph(self.repository, self.registration, lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}), run_root=root / "runs", graph_run_id="logical")
            self.assertEqual(predecessor.run().status, "failed")
            blocker = predecessor._audit_for_run().state["nodes"]["first"]["evidence"]["evidence_ref"]
            with self.assertRaisesRegex(PlanGraphError, "not recorded"):
                PlanGraph.resume(self.repository, self.registration, lambda request: _success(request, "c" * 40), run_root=root / "runs", directive=RepairResumeDirective("logical", "logical", ("first",), f"artifact:sha256:{'e' * 64}"))
            changed_runs = [dict(self.decomposition["runs"][0], verification_argv=["changed"]), dict(self.decomposition["runs"][1])]
            for changed in (
                self.register_variant("drift-argv", runs=changed_runs),
                self.register_variant("drift-sections", plan_sections={"1": "First AC-1 altered", "2": "Second AC-2"}),
                self.register_variant("drift-criteria", acceptance_criteria={"AC-1": "First AC-1", "AC-2": "AC-2"}),
            ):
                with self.subTest(changed=changed.logical_graph_id):
                    with self.assertRaisesRegex(PlanGraphError, "matching failed or blocked"):
                        PlanGraph.resume(self.repository, changed, lambda request: _success(request, "c" * 40), run_root=root / "runs", directive=RepairResumeDirective("logical", "logical", ("first",), blocker))

    def test_repair_reruns_the_selected_frontier_and_its_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registration = self.register_variant(
                "with-third",
                runs=[*self.decomposition["runs"], {"id": "third", "objective": "Second", "plan_sections": ["2"], "criteria": ["AC-2"], "depends_on": ["second"]}],
            )
            predecessor = PlanGraph(
                self.repository, registration,
                lambda request: _success(request, "c" * 40) if request.run.id == "first"
                else FeatureRunOutcome("failed", evidence={"error": "repair"}),
                run_root=root / "runs", graph_run_id="logical",
            )
            self.assertEqual(predecessor.run().status, "failed")
            blocker = predecessor._audit_for_run().state["nodes"]["second"]["evidence"]["evidence_ref"]
            requests = []
            successor = PlanGraph.resume(
                self.repository, registration,
                lambda request: requests.append(request.run.id) or _success(request, "d" * 40),
                run_root=root / "runs", directive=RepairResumeDirective("logical", "logical", ("second",), blocker),
            )
            self.assertEqual(successor.run().status, "succeeded")
            self.assertEqual(requests, ["second", "third"])

    def _reserved_audit(self, root: Path, graph_id: str):
        base = self.base
        graph = PlanGraph(self.repository, self.registration, lambda request: _success(request, "unused"), run_root=root / "runs", graph_run_id=graph_id)
        audit = graph._audit_for_run()
        checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
        audit.reserve_successor_attempt(node_id="first", logical_attempt=1, allocation_id="allocation-first", parent_candidate_commit=base, expected_revision=checkpoint["revision"], expected_staging_head=base)
        return audit

    def _terminal_child(self, run_dir: Path, run_id: str, *, graph_id: str, attempt: dict[str, object], invalid_request: bool = False) -> dict[str, object]:
        child = AuditJournal(run_dir, run_id, actor=AuditActor("child", "feature_run"))
        descriptor = {
            "protocol": "harness-plan-graph-parallel-child-request/1", "graph_id": graph_id,
            "node_id": "first", "allocation": {"batch_id": "batch-first", "logical_attempt": 1,
            "allocation_id": "allocation-first", "checkpoint_revision": attempt["checkpoint_revision"],
            "expected_staging_head": self.base}, "parent_candidate_commit": self.base,
            "dependency_candidates": [], "lane": {"branch": "lane-first", "worktree": "/lane-first", "may_advance_staging": False}, "writable_paths": ["harness_labs/plan_graph.py"],
        }
        verification = {"exit_code": 0, "command": "test"}
        candidate = {"operation": "commit", "candidate_commit": "c" * 40}
        if invalid_request:
            descriptor["lane"]["may_advance_staging"] = True
        descriptor_artifact = child.write_artifact("child-descriptor", descriptor)
        verification_artifact = child.write_artifact("verification", verification)
        candidate_artifact = child.write_artifact("candidate", candidate)
        child.finalize("succeeded", result={}, state=child.checkpoint_state())
        manifest = json.loads((run_dir / "manifest.json").read_text())
        return {"protocol": "harness-plan-graph-parallel-seal-receipt/1", "status": "sealed", "graph_id": graph_id, "node_id": "first", "logical_attempt": 1, "allocation_id": "allocation-first", "parent_candidate_commit": self.base, "candidate_commit": "c" * 40, "canonical_manifest_ref": f"artifact:sha256:{manifest['manifest_hash']}", "descriptor_ref": f"artifact:sha256:{descriptor_artifact.sha256}", "verification_evidence_ref": f"artifact:sha256:{verification_artifact.sha256}", "candidate_receipt_ref": f"artifact:sha256:{candidate_artifact.sha256}", "terminal_journal_event_ref": f"artifact:sha256:{manifest['head_hash']}"}

    @staticmethod
    def _liveness(graph_id: str, *, state: str, token: str) -> dict[str, object]:
        return {"protocol": "harness-plan-graph-parallel-liveness/1", "graph_id": graph_id, "node_id": "first", "logical_attempt": 1, "allocation_id": "allocation-first", "pid": 41, "process_start_token": token, "state": state}

    @staticmethod
    def _force_evidence(audit) -> str:
        artifact = audit.journal.write_artifact(
            "force-reconcile-evidence", {"operator": "recovery controller"}
        )
        audit.journal.append(
            "plan_graph_force_reconcile_evidence_recorded",
            status="running",
            payload={},
            artifacts=(artifact,),
        )
        audit.journal.checkpoint("running", audit.state)
        return f"artifact:sha256:{artifact.sha256}"

    def test_recovery_adopts_dead_child_with_closed_request_and_never_moves_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-recovery")
            node = audit.state["nodes"]["first"]
            child_dir = Path(node["run_dir"])
            receipt = self._terminal_child(child_dir, node["feature_run_id"], graph_id="graph-recovery", attempt=audit.state["successor_attempts"][0])
            (child_dir / "plan-graph-liveness.json").write_text(json.dumps(self._liveness("graph-recovery", state="dead", token="old")))
            (child_dir / "plan-graph-seal-receipt.json").write_text(json.dumps(receipt))
            self.assertEqual(audit.reconcile_interrupted_attempts(process_probe=lambda pid: None), {"first": "sealed"})
            self.assertEqual(audit.state["nodes"]["first"]["candidate_commit"], "c" * 40)
            self.assertEqual(audit.state["current_candidate_commit"], self.base)
            self.assertIsNone(audit.state["nodes"]["first"]["integrated_commit"])
            AuditJournal.verify(audit.run_dir)

    def test_recovery_keeps_matching_live_child_running_and_does_not_adopt_its_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-live")
            node = audit.state["nodes"]["first"]
            child_dir = Path(node["run_dir"])
            receipt = self._terminal_child(child_dir, node["feature_run_id"], graph_id="graph-live", attempt=audit.state["successor_attempts"][0])
            (child_dir / "plan-graph-liveness.json").write_text(json.dumps(self._liveness("graph-live", state="live", token="still-live")))
            (child_dir / "plan-graph-seal-receipt.json").write_text(json.dumps(receipt))
            self.assertEqual(audit.reconcile_interrupted_attempts(process_probe=lambda pid: "still-live"), {"first": "running"})
            self.assertEqual(audit.state["active_node_ids"], ["first"])
            self.assertIsNone(audit.state["nodes"]["first"]["candidate_commit"])

    def test_recovery_rejects_a_seal_bound_to_an_open_child_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-open-request")
            node = audit.state["nodes"]["first"]
            child_dir = Path(node["run_dir"])
            receipt = self._terminal_child(child_dir, node["feature_run_id"], graph_id="graph-open-request", attempt=audit.state["successor_attempts"][0], invalid_request=True)
            (child_dir / "plan-graph-liveness.json").write_text(json.dumps(self._liveness("graph-open-request", state="dead", token="old")))
            (child_dir / "plan-graph-seal-receipt.json").write_text(json.dumps(receipt))
            self.assertEqual(audit.reconcile_interrupted_attempts(process_probe=lambda pid: None), {"first": "blocked"})

    def test_force_block_quarantines_late_manifest_and_open_records_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-force")
            node = audit.state["nodes"]["first"]
            child_dir = Path(node["run_dir"])
            receipt = self._terminal_child(child_dir, node["feature_run_id"], graph_id="graph-force", attempt=audit.state["successor_attempts"][0])
            (child_dir / "plan-graph-seal-receipt.json").write_text(json.dumps(receipt))
            force = {"protocol": "harness-plan-graph-parallel-force-reconcile/1", "graph_id": "graph-force", "node_id": "first", "logical_attempt": 1, "allocation_id": "allocation-first", "disposition": "blocked", "evidence_ref": self._force_evidence(audit)}
            self.assertEqual(audit.reconcile_interrupted_attempts(process_probe=lambda pid: None, force_records=(force,)), {"first": "blocked"})
            self.assertIn("plan_graph_late_manifest_quarantined", (audit.run_dir / "events.jsonl").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-invalid-force")
            with self.assertRaisesRegex(ValueError, "does not match"):
                audit.reconcile_interrupted_attempts(process_probe=lambda pid: None, force_records=({"unexpected": True},))

    def test_force_record_rejects_stale_allocation_and_unresolvable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-stale-force")
            stale = audit.state["successor_attempts"][0]
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.invalidate_successor_attempt(
                allocation_id="allocation-first",
                reason="replace interrupted allocation",
                expected_revision=checkpoint["revision"],
            )
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=2,
                allocation_id="allocation-first-retry",
                parent_candidate_commit=self.base,
                expected_revision=checkpoint["revision"],
                expected_staging_head=self.base,
            )
            stale_force = {"protocol": "harness-plan-graph-parallel-force-reconcile/1", "graph_id": "graph-stale-force", "node_id": "first", "logical_attempt": stale["logical_attempt"], "allocation_id": stale["allocation_id"], "disposition": "blocked", "evidence_ref": self._force_evidence(audit)}
            with self.assertRaisesRegex(ValueError, "does not match"):
                audit.reconcile_interrupted_attempts(process_probe=lambda pid: None, force_records=(stale_force,))

        with tempfile.TemporaryDirectory() as temporary:
            audit = self._reserved_audit(Path(temporary), "graph-unresolvable-force")
            force = {"protocol": "harness-plan-graph-parallel-force-reconcile/1", "graph_id": "graph-unresolvable-force", "node_id": "first", "logical_attempt": 1, "allocation_id": "allocation-first", "disposition": "blocked", "evidence_ref": f"artifact:sha256:{'d' * 64}"}
            with self.assertRaisesRegex(ValueError, "does not match"):
                audit.reconcile_interrupted_attempts(process_probe=lambda pid: None, force_records=(force,))

    def test_audit_state_keeps_logical_identity_attempt_lineage_and_retry_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_commit = self.base
            audit = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-lineage",
            )._audit_for_run()

            initial = audit.state
            self.assertEqual(initial["logical_graph"]["logical_graph_id"], "graph-lineage")
            self.assertEqual(initial["logical_graph"]["plan_digest"], initial["plan_digest"])
            self.assertEqual(initial["logical_graph"]["base_commit"], base_commit)
            self.assertEqual(initial["graph_attempt"], {
                "graph_attempt_id": "graph-lineage",
                "predecessor_attempt_id": None,
            })
            self.assertIsNone(initial["nodes"]["first"]["input_commit"])
            self.assertIsNone(initial["nodes"]["first"]["integrated_commit"])

            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=1,
                allocation_id="allocation-first",
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )
            reserved = audit.state
            first_lineage = reserved["attempt_lineage"]
            self.assertEqual(len(first_lineage), 1)
            self.assertEqual(first_lineage[0]["input_commit"], base_commit)
            self.assertIsNone(first_lineage[0]["predecessor_attempt_id"])
            self.assertEqual(reserved["nodes"]["first"]["input_commit"], base_commit)

            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.node_failed("first", "interrupted", {"reason": "controller stopped"})
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            invalidated = audit.invalidate_successor_attempt(
                allocation_id="allocation-first",
                reason="verified repair required",
                expected_revision=checkpoint["revision"],
            )
            self.assertEqual(invalidated["attempt_id"], first_lineage[0]["attempt_id"])
            self.assertEqual(audit.state["retry_state"]["invalidations"][0]["allocation_id"], "allocation-first")

            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=2,
                allocation_id="allocation-first-retry",
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )
            retried = audit.state
            self.assertEqual(len(retried["attempt_lineage"]), 2)
            self.assertEqual(
                retried["attempt_lineage"][1]["predecessor_attempt_id"],
                first_lineage[0]["attempt_id"],
            )
            self.assertEqual(retried["retry_state"]["reuse"][0]["reused_from_attempt_id"], first_lineage[0]["attempt_id"])

            audit.node_completed("first", "c" * 40)
            completed = audit.state
            self.assertEqual(completed["nodes"]["first"]["integrated_commit"], "c" * 40)
            self.assertEqual(completed["integration_barriers"][0]["input_commit"], base_commit)
            self.assertNotIn("integration_receipts", completed)
            AuditJournal.verify(audit.run_dir)

    def test_succeeded_successor_attempt_cannot_be_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_commit = self.base
            audit = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-succeeded-attempt",
            )._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=1,
                allocation_id="allocation-first",
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )
            audit.node_completed("first", "c" * 40)
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())

            with self.assertRaisesRegex(AuditError, "succeeded attempt"):
                audit.invalidate_successor_attempt(
                    allocation_id="allocation-first",
                    reason="must not retry success",
                    expected_revision=checkpoint["revision"],
                )

            self.assertEqual(audit.state["nodes"]["first"]["status"], "succeeded")
            self.assertEqual(len(audit.state["attempt_lineage"]), 1)

    def test_successor_attempt_batch_is_cas_bound_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_commit = self.base
            graph = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-successor-attempt",
            )
            audit = graph._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            receipts = audit.reserve_successor_attempt_batch(
                allocations=(
                    {"node_id": "first", "allocation_id": "allocation-first"},
                    {"node_id": "second", "allocation_id": "allocation-second"},
                ),
                logical_attempt=1,
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )

            state = audit.state
            evidence = state["successor_attempts"]
            self.assertEqual(len(evidence), 2)
            self.assertEqual(
                {item["checkpoint_revision"] for item in evidence}, {checkpoint["revision"]}
            )
            self.assertEqual(
                {item["parent_candidate_commit"] for item in evidence}, {base_commit}
            )
            self.assertEqual(
                {item["logical_attempt"] for item in evidence}, {1}
            )
            self.assertEqual(state["nodes"]["first"]["status"], "reserved")
            self.assertEqual(state["nodes"]["second"]["status"], "reserved")
            self.assertEqual(state["active_node_ids"], ["first", "second"])
            self.assertEqual(len({receipt["event_hash"] for receipt in receipts}), 1)

            with self.assertRaisesRegex(AuditConflictError, "revision changed"):
                audit.reserve_successor_attempt_batch(
                    allocations=({"node_id": "second", "allocation_id": "allocation-retry"},),
                    logical_attempt=2,
                    parent_candidate_commit=base_commit,
                    expected_revision=checkpoint["revision"],
                    expected_staging_head=base_commit,
                )
            events = [
                json.loads(line)
                for line in (audit.run_dir / "events.jsonl").read_text().splitlines()
            ]
            reservations = [
                event for event in events
                if event["event_type"] == "plan_graph_successor_attempts_reserved"
            ]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["payload"]["allocations"], evidence)
            AuditJournal.verify(audit.run_dir)

    def test_successor_attempt_rejects_non_schema_commit_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-invalid-identity",
            )._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            for parent_candidate_commit, expected_staging_head in (
                ("base", "a" * 40),
                ("a" * 40, "base"),
            ):
                with self.subTest(
                    parent_candidate_commit=parent_candidate_commit,
                    expected_staging_head=expected_staging_head,
                ), self.assertRaisesRegex(ValueError, "full lowercase Git commit"):
                    audit.reserve_successor_attempt(
                        node_id="first",
                        logical_attempt=1,
                        allocation_id="allocation-first",
                        parent_candidate_commit=parent_candidate_commit,
                        expected_revision=checkpoint["revision"],
                        expected_staging_head=expected_staging_head,
                    )

    def test_legacy_checkpoint_requires_explicit_migration_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-legacy",
            )
            audit = graph._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            del checkpoint["state"]["audit_state_protocol"]
            audit.journal.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(PlanGraphError, "legacy-incompatible"):
                PlanGraph(
                    self.repository,
                    self.registration,
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-legacy",
                )._audit_for_run()

    def test_prior_audit_protocol_is_explicitly_legacy_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = PlanGraph(
                self.repository,
                self.registration,
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-prior-protocol",
            )
            audit = graph._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            checkpoint["state"]["audit_state_protocol"] = "harness-plan-graph-audit/1"
            audit.journal.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(PlanGraphError, "legacy-incompatible"):
                PlanGraph(
                    self.repository,
                    self.registration,
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-prior-protocol",
                )._audit_for_run()

    def test_plan_graph_rejects_non_audited_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(TypeError, "run_root"):
                PlanGraph(
                    self.repository,
                    self.registration,
                    lambda request: _success(request, "unused"),
                )
            with self.assertRaisesRegex(TypeError, "state_path"):
                PlanGraph(
                    self.repository,
                    self.registration,
                    lambda request: _success(request, "unused"),
                    state_path=root / "legacy.json",
                )
            with self.assertRaisesRegex(PlanGraphError, "audited PlanGraph"):
                PlanGraph(
                    self.repository,
                    self.registration,
                    lambda request: _success(request, "unused"),
                    run_root=None,
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph(self, launcher, attempt="attempt-one", registration=None):
        return PlanGraph(
            self.repository,
            registration or self.registration,
            launcher,
            run_root=self.run_root,
            graph_run_id=attempt,
        )

    def test_initial_checkpoint_contains_full_dag_before_launch(self) -> None:
        observed = {}

        def launcher(request):
            checkpoint = json.loads(
                (self.run_root / "attempt-one" / "checkpoint.json").read_text()
            )["state"]
            observed.update(checkpoint)
            return FeatureRunOutcome("failed")

        self.graph(launcher).run()
        self.assertEqual(observed["ordered_node_ids"], ["first", "second"])
        self.assertEqual(observed["nodes"]["second"]["status"], "queued")
        self.assertEqual(observed["nodes"]["second"]["depends_on"], ["first"])
        self.assertEqual(
            observed["registration_binding"],
            {
                "logical_graph_id": "observed-graph",
                "registration_protocol": "plan-graph-registration/2",
                "registration_digest": self.registration.graph_digest,
                "graph_attempt_id": "attempt-one",
            },
        )

    def test_same_attempt_resumes_and_terminal_short_circuits(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            self.graph(
                lambda request: success(request, "first-commit")
                if request.run.id == "first"
                else (_ for _ in ()).throw(RuntimeError("stopped"))
            ).run()
        calls = []
        result = self.graph(
            lambda request: calls.append(request.run.id)
            or success(request, "second-commit")
        ).run()
        self.assertEqual((result.status, calls), ("succeeded", ["second"]))
        calls.clear()
        terminal = self.graph(lambda request: calls.append(request.run.id)).run()
        self.assertEqual(terminal.status, "succeeded")
        self.assertEqual(calls, [])

    def test_reused_attempt_rejects_different_registration_before_launch(self) -> None:
        self.graph(lambda request: FeatureRunOutcome("failed")).run()
        other = register_plan_graph(
            repository=self.repository,
            logical_graph_id="other-graph",
            decomposition=self.decomposition,
        )
        calls = []
        with self.assertRaisesRegex(PlanGraphError, "registration binding"):
            self.graph(
                lambda request: calls.append(request), registration=other
            ).run()
        self.assertEqual(calls, [])

    def test_reused_attempt_rejects_checkpoint_node_definition_drift(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            self.graph(
                lambda request: (_ for _ in ()).throw(RuntimeError("stopped"))
            ).run()
        run_dir = self.run_root / "attempt-one"
        journal = AuditJournal.open_existing(
            run_dir, actor=AuditActor("test", "test")
        )
        state = journal.checkpoint_state()
        state["nodes"]["second"]["depends_on"] = []
        journal.checkpoint("running", state)
        with self.assertRaisesRegex(PlanGraphError, "does not match"):
            self.graph(lambda request: success(request, "unused")).run()

    def test_new_attempt_is_full_rerun(self) -> None:
        first_calls = []
        self.graph(
            lambda request: first_calls.append(request.run.id)
            or success(request, f"{request.run.id}-commit")
        ).run()
        second_calls = []
        self.graph(
            lambda request: second_calls.append(request.run.id)
            or success(request, f"new-{request.run.id}-commit"),
            attempt="attempt-two",
        ).run()
        self.assertEqual(first_calls, ["first", "second"])
        self.assertEqual(second_calls, ["first", "second"])

    def test_descriptor_remains_catalog_compatible(self) -> None:
        self.graph(lambda request: FeatureRunOutcome("failed")).run()
        descriptor = json.loads(
            (self.run_root / "attempt-one" / "descriptor.json").read_text()
        )
        self.assertEqual(
            set(descriptor),
            {
                "protocol", "run_kind", "run_id", "created_at", "objective",
                "evidence_classification", "repository", "approved_plan",
                "parent_correlation", "logical_graph_id", "graph_attempt_id",
                "predecessor_attempt_id",
            },
        )
        self.assertEqual(AuditJournal.verify(self.run_root / "attempt-one")["run_id"], "attempt-one")

    def test_register_then_run_cli_from_unrelated_cwd(self) -> None:
        decomposition = self.root / "decomposition.json"
        decomposition.write_text(json.dumps(self.decomposition), encoding="utf-8")
        launcher = self.root / "launcher.py"
        launcher.write_text(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "assert request['protocol'] == 'plan-graph-feature-run-request/1'\n"
            "print(json.dumps({'status':'succeeded','candidate_commit':'candidate-' + request['plan_node_id'],"
            "'plan_graph_id':request['plan_graph_id'],'plan_node_id':request['plan_node_id'],"
            "'feature_run_id':request['feature_run_id'],'run_dir':request['run_dir']}))\n",
            encoding="utf-8",
        )
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_plan_graph.py"
        registered = subprocess.run(
            [sys.executable, str(script), "register", str(decomposition), "--repository", str(self.repository), "--logical-graph-id", "cli-graph"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        registration_path = json.loads(registered.stdout)["registration"]
        run = subprocess.run(
            [sys.executable, str(script), "run", "--repository", str(self.repository), "--registration", registration_path, "--graph-attempt-id", "cli-attempt", "--launcher-command", sys.executable, str(launcher)],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.repository / "logs" / "runs" / "cli-attempt" / "checkpoint.json").is_file())
        self.assertTrue((self.repository / "logs" / "plan-graph-registrations" / "cli-graph.json").is_file())


if __name__ == "__main__":
    unittest.main()
