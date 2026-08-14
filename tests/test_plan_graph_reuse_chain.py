"""Reuse custody must survive successor chains.

Root attempt: a succeeds, b fails. First repair successor reuses a and fails
b again. Second repair successor — resuming from the FIRST successor, whose
own execution never integrated a — must still reuse a instead of re-running
it: custody flows through copied barriers and digest-verified reuse receipts.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
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


class ReuseChainTests(unittest.TestCase):
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
        self.registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="reuse-chain",
            decomposition={
                "plan": "docs/approved-plan.md",
                "base_commit": self.base_commit,
                "runs": [
                    {"id": "a", "objective": "Build A", "plan_sections": ["1"],
                     "criteria": ["AC-1"], "depends_on": [],
                     "verification_argv": ["true"]},
                    {"id": "b", "objective": "Build B", "plan_sections": ["2"],
                     "criteria": ["AC-2"], "depends_on": ["a"],
                     "verification_argv": ["true"]},
                ],
                "plan_sections": {
                    "1": "Build A. AC-1: A works.",
                    "2": "Build B. AC-2: B works.",
                },
                "acceptance_criteria": {"AC-1": "A works.", "AC-2": "B works."},
                "functionality_tests": [],
            },
        )
        self.run_root = self.root / "runs"
        self.launched: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit_file(self, base: str, name: str) -> str:
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=self.repository,
            text=True, input=name, capture_output=True, check=True,
        ).stdout.strip()
        listing = git(self.repository, "ls-tree", base)
        tree = subprocess.run(
            ["git", "mktree"], cwd=self.repository, text=True,
            input=listing + f"\n100644 blob {blob}\t{name}\n",
            capture_output=True, check=True,
        ).stdout.strip()
        return git(self.repository, "commit-tree", tree, "-p", base, "-m", name)

    def launcher(self, fail_b: bool):
        def launch(request):
            self.launched.append(request.plan_node_id)
            if request.plan_node_id == "b" and fail_b:
                return FeatureRunOutcome("failed", evidence={"error": "b exploded"})
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt"
            )
            return replace(
                FeatureRunOutcome("succeeded", candidate_commit=candidate),
                plan_graph_id=request.plan_graph_id,
                plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id,
                run_dir=str(request.run_dir),
            )
        return launch

    def blocker_ref(self, attempt_id: str) -> str:
        events = self.run_root / attempt_id / "events.jsonl"
        for line in events.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            for artifact in event.get("artifacts", []):
                if "node-failure" in str(artifact.get("path", "")):
                    return "artifact:sha256:" + artifact["sha256"]
        raise AssertionError("no node-failure evidence recorded")

    def resume(self, predecessor: str, fail_b: bool) -> PlanGraph:
        return PlanGraph.resume(
            self.repository,
            self.registration,
            self.launcher(fail_b),
            run_root=self.run_root,
            directive=RepairResumeDirective(
                logical_graph_id="root-attempt",
                predecessor_attempt_id=predecessor,
                retry_frontier=("b",),
                blocker_evidence_ref=self.blocker_ref(predecessor),
            ),
        )

    def test_reuse_survives_two_successor_hops(self) -> None:
        root = PlanGraph(
            self.repository, self.registration, self.launcher(fail_b=True),
            run_root=self.run_root, graph_run_id="root-attempt",
        )
        self.assertEqual(root.run().status, "failed")
        self.assertEqual(self.launched, ["a", "b"])

        self.launched.clear()
        first = self.resume("root-attempt", fail_b=True)
        self.assertEqual(first.run().status, "failed")
        self.assertEqual(self.launched, ["b"], "first successor must reuse a")

        self.launched.clear()
        second = self.resume(first.graph_run_id, fail_b=False)
        result = second.run()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            self.launched, ["b"],
            "second successor must inherit reuse custody for a, not re-run it",
        )
        self.assertIn("a", result.completed)
        self.assertIn("b", result.completed)


class ParallelSiblingResumeTests(ReuseChainTests):
    """Resume must accept checkpoints where a parallel sibling sealed.

    Diamond a → (b ∥ c): b fails while c seals on its own branch, so the
    completed set {a, c} is not a topological prefix.  The successor must
    still admit the checkpoint, reuse a and c, and re-run only b.
    """

    def setUp(self) -> None:
        super().setUp()
        self.registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="parallel-resume",
            decomposition={
                "plan": "docs/approved-plan.md",
                "base_commit": self.base_commit,
                "runs": [
                    {"id": "a", "objective": "Build A", "plan_sections": ["1"],
                     "criteria": ["AC-1"], "depends_on": [],
                     "verification_argv": ["true"]},
                    {"id": "b", "objective": "Build B", "plan_sections": ["2"],
                     "criteria": ["AC-2"], "depends_on": ["a"],
                     "verification_argv": ["true"]},
                    {"id": "c", "objective": "Build C", "plan_sections": ["3"],
                     "criteria": ["AC-3"], "depends_on": ["a"],
                     "verification_argv": ["true"]},
                ],
                "plan_sections": {
                    "1": "Build A. AC-1: A works.",
                    "2": "Build B. AC-2: B works.",
                    "3": "Build C. AC-3: C works.",
                },
                "acceptance_criteria": {"AC-1": "A works.", "AC-2": "B works.", "AC-3": "C works."},
                "functionality_tests": [],
            },
        )

    def resume(self, predecessor: str, fail_b: bool) -> PlanGraph:
        return PlanGraph.resume(
            self.repository,
            self.registration,
            self.launcher(fail_b),
            run_root=self.run_root,
            directive=RepairResumeDirective(
                logical_graph_id="root-attempt",
                predecessor_attempt_id=predecessor,
                retry_frontier=("b",),
                blocker_evidence_ref=self.blocker_ref(predecessor),
            ),
        )

    def test_reuse_survives_two_successor_hops(self) -> None:  # noqa: D102
        self.skipTest("inherited serial scenario; covered by ReuseChainTests")

    def test_resume_reuses_parallel_sibling_sealed_past_failed_frontier(self) -> None:
        root = PlanGraph(
            self.repository, self.registration, self.launcher(fail_b=True),
            run_root=self.run_root, graph_run_id="root-attempt",
            max_parallelism=2,
        )
        result = root.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("a", result.completed)
        self.assertIn("c", result.completed, "sibling c must seal despite b failing")

        self.launched.clear()
        successor = self.resume("root-attempt", fail_b=False)
        repaired = successor.run()
        self.assertEqual(repaired.status, "succeeded")
        self.assertEqual(
            self.launched, ["b"],
            "successor must reuse a and the sealed parallel sibling c, re-running only b",
        )
        self.assertEqual(set(repaired.completed), {"a", "b", "c"})


if __name__ == "__main__":
    unittest.main()
