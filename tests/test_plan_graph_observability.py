"""Acceptance tests for registered PlanGraph audit and resume boundaries."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    register_plan_graph,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


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
                "registration_protocol": "plan-graph-registration/1",
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
                "parent_correlation",
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
