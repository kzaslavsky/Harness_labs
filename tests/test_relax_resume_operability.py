"""Finding tests for CB2-05: PlanGraph resume-operability defects (item 16).

Each test method is independently red against the frozen base harness
(``e605fffc90d880fc7e5bb3d779b82b29f74f8e20``) for one of the four verified
resume-operability defects:

1. blocker_evidence_ref only resolved against the graph's own journal, never
   a child run's richer evidence, even though it is reachable from the
   predecessor graph journal.
2. RepairResumeDirective.logical_graph_id was mandatory, forcing an
   operator-supplied value instead of resolving from the predecessor's
   persisted registration binding.
3. PlanGraph.resume silently accepted a conflicting graph_run_id kwarg and
   crashed deep inside admission (TypeError, after lock/dir creation)
   instead of a typed, up-front PlanGraphError.
4. A successor directory left behind by an admission crash permanently
   wasted its ordinal instead of being reclaimed.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    RepairResumeDirective,
    register_plan_graph,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, text=True,
        capture_output=True, check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def _success(request, commit: str) -> FeatureRunOutcome:
    return FeatureRunOutcome(
        "succeeded", commit,
        plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
        feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
    )


class ResumeOperabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "tests@example.com")
        git(self.repository, "config", "user.name", "Tests")
        plan = self.repository / "docs" / "approved-plan.md"
        plan.parent.mkdir()
        plan.write_text("Approved PlanGraph plan\n", encoding="utf-8")
        git(self.repository, "add", "docs/approved-plan.md")
        git(self.repository, "commit", "-m", "approved plan")
        self.base_commit = git(self.repository, "rev-parse", "HEAD")
        self.run_root = self.root / "runs"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, logical_graph_id: str):
        decomposition = {
            "plan": "docs/approved-plan.md",
            "base_commit": self.base_commit,
            "runs": [
                {
                    "id": "a", "objective": "Build A", "plan_sections": ["1"],
                    "criteria": ["AC-1"], "depends_on": [], "verification_argv": [],
                },
            ],
            "plan_sections": {"1": "Build A. AC-1: A works."},
            "acceptance_criteria": {"AC-1": "A works."},
            "functionality_tests": [],
        }
        return register_plan_graph(
            repository=self.repository,
            logical_graph_id=logical_graph_id,
            decomposition=decomposition,
        )

    # -- AC-CB205-1 ---------------------------------------------------------

    def test_blocker_evidence_ref_resolves_through_a_child_runs_own_journal(self) -> None:
        registration = self.register("child-evidence-graph")
        child_ref: dict[str, str] = {}

        def failing_launcher(request):
            child = AuditJournal(
                request.run_dir, request.feature_run_id,
                actor=AuditActor("child", "feature_run"),
            )
            artifact = child.write_artifact(
                "blocker-detail",
                {"detail": "the real failure only the child journal recorded"},
            )
            child.finalize("failed", result={}, state=child.checkpoint_state())
            child_ref["value"] = f"artifact:sha256:{artifact.sha256}"
            return FeatureRunOutcome(
                "failed", evidence={"error": "see the child's own richer evidence"}
            )

        predecessor = PlanGraph(
            self.repository, registration, failing_launcher,
            run_root=self.run_root, graph_run_id="root-attempt",
            logical_graph_id="child-evidence-graph",
        )
        self.assertEqual(predecessor.run().status, "failed")
        self.assertIn("value", child_ref, "launcher must have written its child journal")

        successor = PlanGraph.resume(
            self.repository, registration,
            lambda request: _success(request, "c" * 40),
            run_root=self.run_root,
            directive=RepairResumeDirective(
                logical_graph_id="child-evidence-graph",
                predecessor_attempt_id="root-attempt",
                retry_frontier=("a",),
                blocker_evidence_ref=child_ref["value"],
            ),
        )
        self.assertEqual(successor.run().status, "succeeded")

    # -- AC-CB205-2 -----------------------------------------------------

    def test_omitted_logical_graph_id_resolves_from_registration_binding(self) -> None:
        registration = self.register("resume-op-registration")
        predecessor = PlanGraph(
            self.repository, registration,
            lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}),
            run_root=self.run_root, graph_run_id="root-attempt",
        )
        self.assertEqual(predecessor.run().status, "failed")
        blocker = predecessor._audit_for_run().state["nodes"]["a"]["evidence"]["evidence_ref"]

        successor = PlanGraph.resume(
            self.repository, registration,
            lambda request: _success(request, "c" * 40),
            run_root=self.run_root,
            directive=RepairResumeDirective(
                predecessor_attempt_id="root-attempt",
                retry_frontier=("a",),
                blocker_evidence_ref=blocker,
            ),
        )
        self.assertEqual(successor.logical_graph_id, registration.logical_graph_id)
        self.assertNotEqual(successor.logical_graph_id, "root-attempt")
        self.assertEqual(
            successor.graph_run_id, f"{registration.logical_graph_id}-attempt-1"
        )
        self.assertEqual(successor.run().status, "succeeded")

        with self.assertRaises(PlanGraphError):
            PlanGraph.resume(
                self.repository, registration,
                lambda request: _success(request, "d" * 40),
                run_root=self.run_root,
                directive=RepairResumeDirective(
                    logical_graph_id="an-explicitly-wrong-logical-id",
                    predecessor_attempt_id="root-attempt",
                    retry_frontier=("a",),
                    blocker_evidence_ref=blocker,
                ),
            )

    # -- AC-CB205-3 -----------------------------------------------------

    def test_resume_rejects_a_graph_run_id_kwarg_before_any_lock_or_directory(self) -> None:
        registration = self.register("graph-run-id-kwarg-graph")
        predecessor = PlanGraph(
            self.repository, registration,
            lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}),
            run_root=self.run_root, graph_run_id="root-attempt",
            logical_graph_id="graph-run-id-kwarg-graph",
        )
        self.assertEqual(predecessor.run().status, "failed")
        blocker = predecessor._audit_for_run().state["nodes"]["a"]["evidence"]["evidence_ref"]
        before = {path.name for path in self.run_root.iterdir()} if self.run_root.exists() else set()

        with self.assertRaises(PlanGraphError) as context:
            PlanGraph.resume(
                self.repository, registration,
                lambda request: _success(request, "c" * 40),
                run_root=self.run_root,
                directive=RepairResumeDirective(
                    logical_graph_id="graph-run-id-kwarg-graph",
                    predecessor_attempt_id="root-attempt",
                    retry_frontier=("a",),
                    blocker_evidence_ref=blocker,
                ),
                graph_run_id="operator-supplied-id",
            )
        self.assertIn("graph_run_id", str(context.exception))
        after = {path.name for path in self.run_root.iterdir()} if self.run_root.exists() else set()
        self.assertEqual(before, after, "no lock or directory may be created for a rejected resume")

    # -- AC-CB205-4 -----------------------------------------------------

    def test_admission_crash_orphan_is_reclaimed_instead_of_wasting_its_ordinal(self) -> None:
        registration = self.register("reclaim-graph")
        predecessor = PlanGraph(
            self.repository, registration,
            lambda request: FeatureRunOutcome("failed", evidence={"error": "repair"}),
            run_root=self.run_root, graph_run_id="root-attempt",
            logical_graph_id="reclaim-graph",
        )
        self.assertEqual(predecessor.run().status, "failed")
        blocker = predecessor._audit_for_run().state["nodes"]["a"]["evidence"]["evidence_ref"]
        directive = RepairResumeDirective(
            logical_graph_id="reclaim-graph",
            predecessor_attempt_id="root-attempt",
            retry_frontier=("a",),
            blocker_evidence_ref=blocker,
        )

        # Patched here rather than PlanGraphAudit._resume_state: that method is
        # also called earlier, while __init__ is still building _initial_state
        # -- before the run directory and admission-liveness marker exist.
        # _validate_repair_contracts runs later, after both are durably on
        # disk but before the plan_graph_repair_successor_allocated event, the
        # exact window this defect class is about.
        with patch(
            "harness_labs.plan_graph_audit._validate_repair_contracts",
            side_effect=RuntimeError("simulated admission crash"),
        ):
            with self.assertRaises(RuntimeError):
                PlanGraph.resume(
                    self.repository, registration,
                    lambda request: _success(request, "c" * 40),
                    run_root=self.run_root, directive=directive,
                )

        orphan_dir = self.run_root / "reclaim-graph-attempt-1"
        self.assertTrue(orphan_dir.is_dir(), "the crashed admission must leave its partial directory")
        self.assertFalse((orphan_dir / "manifest.json").exists())

        successor = PlanGraph.resume(
            self.repository, registration,
            lambda request: _success(request, "d" * 40),
            run_root=self.run_root, directive=directive,
            child_liveness_probe=lambda pid: None,
        )

        self.assertEqual(
            successor.graph_run_id, "reclaim-graph-attempt-1",
            "the reclaimed ordinal must be reused, not skipped",
        )
        # The path is occupied again -- by the freshly created successor, not
        # the orphan: its own directory was renamed aside, never deleted.
        self.assertIn(
            "plan_graph_repair_successor_allocated",
            (orphan_dir / "events.jsonl").read_text(encoding="utf-8"),
        )
        reclaimed = sorted(self.run_root.glob(".reclaim-graph-attempt-1.orphan-reclaimed-*"))
        self.assertEqual(len(reclaimed), 1)
        self.assertNotIn(
            "plan_graph_repair_successor_allocated",
            (reclaimed[0] / "events.jsonl").read_text(encoding="utf-8"),
        )
        AuditJournal.verify(reclaimed[0])
        AuditJournal.verify(self.run_root / "root-attempt")
        self.assertEqual(successor.run().status, "succeeded")


if __name__ == "__main__":
    unittest.main()
