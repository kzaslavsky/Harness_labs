"""End-to-end FeatureRun Git transaction tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_labs.attempts import TaskResult
from harness_labs.audit import AuditJournal
from harness_labs.controller_kernel import RunContract
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_scheduler import RoleProfile
from harness_labs.feature_run import ReviewFixPolicy, run_feature_worktree
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


class _ReviewRepairFactory:
    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.review_count = 0

    def __call__(self, stage, attempt):
        factory = self

        class Executor:
            def execute(self, current_attempt) -> TaskResult:
                if stage == "review":
                    factory.review_count += 1
                    findings = (
                        (
                            {
                                "id": "review-fix",
                                "file": "feature.txt",
                                "subject": "review fix",
                                "statement": "Record the reviewed candidate.",
                                "category": "correctness",
                                "severity": "major",
                                "score": 90,
                                "fix_cost": "local",
                                "protects": "acceptance criterion built",
                                "requires_disposition": True,
                            },
                        )
                        if factory.review_count == 1
                        else ()
                    )
                    return TaskResult(
                        current_attempt.attempt_id,
                        "succeeded",
                        semantic_payload(
                            summary="Review complete.",
                            details_schema="review-fix-review/1",
                            details={},
                            findings=findings,
                        ),
                    )
                key = "feature.txt:review-fix"
                if stage == "fix":
                    (factory.worktree / "reviewed.txt").write_text(
                        "reviewed\n", encoding="utf-8"
                    )
                    details = {"addressed_finding_keys": [key]}
                else:
                    details = {"verified_finding_keys": [key]}
                return TaskResult(
                    current_attempt.attempt_id,
                    "succeeded",
                    semantic_payload(
                        summary=f"{stage} complete.",
                        details_schema=f"review-fix-{stage}/1",
                        details=details,
                    ),
                )

        return Executor()


class _VerificationRepairExecutor:
    def __init__(self, worktree, seen_contexts, *, repair=True) -> None:
        self.worktree = worktree
        self.seen_contexts = seen_contexts
        self.repair = repair

    def execute(self, attempt) -> TaskResult:
        self.seen_contexts.append(json.loads(attempt.context))
        if self.repair:
            (self.worktree / "verified.txt").write_text(
                "repaired\n",
                encoding="utf-8",
            )
        return TaskResult(
            attempt.attempt_id,
            "succeeded",
            {"summary": "Applied bounded verification repair."},
        )


class FeatureRunTests(unittest.TestCase):
    def test_deterministic_verification_rejects_model_verify_phase(self) -> None:
        schema = CoordinatorDispatchSchema(
            "invalid-double-verification/1",
            (
                CoordinatorSegment(
                    id="verify",
                    phases=("verify",),
                    instructions="Ask a model to verify.",
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "coordinator verify phase"):
            run_feature_worktree(
                base_repository=Path("unused"),
                base_branch="main",
                feature_branch="feature/test",
                worktree_path=Path("unused-worktree"),
                run_dir=Path("unused-run"),
                contract_factory=lambda worktree, receipt: None,
                schema=schema,
                session_factory=lambda worktree, launch, evidence: None,
                profile_builder=lambda worktree, evidence: (),
                allowed_paths=("feature.txt",),
                commit_message="unused",
                verification_argv=("python3", "-m", "unittest"),
                verification_repair_executor_factory=lambda attempt: None,
            )

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
                allowed_paths=("feature.txt", "reviewed.txt"),
                commit_message="Build feature",
                merge=False,
                review_fix_executor_factory=_ReviewRepairFactory(root / "feature"),
                review_fix_policy=ReviewFixPolicy(),
                verification_argv=(
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('feature.txt').read_text() == 'built\\n'",
                ),
                verification_repair_executor_factory=lambda attempt: self.fail(
                    "repair must not run when deterministic verification passes"
                ),
                evidence_classification="component",
            )

            self.assertEqual(
                result.status,
                "succeeded",
                (
                    result.review_fix.reason
                    if result.review_fix
                    else result.dispatch.result.payload
                ),
            )
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(result.verification.repair_attempts, 0)
            self.assertEqual(
                [attempt["stage"] for attempt in result.verification.command_attempts],
                ["post_implementation", "post_review_repair"],
            )
            self.assertIsNotNone(result.review_fix)
            self.assertEqual(result.review_fix.cycles, 2)
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

    def test_failed_verification_repairs_same_candidate_and_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts = []
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    seen_contexts,
                ),
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [7, 0],
            )
            self.assertEqual(
                seen_contexts[0]["failed_verification"]["stdout"],
                "verification failed\n",
            )
            self.assertEqual(
                seen_contexts[0]["failed_verification"]["stderr"],
                "missing verified.txt\n",
            )
            self.assertEqual(
                (result.worktree_path / "verified.txt").read_text(encoding="utf-8"),
                "repaired\n",
            )
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create", "commit", "integrate"],
            )
            AuditJournal.verify(root / "run")

    def test_exhausted_repair_blocks_without_discarding_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_contexts = []
            result = self._run_verification_recovery_case(
                root,
                lambda worktree, attempt: _VerificationRepairExecutor(
                    worktree,
                    seen_contexts,
                    repair=False,
                ),
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.verification.status, "blocked")
            self.assertEqual(result.verification.repair_attempts, 1)
            self.assertEqual(
                [item["exit_code"] for item in result.verification.command_attempts],
                [7, 7],
            )
            self.assertEqual(
                [receipt["operation"] for receipt in result.git_receipts],
                ["create"],
            )
            self.assertEqual(
                (result.worktree_path / "feature.txt").read_text(encoding="utf-8"),
                "built\n",
            )
            self.assertTrue(result.worktree_path.exists())
            AuditJournal.verify(root / "run")

    def _run_verification_recovery_case(self, root, repair_factory):
        base = root / "base"
        base.mkdir()
        git(base, "init", "-b", "main")
        git(base, "config", "user.name", "Harness Tests")
        git(base, "config", "user.email", "harness@example.invalid")
        (base / "README.md").write_text("base\n", encoding="utf-8")
        git(base, "add", "README.md")
        git(base, "commit", "--no-gpg-sign", "-m", "Base")
        schema = CoordinatorDispatchSchema(
            "feature-verification-test/1",
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
                run_id="feature-verification-run",
                objective="Build and verify a file.",
                phases=("active",),
                criteria=(
                    {
                        "id": "built",
                        "statement": "The file is built and verified.",
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

        command = (
            "python3",
            "-c",
            "import pathlib,sys; p=pathlib.Path('verified.txt'); "
            "print('verification failed') if not p.exists() else None; "
            "print('missing verified.txt', file=sys.stderr) if not p.exists() else None; "
            "sys.exit(0 if p.exists() else 7)",
        )
        worktree = root / "feature"
        return run_feature_worktree(
            base_repository=base,
            base_branch="main",
            feature_branch="feature/test",
            worktree_path=worktree,
            run_dir=root / "run",
            contract_factory=contract_factory,
            schema=schema,
            session_factory=session_factory,
            profile_builder=lambda candidate, evidence: (
                RoleProfile(
                    "builder",
                    "builder",
                    frozenset({"repo.write"}),
                    lambda task: _BuildExecutor(task, candidate, evidence),
                ),
            ),
            allowed_paths=("feature.txt", "verified.txt"),
            commit_message="Build verified feature",
            merge=False,
            verification_argv=command,
            verification_repair_executor_factory=lambda attempt: repair_factory(
                worktree,
                attempt,
            ),
            verification_repair_limit=1,
            evidence_classification="component",
        )


if __name__ == "__main__":
    unittest.main()
