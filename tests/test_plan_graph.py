"""Deterministic acceptance tests for registered PlanGraph execution."""

from __future__ import annotations

from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness_labs.plan_graph import (
    FEATURE_RUN_REQUEST_PROTOCOL,
    FeatureRunOutcome,
    FeatureRunRequest,
    PlanGraph,
    PlanGraphError,
    PlanGraphRegistration,
    PlanRun,
    ReadySetDispatch,
    ReadySetScheduler,
    SubprocessFeatureRunLauncher,
    load_registration,
    persist_registration,
    register_plan_graph,
    registration_bytes,
    verify_registration,
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


def decomposition(base_commit: str, *, functionality_tests=()) -> dict[str, object]:
    return {
        "plan": "docs/approved-plan.md",
        "base_commit": base_commit,
        "runs": [
            {
                "id": "a",
                "objective": "Build A",
                "plan_sections": ["1"],
                "criteria": ["AC-1"],
                "depends_on": [],
                "verification_argv": ["python3", "-m", "unittest"],
            },
            {
                "id": "b",
                "objective": "Build B",
                "plan_sections": ["2"],
                "criteria": ["AC-2"],
                "depends_on": ["a"],
                "verification_argv": ["python3", "-m", "unittest"],
            },
        ],
        "plan_sections": {
            "1": "Build A. AC-1: A works.",
            "2": "Build B. AC-2: B works.",
        },
        "acceptance_criteria": {"AC-1": "A works.", "AC-2": "B works."},
        "functionality_tests": list(functionality_tests),
    }


class PlanGraphTests(unittest.TestCase):
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
        self.payload = decomposition(self.base_commit)
        self.registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=self.payload,
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph(self, launcher, *, registration=None, **options) -> PlanGraph:
        self.counter += 1

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
            registration or self.registration,
            correlated,
            run_root=self.root / "runs",
            graph_run_id=f"attempt-{self.counter}",
            **options,
        )

    def test_same_commit_is_deterministic_across_worktrees(self) -> None:
        worktree = self.root / "worktree"
        git(self.repository, "worktree", "add", "--detach", str(worktree), self.base_commit)
        other = register_plan_graph(
            repository=worktree,
            logical_graph_id="example-graph",
            decomposition=self.payload,
        )
        self.assertEqual(registration_bytes(other), registration_bytes(self.registration))

    def test_every_semantic_change_changes_digest(self) -> None:
        changes = []
        for mutate in (
            lambda value: value["runs"][0].update(objective="Build A more"),
            lambda value: value["runs"].reverse(),
            lambda value: value["runs"][1].update(depends_on=[]),
            lambda value: value["runs"][0].update(verification_argv=["true"]),
            lambda value: value.update(functionality_tests=["true"]),
        ):
            changed = json.loads(json.dumps(self.payload))
            mutate(changed)
            if changed["runs"][0]["objective"] == "Build A more":
                changed["plan_sections"][changed["runs"][0]["plan_sections"][0]] += " Build A more"
            changes.append(changed)
        for changed in changes:
            with self.subTest(changed=changed):
                candidate = register_plan_graph(
                    repository=self.repository,
                    logical_graph_id="example-graph",
                    decomposition=changed,
                )
                self.assertNotEqual(candidate.graph_digest, self.registration.graph_digest)

    def test_identity_is_deliberately_part_of_digest(self) -> None:
        other = register_plan_graph(
            repository=self.repository,
            logical_graph_id="other-graph",
            decomposition=self.payload,
        )
        self.assertNotEqual(other.graph_digest, self.registration.graph_digest)

    def test_registration_reads_git_not_dirty_worktree(self) -> None:
        (self.repository / "docs" / "approved-plan.md").write_text("dirty\n")
        dirty = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=self.payload,
        )
        self.assertEqual(dirty.plan_sha256, self.registration.plan_sha256)
        changed = dict(self.payload, plan="docs/untracked.md")
        (self.repository / "docs" / "untracked.md").write_text("untracked\n")
        with self.assertRaises(PlanGraphError):
            register_plan_graph(
                repository=self.repository,
                logical_graph_id="untracked-graph",
                decomposition=changed,
            )

    def test_invalid_paths_and_ids_are_rejected_before_attempt(self) -> None:
        for plan in ("/absolute.md", "../escape.md", "docs/./approved-plan.md"):
            with self.subTest(plan=plan), self.assertRaises(PlanGraphError):
                register_plan_graph(
                    repository=self.repository,
                    logical_graph_id="example-graph",
                    decomposition=dict(self.payload, plan=plan),
                )
        with self.assertRaises(ValueError):
            register_plan_graph(
                repository=self.repository,
                logical_graph_id="Bad/Graph",
                decomposition=self.payload,
            )
        with self.assertRaises(ValueError):
            PlanGraph(
                self.repository,
                self.registration,
                lambda request: FeatureRunOutcome("failed"),
                run_root=self.root / "runs",
                graph_run_id="../bad",
            )
        self.assertFalse((self.root / "runs").exists())

    def test_persistence_is_canonical_idempotent_and_collision_safe(self) -> None:
        root = self.root / "registrations"
        path = persist_registration(
            repository=self.repository,
            registration_root=root,
            registration=self.registration,
        )
        again = persist_registration(
            repository=self.repository,
            registration_root=root,
            registration=self.registration,
        )
        self.assertEqual(path, again)
        self.assertEqual(load_registration(path), self.registration)
        different = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=dict(self.payload, functionality_tests=["true"]),
        )
        with self.assertRaisesRegex(PlanGraphError, "already registered differently"):
            persist_registration(
                repository=self.repository,
                registration_root=root,
                registration=different,
            )

    def test_concurrent_different_publications_have_one_winner(self) -> None:
        root = self.root / "concurrent-registrations"
        different = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=dict(self.payload, functionality_tests=["true"]),
        )

        def publish(registration):
            try:
                persist_registration(
                    repository=self.repository,
                    registration_root=root,
                    registration=registration,
                )
                return "published"
            except PlanGraphError:
                return "collision"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(publish, (self.registration, different))
            )
        self.assertCountEqual(outcomes, ["published", "collision"])
        winner = load_registration(root / "example-graph.json")
        self.assertIn(
            winner.graph_digest,
            {self.registration.graph_digest, different.graph_digest},
        )

    def test_registration_tampering_is_rejected(self) -> None:
        variants = (
            replace(self.registration, protocol="unknown/1"),
            replace(self.registration, graph_digest="0" * 64),
            replace(self.registration, plan_sha256="0" * 64),
            replace(self.registration, definition_json="{ \"runs\": [] }"),
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(PlanGraphError):
                verify_registration(self.repository, variant)
        payload = asdict(self.registration)
        payload["unknown"] = True
        path = self.root / "bad.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(PlanGraphError):
            load_registration(path)

    @patch("harness_labs.plan_graph.subprocess.run")
    def test_subprocess_wire_contract_keeps_both_commit_meanings(self, run) -> None:
        request = FeatureRunRequest(
            FEATURE_RUN_REQUEST_PROTOCOL,
            PlanRun("a", "Build A", ("1",), ("AC-1",)),
            "candidate",
            "docs/approved-plan.md",
            self.base_commit,
            self.registration.plan_sha256,
            "attempt-1",
            "a",
            "attempt-1-a",
            self.root / "runs" / "attempt-1-a",
        )
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "failed"}),
            stderr="",
        )
        SubprocessFeatureRunLauncher(("launcher",))(request)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["protocol"], FEATURE_RUN_REQUEST_PROTOCOL)
        self.assertEqual(payload["base_commit"], "candidate")
        self.assertEqual(payload["plan_base_commit"], self.base_commit)
        self.assertEqual(payload["plan_sha256"], self.registration.plan_sha256)

    def test_sequential_candidate_and_registered_functionality_test(self) -> None:
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="with-final-test",
            decomposition=dict(self.payload, functionality_tests=["test final"]),
        )
        calls = []
        tests = []
        result = self.graph(
            lambda request: calls.append((request.run.id, request.base_commit))
            or FeatureRunOutcome("succeeded", f"{request.run.id}-commit"),
            registration=registration,
            functionality_test_runner=lambda command, commit: tests.append((command, commit)),
        ).run()
        self.assertEqual(result.candidate_commit, "b-commit")
        self.assertEqual(calls, [("a", self.base_commit), ("b", "a-commit")])
        self.assertEqual(tests, [("test final", "b-commit")])
        with self.assertRaises(TypeError):
            PlanGraph(
                self.repository,
                registration,
                lambda request: FeatureRunOutcome("failed"),
                run_root=self.root / "runs",
                functionality_tests=("runtime addition",),
            )

    @patch("harness_labs.plan_graph.subprocess.run")
    def test_default_functionality_clone_uses_explicit_repository(self, run) -> None:
        run.return_value.returncode = 0
        from harness_labs.plan_graph import _run_functionality_test

        _run_functionality_test(self.repository, "true", self.base_commit)
        clone = run.call_args_list[0]
        self.assertEqual(clone.args[0][-2], str(self.repository))

    def test_request_requires_initialized_audit(self) -> None:
        graph = self.graph(lambda request: FeatureRunOutcome("failed"))
        with self.assertRaisesRegex(PlanGraphError, "initialized PlanGraph audit"):
            graph._request_for_run(graph.plan.runs[0], self.base_commit)

    def test_failure_stops_dependents_and_identity_mismatch_fails(self) -> None:
        calls = []
        result = self.graph(
            lambda request: calls.append(request.run.id) or FeatureRunOutcome("failed")
        ).run()
        self.assertEqual((result.status, result.failed_run_id, calls), ("failed", "a", ["a"]))
        self.counter += 1
        mismatch = PlanGraph(
            self.repository,
            self.registration,
            lambda request: FeatureRunOutcome("succeeded", "bad-commit"),
            run_root=self.root / "runs",
            graph_run_id=f"attempt-{self.counter}",
        ).run()
        self.assertEqual(mismatch.status, "failed")

    def test_retired_importer_refuses_without_creating_state(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "import_plan_graph_state.py"
        completed = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("incompatible", completed.stderr)


if __name__ == "__main__":
    unittest.main()
