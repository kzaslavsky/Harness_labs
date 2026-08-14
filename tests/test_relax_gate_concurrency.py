"""Red/green finding test for CB2-03: exclusive gate execution slot.

Item 14 (load half), the CB-06 failure mode: admitting more than one node
concurrently must never let their deterministic verification commands run
at the same time, since that silently halves the gate's effective
wall-clock budget. This file is self-contained (duplicates the small ready-
set test scaffold rather than importing it) so the gate can run it in
isolation.

Behavioral red on the frozen base harness: ``FeatureRunRequest`` has no
verification-serialization mechanism at all, so two admitted siblings'
stub verification commands are observed executing with overlapping wall-
clock spans, and the graph journal records zero gate-slot events. On the
candidate, a graph-owned exclusive slot threaded through the request
serializes exactly the verification-command critical section (dispatch,
review, fix stay concurrent) and the acquisition/release is journaled per
node with the admitted-concurrency count.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from harness_labs.plangraph.plan_graph import (
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


def run_payload(node_id: str) -> dict[str, object]:
    return {
        "id": node_id,
        "objective": f"Build {node_id}",
        "plan_sections": [node_id],
        "criteria": [f"AC-{node_id}"],
        "depends_on": [],
        "verification_argv": ["python3", "-m", "unittest"],
    }


def decomposition(base_commit: str, node_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "plan": "docs/approved-plan.md",
        "base_commit": base_commit,
        "runs": [run_payload(node_id) for node_id in node_ids],
        "plan_sections": {
            node_id: f"Build {node_id}. AC-{node_id}: {node_id} works."
            for node_id in node_ids
        },
        "acceptance_criteria": {
            f"AC-{node_id}": f"{node_id} works." for node_id in node_ids
        },
        "functionality_tests": [],
    }


class ExclusiveGateSlotConcurrencyTests(unittest.TestCase):
    """Two independent roots, admitted together under max_parallelism=2."""

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
        self.runs_root = self.root / "runs"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit_file(self, base: str, name: str, content: str) -> str:
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

    def build_graph(
        self,
        launcher,
        graph_run_id: str,
        *,
        max_parallelism: int,
        node_ids: tuple[str, ...] = ("v1", "v2"),
    ) -> PlanGraph:
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id=f"gate-slot-{graph_run_id}",
            decomposition=decomposition(self.base_commit, node_ids),
        )

        def correlated(request):
            # Mirrors how a real launcher binds the reserved child identity
            # onto its outcome; the ready-set scheduler rejects an outcome
            # that does not name the request it answers.
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
            run_root=self.runs_root,
            graph_run_id=graph_run_id,
            max_parallelism=max_parallelism,
        )

    def journal_events(self, graph_run_id: str) -> list[dict[str, object]]:
        events_path = self.runs_root / graph_run_id / "events.jsonl"
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_verification_commands_never_overlap_under_parallel_admission(self) -> None:
        """Two admitted siblings' stub verification commands must not overlap.

        Behavioral red on base: nothing serializes verification-command
        execution across concurrently admitted nodes, so both siblings'
        0.3s "verification" windows are observed overlapping.
        """
        barrier = threading.Barrier(2, timeout=20)
        spans: dict[str, tuple[float, float]] = {}
        spans_lock = threading.Lock()

        def launcher(request):
            slot = getattr(request, "verification_gate_slot", None)
            # Both siblings reach their verification stage together; a
            # serializing slot forces one to wait out the other's window,
            # while an absent slot lets both windows run concurrently.
            barrier.wait()
            with (slot or nullcontext()):
                start = time.monotonic()
                time.sleep(0.3)
                end = time.monotonic()
            with spans_lock:
                spans[request.plan_node_id] = (start, end)
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt", request.plan_node_id
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph_run_id = "concurrency-attempt"
        result = self.build_graph(launcher, graph_run_id, max_parallelism=2).run()
        self.assertEqual(result.status, "succeeded")

        self.assertEqual(set(spans), {"v1", "v2"})
        (start_v1, end_v1) = spans["v1"]
        (start_v2, end_v2) = spans["v2"]
        overlap = start_v1 < end_v2 and start_v2 < end_v1
        self.assertFalse(
            overlap,
            "sibling verification-command spans overlapped under parallel "
            "admission; the gate's effective wall-clock budget was silently "
            "halved",
        )

    def test_gate_slot_acquisition_and_release_are_journaled_per_node(self) -> None:
        """The graph journal must record both nodes' slot acquire/release.

        Behavioral red on base: the journal has zero gate-slot events
        because no such mechanism exists.
        """
        barrier = threading.Barrier(2, timeout=20)

        def launcher(request):
            slot = getattr(request, "verification_gate_slot", None)
            barrier.wait()
            with (slot or nullcontext()):
                time.sleep(0.05)
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt", request.plan_node_id
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph_run_id = "journal-attempt"
        result = self.build_graph(launcher, graph_run_id, max_parallelism=2).run()
        self.assertEqual(result.status, "succeeded")

        events = self.journal_events(graph_run_id)
        acquired = [
            event for event in events
            if event.get("event_type") == "plan_graph_gate_slot_acquired"
        ]
        released = [
            event for event in events
            if event.get("event_type") == "plan_graph_gate_slot_released"
        ]
        self.assertEqual(
            {event["payload"]["plan_node_id"] for event in acquired}, {"v1", "v2"}
        )
        self.assertEqual(
            {event["payload"]["plan_node_id"] for event in released}, {"v1", "v2"}
        )
        for event in acquired + released:
            concurrency = event["payload"]["admitted_concurrency"]
            self.assertIsInstance(concurrency, int)
            self.assertGreaterEqual(concurrency, 1)

    def test_max_parallelism_one_stays_byte_identical(self) -> None:
        """Sequential execution emits no slot events and no diverging field.

        Guards AC-CB203-2: with max_parallelism=1, no gate slot is created,
        no slot events are journaled, and every ``FeatureRunRequest`` the
        graph issues carries ``verification_gate_slot=None`` — the runtime
        payload matches the registered plan run exactly.
        """
        requests = []

        def launcher(request):
            requests.append(request)
            candidate = self.commit_file(
                request.base_commit, f"{request.plan_node_id}.txt", request.plan_node_id
            )
            return FeatureRunOutcome("succeeded", candidate_commit=candidate)

        graph_run_id = "sequential-attempt"
        graph = self.build_graph(
            launcher, graph_run_id, max_parallelism=1, node_ids=("s1",)
        )
        result = graph.run()
        self.assertEqual(result.status, "succeeded")

        self.assertEqual(len(requests), 1)
        self.assertIsNone(
            getattr(requests[0], "verification_gate_slot", "MISSING-ATTRIBUTE")
        )
        events = self.journal_events(graph_run_id)
        slot_events = [
            event for event in events
            if str(event.get("event_type", "")).startswith("plan_graph_gate_slot_")
        ]
        self.assertEqual(slot_events, [])


if __name__ == "__main__":
    unittest.main()
