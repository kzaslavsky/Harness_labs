"""Acceptance tests for durable and correlated PlanGraph execution."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from harness_labs.audit import AuditJournal
from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    PlanGraphPlan,
    PlanRun,
)


def _plan(path: Path) -> PlanGraphPlan:
    path.write_text("approved plan\n", encoding="utf-8")
    return PlanGraphPlan(
        plan=str(path),
        base_commit="base",
        runs=(
            PlanRun("first", "First", ("1",), ("AC-1",)),
            PlanRun("second", "Second", ("2",), ("AC-2",), ("first",)),
        ),
        plan_sections={"1": "First AC-1", "2": "Second AC-2"},
        acceptance_criteria={"AC-1": "AC-1", "AC-2": "AC-2"},
    )


def _success(request, commit: str) -> FeatureRunOutcome:
    return FeatureRunOutcome(
        "succeeded",
        commit,
        plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id,
        feature_run_id=request.feature_run_id,
        run_dir=str(request.run_dir),
    )


class PlanGraphObservabilityTests(unittest.TestCase):
    def test_new_graph_requires_a_durable_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(PlanGraphError, "run_root is required"):
                PlanGraph(
                    _plan(root / "plan.md"),
                    lambda request: _success(request, "unused"),
                )

    def test_graph_id_cannot_resolve_to_the_run_root_or_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for graph_run_id in (".", ".."):
                with self.subTest(graph_run_id=graph_run_id):
                    with self.assertRaisesRegex(PlanGraphError, "path-safe"):
                        PlanGraph(
                            _plan(root / f"{graph_run_id}.md"),
                            lambda request: _success(request, "unused"),
                            run_root=root / "runs",
                            graph_run_id=graph_run_id,
                        ).run()

    def test_new_graph_binds_descriptor_and_durable_node_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = []
            result = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: requests.append(request) or _success(
                    request, f"{request.run.id}-commit"
                ),
                run_root=root / "logs" / "runs",
                graph_run_id="graph-1",
            ).run()

            run_dir = root / "logs" / "runs" / "graph-1"
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            descriptor = json.loads((run_dir / "descriptor.json").read_text())
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(descriptor["run_id"], "graph-1")
            self.assertEqual(
                checkpoint["state"]["nodes"]["first"]["status"], "succeeded"
            )
            self.assertEqual(
                checkpoint["state"]["nodes"]["second"]["candidate_commit"],
                "second-commit",
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in events
                    if event["event_type"].startswith("plan_")
                ],
                [
                    "plan_graph_initialized",
                    "plan_node_started",
                    "plan_node_completed",
                    "plan_node_started",
                    "plan_node_completed",
                    "plan_graph_completed",
                ],
            )
            self.assertEqual(AuditJournal.verify(run_dir)["run_id"], "graph-1")
            self.assertEqual(requests[0].plan_graph_id, "graph-1")
            self.assertEqual(requests[0].plan_node_id, "first")
            self.assertEqual(requests[0].feature_run_id, "graph-1-first")
            self.assertEqual(
                requests[0].run_dir,
                (root / "logs" / "runs" / "graph-1-first").resolve(),
            )

    def test_resume_uses_durable_successful_node_not_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_plan = _plan(root / "plan.md")
            with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                PlanGraph(
                    queue_plan,
                    lambda request: (
                        _success(request, "first-commit")
                        if request.run.id == "first"
                        else (_ for _ in ()).throw(RuntimeError("controller stopped"))
                    ),
                    run_root=root / "runs",
                    graph_run_id="graph-resume",
                ).run()
            calls = []
            second = PlanGraph(
                queue_plan,
                lambda request: calls.append(request.run.id) or _success(request, "second-commit"),
                run_root=root / "runs",
                graph_run_id="graph-resume",
            ).run()
            self.assertEqual(second.status, "succeeded")
            self.assertEqual(calls, ["second"])

    def test_resume_rejects_a_changed_decomposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_plan = _plan(root / "plan.md")
            with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                PlanGraph(
                    queue_plan,
                    lambda request: (
                        _success(request, "first-commit")
                        if request.run.id == "first"
                        else (_ for _ in ()).throw(RuntimeError("controller stopped"))
                    ),
                    run_root=root / "runs",
                    graph_run_id="graph-plan-binding",
                ).run()
            changed_plan = PlanGraphPlan(
                plan=queue_plan.plan,
                base_commit=queue_plan.base_commit,
                runs=(
                    queue_plan.runs[0],
                    PlanRun("second", "Second", ("2",), ("AC-2",)),
                ),
                plan_sections=queue_plan.plan_sections,
                acceptance_criteria=queue_plan.acceptance_criteria,
            )
            with self.assertRaisesRegex(PlanGraphError, "does not match the supplied plan"):
                PlanGraph(
                    changed_plan,
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-plan-binding",
                ).run()

    def test_resume_rejects_changed_launch_or_final_test_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_plan = _plan(root / "plan.md")
            first = PlanGraph(
                queue_plan,
                lambda request: _success(request, f"{request.run.id}-commit"),
                run_root=root / "runs",
                graph_run_id="graph-contract-binding",
            ).run()
            self.assertEqual(first.status, "succeeded")
            changed_plan = PlanGraphPlan(
                plan=queue_plan.plan,
                base_commit=queue_plan.base_commit,
                runs=(
                    PlanRun(
                        "first",
                        "First",
                        ("1",),
                        ("AC-1",),
                        verification_argv=("verify-first",),
                    ),
                    queue_plan.runs[1],
                ),
                plan_sections=queue_plan.plan_sections,
                acceptance_criteria=queue_plan.acceptance_criteria,
                functionality_tests=("verify final",),
            )
            with self.assertRaisesRegex(PlanGraphError, "does not match the supplied plan"):
                PlanGraph(
                    changed_plan,
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-contract-binding",
                ).run()

    def test_mismatched_child_identity_is_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: FeatureRunOutcome("succeeded", "bad-commit"),
                run_root=root / "runs",
                graph_run_id="graph-mismatch",
            ).run()
            self.assertEqual(result.status, "failed")
            checkpoint = json.loads(
                (root / "runs" / "graph-mismatch" / "checkpoint.json").read_text()
            )
            self.assertEqual(
                checkpoint["state"]["nodes"]["first"]["status"], "failed"
            )


if __name__ == "__main__":
    unittest.main()
