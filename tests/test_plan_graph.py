"""Deterministic acceptance tests for the minimal sequential PlanGraph."""

from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from harness_labs.plan_graph import (
    FeatureRunOutcome,
    FeatureRunRequest,
    PlanGraph,
    PlanGraphError,
    PlanGraphPlan,
    PlanRun,
    SubprocessFeatureRunLauncher,
    plan_from_mapping,
)


def plan(*runs: PlanRun) -> PlanGraphPlan:
    return PlanGraphPlan(
        plan="docs/development/APPROVED_PLAN.md",
        base_commit="base",
        runs=runs,
        plan_sections={
            "1": "Build A. AC-1: A works.",
            "2": "Build B. AC-2: B works.",
        },
        acceptance_criteria={"AC-1": "A works.", "AC-2": "B works."},
    )


class PlanGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._state_counter = 0

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _legacy_graph(self, *args, **kwargs) -> PlanGraph:
        self._state_counter += 1
        return PlanGraph(
            *args,
            state_path=(
                Path(self._temporary_directory.name) / f"legacy-{self._state_counter}.json"
            ),
            **kwargs,
        )

    @patch("harness_labs.plan_graph.subprocess.run")
    def test_subprocess_launcher_keeps_one_graph_run_moving(self, run) -> None:
        commits = iter(("A-commit", "B-commit"))

        def respond(*args, **kwargs):
            request = json.loads(kwargs["input"])
            commit = next(commits)
            self.assertEqual(
                request["base_commit"],
                "base" if commit == "A-commit" else "A-commit",
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"status": "succeeded", "candidate_commit": commit}
                ),
                stderr="",
            )

        run.side_effect = respond
        result = self._legacy_graph(
            plan(
                PlanRun("A", "Build A", ("1",), ("AC-1",)),
                PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",)),
            ),
            SubprocessFeatureRunLauncher(("feature-run", "--json")),
        ).run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.candidate_commit, "B-commit")
        self.assertEqual(run.call_count, 2)

    @patch("harness_labs.plan_graph.subprocess.run")
    def test_subprocess_launcher_returns_concise_failure_evidence(self, run) -> None:
        run.return_value = SimpleNamespace(
            returncode=7,
            stdout="",
            stderr="bounded implementation failed",
        )
        outcome = SubprocessFeatureRunLauncher(("feature-run",))(
            FeatureRunRequest(
                PlanRun("A", "Build A", ("1",), ("AC-1",)),
                "base",
                "plan.md",
            )
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.evidence["exit_code"], 7)
        self.assertIn("bounded implementation failed", outcome.evidence["stderr"])

    def test_dependency_candidate_and_final_test(self) -> None:
        calls = []
        tests = []

        def launcher(request):
            calls.append((request.run.id, request.base_commit))
            return FeatureRunOutcome("succeeded", f"{request.run.id}-commit")

        graph = self._legacy_graph(
            plan(
                PlanRun("A", "Build A", ("1",), ("AC-1",)),
                PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",)),
            ),
            launcher,
            functionality_tests=("test final",),
            functionality_test_runner=lambda command, commit: tests.append((command, commit)),
        )

        result = graph.run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.candidate_commit, "B-commit")
        self.assertEqual(calls, [("A", "base"), ("B", "A-commit")])
        self.assertEqual(tests, [("test final", "B-commit")])

    def test_mapping_carries_run_verification_and_final_functionality(self) -> None:
        payload = {
            "plan": "docs/development/APPROVED_PLAN.md",
            "base_commit": "base",
            "runs": [
                {
                    "id": "A",
                    "objective": "Build A",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "verification_argv": ["python3", "scripts/ui_walk.py"],
                }
            ],
            "plan_sections": {"1": "Build A. AC-1: A works."},
            "acceptance_criteria": {"AC-1": "A works."},
            "functionality_tests": ["python3 scripts/ui_walk.py"],
        }
        requests = []
        final_tests = []
        graph = self._legacy_graph(
            plan_from_mapping(payload),
            lambda request: (
                requests.append(request)
                or FeatureRunOutcome("succeeded", "candidate")
            ),
            functionality_test_runner=lambda command, commit: final_tests.append(
                (command, commit)
            ),
        )

        result = graph.run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            requests[0].run.verification_argv,
            ("python3", "scripts/ui_walk.py"),
        )
        self.assertEqual(final_tests, [("python3 scripts/ui_walk.py", "candidate")])

    def test_sequential_candidate_includes_multiple_roots_and_dependencies(self) -> None:
        calls = []
        queue_plan = PlanGraphPlan(
            plan="docs/development/APPROVED_PLAN.md",
            base_commit="base",
            runs=(
                PlanRun("A", "Build A", ("1",), ("AC-1",)),
                PlanRun("B", "Build B", ("2",), ("AC-2",)),
                PlanRun("C", "Build C", ("3",), ("AC-3",), ("A", "B")),
            ),
            plan_sections={
                "1": "Build A. AC-1: A works.",
                "2": "Build B. AC-2: B works.",
                "3": "Build C. AC-3: C works.",
            },
            acceptance_criteria={
                "AC-1": "A works.",
                "AC-2": "B works.",
                "AC-3": "C works.",
            },
        )

        result = self._legacy_graph(
            queue_plan,
            lambda request: (
                calls.append((request.run.id, request.base_commit))
                or FeatureRunOutcome("succeeded", f"{request.run.id}-commit")
            ),
        ).run()

        self.assertEqual(result.candidate_commit, "C-commit")
        self.assertEqual(
            calls,
            [("A", "base"), ("B", "A-commit"), ("C", "B-commit")],
        )

    def test_cited_sections_constrain_objective_and_criterion(self) -> None:
        invalid_plans = (
            plan(PlanRun("A", "Unapproved work", ("1",), ("AC-1",))),
            PlanGraphPlan(
                plan="docs/development/APPROVED_PLAN.md",
                base_commit="base",
                runs=(PlanRun("A", "Build A", ("1",), ("AC-1",)),),
                plan_sections={"1": "Build A."},
                acceptance_criteria={"AC-1": "A works."},
            ),
        )
        for invalid in invalid_plans:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(PlanGraphError, "absent from"):
                    self._legacy_graph(
                        invalid, lambda request: FeatureRunOutcome("succeeded")
                    ).run()

    @patch("harness_labs.plan_graph.subprocess.run")
    def test_default_functionality_test_uses_candidate_checkout(self, run) -> None:
        run.return_value.returncode = 0

        from harness_labs.plan_graph import _run_functionality_test

        _run_functionality_test("test final", "candidate-commit")

        clone, checkout, test = run.call_args_list
        self.assertEqual(clone.args[0][:4], ["git", "clone", "--shared", "--no-checkout"])
        self.assertEqual(checkout.args[0][-1], "candidate-commit")
        self.assertEqual(test.args[0], "test final")
        self.assertEqual(test.kwargs["cwd"].name, "candidate")

    def test_invalid_references_cycles_and_coverage_prevent_launch(self) -> None:
        invalid_plans = (
            plan(PlanRun("A", "Build A", ("missing",), ("AC-1",))),
            plan(PlanRun("A", "Build A", ("1",), ("AC-1",), ("B",)), PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",))),
            plan(PlanRun("A", "Build A", ("1",), ("AC-1",))),
        )
        for invalid in invalid_plans:
            with self.subTest(invalid=invalid):
                calls = []
                with self.assertRaises(PlanGraphError):
                    self._legacy_graph(
                        invalid, lambda request: calls.append(request)
                    ).run()
                self.assertEqual(calls, [])

    def test_failure_stops_dependents(self) -> None:
        calls = []

        def launcher(request):
            calls.append(request.run.id)
            return FeatureRunOutcome("failed")

        result = self._legacy_graph(
            plan(
                PlanRun("A", "Build A", ("1",), ("AC-1",)),
                PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",)),
            ),
            launcher,
        ).run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_run_id, "A")
        self.assertEqual(calls, ["A"])

    def test_restart_skips_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "plan-state.json"
            first_calls = []

            def first_launcher(request):
                first_calls.append(request.run.id)
                return FeatureRunOutcome(
                    "succeeded", "A-commit" if request.run.id == "A" else None
                ) if request.run.id == "A" else FeatureRunOutcome("failed")

            queue_plan = plan(
                PlanRun("A", "Build A", ("1",), ("AC-1",)),
                PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",)),
            )
            first = PlanGraph(queue_plan, first_launcher, state_path=state).run()
            self.assertEqual(first.failed_run_id, "B")
            self.assertEqual(first_calls, ["A", "B"])

            second_calls = []
            second = PlanGraph(
                queue_plan,
                lambda request: (
                    second_calls.append((request.run.id, request.base_commit))
                    or FeatureRunOutcome("succeeded", "B-commit")
                ),
                state_path=state,
            ).run()
            self.assertEqual(second.status, "succeeded")
            self.assertEqual(second_calls, [("B", "A-commit")])

    def test_interchangeable_launchers_need_no_graph_change(self) -> None:
        queue_plan = plan(PlanRun("A", "Build A", ("1",), ("AC-1",)), PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",)))
        for prefix in ("one", "two"):
            with self.subTest(prefix=prefix):
                result = self._legacy_graph(
                    queue_plan,
                    lambda request, prefix=prefix: FeatureRunOutcome("succeeded", f"{prefix}-{request.run.id}"),
                ).run()
                self.assertEqual(result.candidate_commit, f"{prefix}-B")

    def test_resume_rejects_a_completed_dependent_without_its_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "plan-state.json"
            state.write_text('{"completed": {"B": "B-commit"}}\n', encoding="utf-8")
            with self.assertRaises(PlanGraphError):
                PlanGraph(
                    plan(
                        PlanRun("A", "Build A", ("1",), ("AC-1",)),
                        PlanRun("B", "Build B", ("2",), ("AC-2",), ("A",)),
                    ),
                    lambda request: FeatureRunOutcome("succeeded", "unused"),
                    state_path=state,
                ).run()


if __name__ == "__main__":
    unittest.main()
