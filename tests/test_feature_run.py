"""End-to-end FeatureRun Git transaction tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_labs.attempts import TaskResult
from harness_labs.audit import AuditJournal
from harness_labs.controller_kernel import RunContract
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_scheduler import RoleProfile
from harness_labs.feature_run import run_feature_worktree
from harness_labs.coordinator_schema import (
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)
from tests.controller_scenario_fixtures import ScriptedCoordinatorSession


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


class _BuildExecutor:
    def __init__(self, task, worktree, evidence) -> None:
        self.task = task
        self.worktree = worktree
        self.evidence = evidence

    def execute(self, attempt) -> TaskResult:
        (self.worktree / "feature.txt").write_text("built\n", encoding="utf-8")
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built feature.txt\n",
            media_type="text/markdown",
            producer_task_id=self.task["id"],
        )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built.",
                details_schema=self.task["details_schema"],
                details={"paths": ["feature.txt"]},
                artifacts=(artifact.as_dict(),),
                criterion_coverage=(
                    {
                        "criterion_id": "built",
                        "status": "satisfied",
                        "evidence_refs": [artifact.ref],
                    },
                ),
            ),
            evidence=(artifact.ref,),
        )


class FeatureRunTests(unittest.TestCase):
    def test_feature_run_creates_commits_and_leaves_merge_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            base.mkdir()
            git(base, "init", "-b", "main")
            git(base, "config", "user.name", "Harness Tests")
            git(base, "config", "user.email", "harness@example.invalid")
            (base / "README.md").write_text("base\n", encoding="utf-8")
            git(base, "add", "README.md")
            git(base, "commit", "--no-gpg-sign", "-m", "Base")
            base_head = git(base, "rev-parse", "HEAD")
            schema = CoordinatorDispatchSchema(
                "feature-test/1",
                (
                    CoordinatorSegment(
                        id="active",
                        phases=("active",),
                        instructions="Build and complete.",
                    ),
                ),
            )

            def contract_factory(worktree, receipt):
                return RunContract(
                    run_id="feature-run",
                    objective="Build a file.",
                    phases=("active",),
                    criteria=(
                        {
                            "id": "built",
                            "statement": "The file is built.",
                            "source": "operator",
                        },
                    ),
                    terminal_artifact_kinds=("implementation-summary",),
                    repository={
                        "path": str(worktree),
                        "branch": receipt["feature_branch"],
                        "base_branch": receipt["base_branch"],
                        "base_commit": receipt["base_commit"],
                    },
                )

            def session_factory(worktree, launch, evidence):
                return ScriptedCoordinatorSession(
                    [
                        (
                            "task_dispatch",
                            {
                                "tasks": [
                                    {
                                        "id": "build",
                                        "role": "builder",
                                        "objective": "Build feature.txt",
                                        "details_schema": "build/1",
                                        "required_capabilities": ["repo.write"],
                                        "acceptance_criteria": ["built"],
                                        "dependencies": [],
                                    }
                                ],
                                "max_parallelism": 1,
                            },
                        ),
                        ("run_complete_request", {}),
                    ],
                    final="Complete.",
                )

            result = run_feature_worktree(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/test",
                worktree_path=root / "feature",
                run_dir=root / "run",
                contract_factory=contract_factory,
                schema=schema,
                session_factory=session_factory,
                profile_builder=lambda worktree, evidence: (
                    RoleProfile(
                        "builder",
                        "builder",
                        frozenset({"repo.write"}),
                        lambda task: _BuildExecutor(task, worktree, evidence),
                    ),
                ),
                allowed_paths=("feature.txt",),
                commit_message="Build feature",
                merge=False,
                evidence_classification="component",
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create", "commit", "integrate"],
            )
            self.assertEqual(result.git_receipts[-1]["status"], "ready_not_merged")
            self.assertEqual(git(base, "rev-parse", "HEAD"), base_head)
            self.assertNotEqual(
                git(result.worktree_path, "rev-parse", "HEAD"),
                base_head,
            )
            AuditJournal.verify(root / "run")


if __name__ == "__main__":
    unittest.main()
