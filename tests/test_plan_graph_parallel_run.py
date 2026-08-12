"""Acceptance tests for ready-set (parallel) PlanGraph execution.

The fake launcher creates REAL commits with Git plumbing so join merges,
ancestor pruning, and the final sink join are exercised against an actual
object store, and a barrier proves genuine concurrency.
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    register_plan_graph,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def run_payload(node_id: str, depends_on: list[str]) -> dict[str, object]:
    return {
        "id": node_id,
        "objective": f"Build {node_id}",
        "plan_sections": [node_id],
        "criteria": [f"AC-{node_id}"],
        "depends_on": depends_on,
        "verification_argv": ["python3", "-m", "unittest"],
    }


def decomposition(base_commit: str, edges: dict[str, list[str]]) -> dict[str, object]:
    return {
        "plan": "docs/approved-plan.md",
        "base_commit": base_commit,
        "runs": [run_payload(node, deps) for node, deps in edges.items()],
        "plan_sections": {
            node: f"Build {node}. AC-{node}: {node} works." for node in edges
        },
        "acceptance_criteria": {f"AC-{node}": f"{node} works." for node in edges},
        "functionality_tests": [],
    }


class ParallelPlanGraphTests(unittest.TestCase):
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
        self.git_lock = threading.Lock()
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit_file(self, base: str, name: str, content: str) -> str:
        """Create a real commit on top of ``base`` adding one file."""
        with self.git_lock:
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=self.repository, text=True, input=content,
                capture_output=True, check=True,
            ).stdout.strip()
            listing = git(self.repository, "ls-tree", base)
            entry = f"100644 blob {blob}\t{name}"
            tree = subprocess.run(
                ["git", "mktree"],
                cwd=self.repository, text=True, input=listing + "\n" + entry + "\n",
                capture_output=True, check=True,
            ).stdout.strip()
            return git(
                self.repository, "commit-tree", tree, "-p", base, "-m", f"add {name}"
            )

    def graph(self, edges, launcher, *, max_parallelism, **options) -> PlanGraph:
        self.counter += 1
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id=f"parallel-graph-{self.counter}",
            decomposition=decomposition(self.base_commit, edges),
        )

        def correlated(request):
            outcome = launcher(request)
            if outcome.status != "succeeded":
                return outcome
            return replace(
                outcome,
                plan_graph_id=request.plan_graph_id,
                plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id,
                run_dir=str(request.run_dir),
            )

        return PlanGraph(
            self.repository,
            registration,
            correlated,
            run_root=self.root / "runs" / str(self.counter),
            graph_run_id=f"parallel-attempt-{self.counter}",
            max_parallelism=max_parallelism,
            **options,
        )

    def tree_files(self, commit: str) -> set[str]:
        return {
            line.split("\t", 1)[1]
            for line in git(self.repository, "ls-tree", "-r", commit).splitlines()
        }

    def test_independent_roots_run_concurrently(self) -> None:
        barrier = threading.Barrier(2, timeout=20)
        bases: dict[str, str] = {}

        def launcher(request):
            if request.plan_node_id in {"a1", "a2"}:
                # Both roots must be in flight together; a serial executor
                # deadlocks the barrier and fails the run via timeout.
                barrier.wait()
            bases[request.plan_node_id] = request.base_commit
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        result = self.graph(
            {"a1": [], "a2": []}, launcher, max_parallelism=2
        ).run()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(bases["a1"], self.base_commit)
        self.assertEqual(bases["a2"], self.base_commit)
        # The final candidate joins both sinks.
        files = self.tree_files(result.candidate_commit)
        self.assertIn("a1.txt", files)
        self.assertIn("a2.txt", files)

    def test_diamond_join_bases_dependent_on_both_parents(self) -> None:
        bases: dict[str, str] = {}

        def launcher(request):
            bases[request.plan_node_id] = request.base_commit
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        result = self.graph(
            {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]},
            launcher, max_parallelism=2,
        ).run()
        self.assertEqual(result.status, "succeeded")
        join_base = self.tree_files(bases["d"])
        self.assertIn("b.txt", join_base)
        self.assertIn("c.txt", join_base)
        self.assertIn("a.txt", join_base)
        final = self.tree_files(result.candidate_commit)
        self.assertEqual(
            {"a.txt", "b.txt", "c.txt", "d.txt", "docs/approved-plan.md"}, final
        )

    def test_linear_chain_joins_without_merge_commits(self) -> None:
        def launcher(request):
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        result = self.graph(
            {"a": [], "b": ["a"], "c": ["a", "b"]}, launcher, max_parallelism=2
        ).run()
        self.assertEqual(result.status, "succeeded")
        # c depends on a AND b, but a is an ancestor of b's candidate — the
        # join must prune to b's tip without a synthetic merge commit.
        parents = git(
            self.repository, "rev-list", "--parents", "-n", "1",
            result.candidate_commit,
        ).split()
        self.assertEqual(len(parents), 2, "sink commit must not be a merge")

    def test_sibling_path_conflict_blocks_the_join(self) -> None:
        def launcher(request):
            # Both siblings write the SAME path with different content, so
            # the dependent join must conflict.
            candidate = self.commit_file(
                request.base_commit, "shared.txt", request.plan_node_id
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        result = self.graph(
            {"b": [], "c": [], "d": ["b", "c"]}, launcher, max_parallelism=2
        ).run()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failed_run_id, "d")

    def test_sibling_failure_drains_and_seals_the_survivor(self) -> None:
        release = threading.Event()

        def launcher(request):
            if request.plan_node_id == "bad":
                return FeatureRunOutcome(
                    "failed", evidence={"error": "worker exploded"}
                )
            release.wait(timeout=20)
            candidate = self.commit_file(
                request.base_commit, "good.txt", "good"
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph = self.graph(
            {"good": [], "bad": [], "tail": ["good", "bad"]},
            launcher, max_parallelism=2,
        )
        # Let the failure land first, then release the survivor.
        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            result = graph.run()
        finally:
            timer.cancel()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_run_id, "bad")
        # The in-flight survivor was drained and sealed, not abandoned.
        self.assertIn("good", result.completed)
        self.assertNotIn("tail", result.completed)

    def test_launcher_exception_is_a_child_failure(self) -> None:
        def launcher(request):
            raise RuntimeError("launcher escaped")

        result = self.graph({"a": []}, launcher, max_parallelism=2).run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_run_id, "a")

    def test_max_parallelism_validation(self) -> None:
        with self.assertRaises(Exception):
            self.graph({"a": []}, lambda request: None, max_parallelism=0)


if __name__ == "__main__":
    unittest.main()
