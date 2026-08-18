"""Acceptance tests for ready-set (parallel) PlanGraph execution.

The fake launcher creates REAL commits with Git plumbing so join merges,
ancestor pruning, and the final sink join are exercised against an actual
object store, and a barrier proves genuine concurrency.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    PlanRun,
    ReadySetScheduler,
    register_plan_graph,
)


def _run(node_id: str, depends_on: list[str]) -> PlanRun:
    return PlanRun(
        id=node_id,
        objective=f"Build {node_id}",
        plan_sections=(node_id,),
        criteria=(f"AC-{node_id}",),
        depends_on=tuple(depends_on),
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

    def journal_events(self, graph: PlanGraph) -> list[dict[str, object]]:
        events_path = graph.run_root / graph.graph_run_id / "events.jsonl"
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

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

    def _blocked_beside_a_slow_sibling(self, **options):
        """Run bad+slow concurrently; bad fails first, slow finishes later.

        ``extra`` is an unrelated node with no dependency on either, ready
        the whole time but held out of the first ready set by
        ``max_parallelism=2``.  ``tail`` depends on the failing node.
        """
        release = threading.Event()
        launched: list[str] = []
        launch_lock = threading.Lock()

        def launcher(request):
            with launch_lock:
                launched.append(request.plan_node_id)
            if request.plan_node_id == "bad":
                return FeatureRunOutcome(
                    "failed", evidence={"error": "worker exploded"}
                )
            if request.plan_node_id == "slow":
                release.wait(timeout=20)
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph = self.graph(
            {"bad": [], "slow": [], "extra": [], "tail": ["bad"]},
            launcher, max_parallelism=2, **options,
        )
        # Let the failure land and any follow-on admission happen, then
        # release the long-running sibling.
        timer = threading.Timer(1.0, release.set)
        timer.start()
        try:
            result = graph.run()
        finally:
            timer.cancel()
        return graph, result, launched

    def test_block_stops_all_admission_by_default(self) -> None:
        _, result, launched = self._blocked_beside_a_slow_sibling()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_run_id, "bad")
        # The in-flight sibling drains and seals; nothing new is admitted.
        self.assertIn("slow", result.completed)
        self.assertNotIn("extra", launched)
        self.assertNotIn("tail", launched)

    def test_independent_node_is_admitted_after_a_block_when_opted_in(self) -> None:
        _, result, launched = self._blocked_beside_a_slow_sibling(
            continue_independent_after_block=True,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_run_id, "bad")
        self.assertIn("slow", result.completed)
        # The unrelated node took the freed slot and sealed.
        self.assertIn("extra", launched)
        self.assertIn("extra", result.completed)
        # The blocked node is never relaunched inside its own attempt, and a
        # dependent of it is still never launched.
        self.assertEqual(launched.count("bad"), 1)
        self.assertNotIn("tail", launched)
        self.assertNotIn("tail", result.completed)

    def _two_blocked_frontier(self, **options) -> tuple[list, str]:
        """Run a graph where two independent nodes block; return the frontier."""
        def launcher(request):
            if request.plan_node_id in {"bad", "worse"}:
                return FeatureRunOutcome(
                    "blocked", evidence={"error": f"{request.plan_node_id} blocked"}
                )
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph = self.graph(
            {"bad": [], "worse": [], "tail": ["bad", "worse"]},
            launcher, max_parallelism=2, **options,
        )
        result = graph.run()
        self.assertEqual(result.status, "blocked")
        escalation = json.loads(
            (graph.run_root / graph.graph_run_id / "escalation.json").read_text(
                encoding="utf-8"
            )
        )
        return (
            escalation["resume_directive_template"]["retry_frontier"],
            result.failed_run_id,
        )

    def test_escalation_retry_frontier_names_every_terminal_node(self) -> None:
        """With the flag on, the resume template names both blocked nodes."""
        frontier, failed_run_id = self._two_blocked_frontier(
            continue_independent_after_block=True
        )
        self.assertEqual(frontier[0], failed_run_id)
        self.assertEqual(set(frontier), {"bad", "worse"})

    def test_escalation_retry_frontier_is_unchanged_by_default(self) -> None:
        """escalation.json is a published contract read by operator loops, so
        the widened frontier is gated on the same flag that makes
        multi-terminal attempts common. Off, it keeps its long-standing
        single-element form -- including the pre-existing under-report when a
        drain produces a second terminal node, which is what this graph does.
        """
        frontier, failed_run_id = self._two_blocked_frontier()
        self.assertEqual(frontier, [failed_run_id])

    def test_launcher_exception_is_a_child_failure(self) -> None:
        def launcher(request):
            raise RuntimeError("launcher escaped")

        result = self.graph({"a": []}, launcher, max_parallelism=2).run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_run_id, "a")

    def test_max_parallelism_validation(self) -> None:
        with self.assertRaises(Exception):
            self.graph({"a": []}, lambda request: None, max_parallelism=0)

    def test_launcher_that_ignores_the_gate_slot_is_journaled_as_bypassed(self) -> None:
        """An out-of-process-style launcher that never enters its hold.

        SubprocessFeatureRunLauncher cannot carry the in-process hold across
        a subprocess boundary, so a node it launches can complete
        successfully without ever acquiring the exclusive slot. That must
        not look, in the journal, like a run in which no node needed the
        slot at all.
        """

        def launcher(request):
            # Never touches request.verification_gate_slot.
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph = self.graph({"a": [], "b": []}, launcher, max_parallelism=2)
        result = graph.run()
        self.assertEqual(result.status, "succeeded")

        events = self.journal_events(graph)
        bypassed = {
            event["payload"]["plan_node_id"] for event in events
            if event.get("event_type") == "plan_graph_gate_slot_bypassed"
        }
        self.assertEqual(bypassed, {"a", "b"})
        acquired = [
            event for event in events
            if event.get("event_type") == "plan_graph_gate_slot_acquired"
        ]
        self.assertEqual(acquired, [])

    def test_launcher_that_uses_the_gate_slot_is_not_journaled_as_bypassed(self) -> None:
        barrier = threading.Barrier(2, timeout=20)

        def launcher(request):
            barrier.wait()
            with (request.verification_gate_slot or nullcontext()):
                pass
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt",
                request.plan_node_id,
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph = self.graph({"a": [], "b": []}, launcher, max_parallelism=2)
        result = graph.run()
        self.assertEqual(result.status, "succeeded")

        events = self.journal_events(graph)
        bypassed = [
            event for event in events
            if event.get("event_type") == "plan_graph_gate_slot_bypassed"
        ]
        self.assertEqual(bypassed, [])
        acquired = {
            event["payload"]["plan_node_id"] for event in events
            if event.get("event_type") == "plan_graph_gate_slot_acquired"
        }
        self.assertEqual(acquired, {"a", "b"})


class ReadySetWithheldTests(unittest.TestCase):
    """Admission-only unit tests for the ``withheld`` selection exclusion."""

    def scheduler(self, *, max_parallelism: int = 2) -> ReadySetScheduler:
        return ReadySetScheduler(
            [
                _run("bad", []),
                _run("extra", []),
                _run("tail", ["bad"]),
            ],
            max_parallelism=max_parallelism,
        )

    def test_default_selection_is_unchanged(self) -> None:
        selected = self.scheduler().select(set())
        self.assertEqual(
            [unit.node_id for unit in selected], ["bad", "extra"]
        )

    def test_withheld_node_frees_its_slot_for_a_later_ready_node(self) -> None:
        selected = self.scheduler(max_parallelism=1).select(
            set(), withheld=("bad",)
        )
        self.assertEqual([unit.node_id for unit in selected], ["extra"])

    def test_withheld_node_does_not_unblock_its_dependents(self) -> None:
        selected = self.scheduler().select(set(), withheld=("bad",))
        self.assertNotIn("tail", [unit.node_id for unit in selected])

    def test_withheld_rejects_unknown_and_sealed_nodes(self) -> None:
        with self.assertRaisesRegex(PlanGraphError, "unknown"):
            self.scheduler().select(set(), withheld=("nope",))
        with self.assertRaisesRegex(PlanGraphError, "cannot be sealed"):
            self.scheduler().select({"bad"}, withheld=("bad",))


if __name__ == "__main__":
    unittest.main()
