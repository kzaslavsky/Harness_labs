"""Finding tests for CB-05: retry-with-adoption for writable dispatches.

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it imports nothing that does not already exist at the
base commit. The base harness's writable executors accept a dirty repository
baseline whenever the ``allow_dirty_baseline`` constructor flag is set,
regardless of what is actually dirty; the candidate replaces that
constructor-frozen boolean with a per-dispatch ``dirty_baseline_grant`` that
must name an existing ``workspace-change-receipt`` evidence entry whose
recorded ``changed_paths`` covers every currently dirty path, and
``harness_labs.feature_run`` supplies that grant automatically for
verification-repair and review-fix dispatches from the prior attempt's
receipt.

``CodexSemanticTaskExecutorTests`` in this file uses ``inspect.signature`` to
build constructor kwargs, because the constructor parameter itself is exactly
what changed between the base and candidate trees (``allow_dirty_baseline``
on base, ``dirty_baseline_grant`` on the candidate); this lets the same
assertions run unmodified against both signatures instead of failing with a
usage error on one of them.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

from harness_labs.core.attempts import TaskAttempt, TaskResult
from harness_labs.core.audit import AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_kernel import RunContract
from harness_labs.core.controller_live import CodexSemanticTaskExecutor
from harness_labs.core.controller_results import semantic_payload, validate_semantic_result
from harness_labs.core.controller_scheduler import RoleProfile
from harness_labs.core.coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment
from harness_labs.feature_run import ReviewFixPolicy, run_feature_worktree
from harness_labs.core.git_transaction import workspace_snapshot
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


def _dirty_paths(worktree: Path) -> list[str]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    ).stdout.split()
    return sorted(set(tracked) | set(untracked))


def _events(root: Path) -> list:
    return [
        json.loads(line)
        for line in (root / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _writable_executor_kwargs(
    executor_cls: type,
    *,
    dirty_ok: bool,
    receipt_ref: str | None,
) -> dict:
    """Adapt to whichever dirty-baseline constructor parameter actually exists.

    Base tree: ``allow_dirty_baseline`` is a blanket boolean unrelated to any
    receipt. Candidate tree: ``dirty_baseline_grant`` names a receipt that
    must actually cover the dirty paths, checked by the executor itself.
    """

    sig = inspect.signature(executor_cls.__init__)
    if "dirty_baseline_grant" in sig.parameters:
        return {
            "dirty_baseline_grant": (
                {"receipt_ref": receipt_ref} if dirty_ok else None
            )
        }
    return {"allow_dirty_baseline": dirty_ok}


_VERIFY_SCRIPT = (
    "import json, pathlib, sys\n"
    "p = pathlib.Path('failing.json')\n"
    "ids = json.loads(p.read_text()) if p.exists() else []\n"
    "for i in ids:\n"
    "    print(f'FAILED tests/test_{i}.py::test_{i} - AssertionError: still failing')\n"
    "sys.exit(1 if ids else 0)\n"
)


class _ImplementerBuildExecutor:
    """The initial writable coordinator dispatch: leaves a receipted dirty tree."""

    def __init__(self, worktree: Path, evidence: EvidenceCatalog, path: str, content: str) -> None:
        self.worktree = worktree
        self.evidence = evidence
        self.path = path
        self.content = content

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        (self.worktree / self.path).write_text(self.content, encoding="utf-8")
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built the candidate with a declared change.\n",
            media_type="text/markdown",
            producer_task_id="build",
        )
        # Real receipts (see claude_task_executor.py / controller_live.py)
        # always record actual per-file content state; the shared grant
        # verifier now checks it, so this hand-rolled receipt must too, or
        # it can never qualify as a covering receipt.
        snapshot = workspace_snapshot(self.worktree)
        receipt = self.evidence.add(
            kind="workspace-change-receipt",
            content={
                "protocol": "workspace-change-receipt/2",
                "changed_paths": [self.path],
                "files": {self.path: snapshot["files"].get(self.path)},
            },
            media_type="application/json",
            producer_task_id="build",
        )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built.",
                details_schema="build/1",
                details={"paths": [self.path]},
                artifacts=(artifact.as_dict(), receipt.as_dict()),
                criterion_coverage=(
                    {
                        "criterion_id": "built",
                        "status": "satisfied",
                        "evidence_refs": [artifact.ref],
                    },
                ),
            ),
            evidence=(artifact.ref, receipt.ref),
        )


def _refused_for_ungranted_dirty_baseline(
    executor: object,
    worktree: Path,
    evidence: EvidenceCatalog,
    attempt: TaskAttempt,
) -> TaskResult | None:
    """Reproduce the executor-level preflight for a hand-rolled test double.

    Returns the refusal ``TaskResult`` when the workspace is dirty and
    ``executor.dirty_baseline_grant`` does not name a receipt whose
    ``changed_paths`` covers every dirty path; ``None`` means proceed.
    """

    dirty = _dirty_paths(worktree)
    if not dirty:
        return None
    grant = getattr(executor, "dirty_baseline_grant", None)
    receipt_ref = grant.get("receipt_ref") if isinstance(grant, Mapping) else None
    if isinstance(receipt_ref, str) and evidence.contains(receipt_ref):
        try:
            receipt = json.loads(evidence.open(receipt_ref))
        except json.JSONDecodeError:
            receipt = None
        if isinstance(receipt, Mapping) and set(dirty) <= set(
            receipt.get("changed_paths", ())
        ):
            return None
    return TaskResult(
        attempt.attempt_id,
        "failed",
        {"error": "writable worker requires a clean repository baseline"},
    )


class _AdoptionAwareRepairExecutor:
    """A verification-repair executor honoring only an audited adoption grant."""

    dirty_baseline_grant = None

    def __init__(self, worktree: Path, evidence: EvidenceCatalog) -> None:
        self.worktree = worktree
        self.evidence = evidence
        self.calls = 0

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        self.calls += 1
        refusal = _refused_for_ungranted_dirty_baseline(
            self, self.worktree, self.evidence, attempt
        )
        if refusal is not None:
            return refusal
        (self.worktree / "failing.json").write_text(
            json.dumps([]), encoding="utf-8"
        )
        return TaskResult(
            attempt.attempt_id,
            "succeeded",
            {"summary": "Repaired within the adopted baseline."},
        )


class AdoptionRepairIntegrationTest(unittest.TestCase):
    """AC-CB05-3: a verification-repair dispatch adopts the prior receipt."""

    def test_repair_dispatch_adopts_the_prior_receipted_baseline(self) -> None:
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

            schema = CoordinatorDispatchSchema(
                "adoption-repair-test/1",
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
                    run_id="adoption-repair-run",
                    objective="Build a file and repair it in place.",
                    phases=("active",),
                    criteria=(
                        {
                            "id": "built",
                            "statement": "The declared failing test clears.",
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
                                        "objective": "Build failing.json",
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

            worktree = root / "feature"
            holder: dict = {}
            container: dict = {}

            def profile_builder(candidate, evidence):
                holder["evidence"] = evidence
                return (
                    RoleProfile(
                        "builder",
                        "builder",
                        frozenset({"repo.write"}),
                        lambda task: _ImplementerBuildExecutor(
                            candidate, evidence, "failing.json", json.dumps(["a"])
                        ),
                    ),
                )

            def repair_factory(attempt):
                if "executor" not in container:
                    container["executor"] = _AdoptionAwareRepairExecutor(
                        worktree, holder["evidence"]
                    )
                return container["executor"]

            result = run_feature_worktree(
                base_repository=base,
                base_branch="main",
                feature_branch="feature/test",
                worktree_path=worktree,
                run_dir=root / "run",
                contract_factory=contract_factory,
                schema=schema,
                session_factory=session_factory,
                profile_builder=profile_builder,
                allowed_paths=("failing.json",),
                commit_message="Build and repair with baseline adoption",
                merge=False,
                verification_argv=("python3", "-c", _VERIFY_SCRIPT),
                verification_repair_executor_factory=repair_factory,
                verification_repair_limit=1,
            )

            self.assertEqual(
                result.status,
                "succeeded",
                (
                    result.verification.reason
                    if result.verification
                    else result.dispatch.result.payload
                ),
            )
            self.assertIsNotNone(result.verification)
            self.assertEqual(result.verification.status, "succeeded")
            self.assertEqual(container["executor"].calls, 1)

            events = _events(root)
            grant_events = [
                event
                for event in events
                if event.get("event_type") == "dirty_baseline_adoption_grant_supplied"
            ]
            self.assertEqual(
                len(grant_events),
                1,
                "expected exactly one recorded adoption grant supplied to the repair dispatch",
            )
            self.assertTrue(
                str(grant_events[0]["payload"]["receipt_ref"]).startswith(
                    "artifact:sha256:"
                )
            )
            AuditJournal.verify(root / "run")


class _AdoptionAwareReviewFixFactory:
    """A review-fix executor factory whose fix stage honors only a valid grant."""

    def __init__(self) -> None:
        self.worktree: Path | None = None
        self.evidence: EvidenceCatalog | None = None
        self.fix_calls = 0
        self.review_calls = 0

    def __call__(self, stage: str, attempt: TaskAttempt):
        factory = self

        class _Executor:
            dirty_baseline_grant = None

            def execute(self, current_attempt: TaskAttempt) -> TaskResult:
                if stage == "review":
                    factory.review_calls += 1
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
                        if factory.review_calls == 1
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
                    factory.fix_calls += 1
                    refusal = _refused_for_ungranted_dirty_baseline(
                        self, factory.worktree, factory.evidence, current_attempt
                    )
                    if refusal is not None:
                        return refusal
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

        return _Executor()


class AdoptionReviewFixIntegrationTest(unittest.TestCase):
    """AC-CB05-3: a review-fix "fix" dispatch adopts the prior receipt."""

    def test_fix_dispatch_adopts_the_prior_receipted_baseline(self) -> None:
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

            schema = CoordinatorDispatchSchema(
                "adoption-review-test/1",
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
                    run_id="adoption-review-run",
                    objective="Build a file and address the review finding in place.",
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

            factory = _AdoptionAwareReviewFixFactory()

            def profile_builder(candidate, evidence):
                factory.worktree = candidate
                factory.evidence = evidence
                return (
                    RoleProfile(
                        "builder",
                        "builder",
                        frozenset({"repo.write"}),
                        lambda task: _ImplementerBuildExecutor(
                            candidate, evidence, "feature.txt", "built\n"
                        ),
                    ),
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
                profile_builder=profile_builder,
                allowed_paths=("feature.txt", "reviewed.txt"),
                commit_message="Build and address review finding with baseline adoption",
                merge=False,
                review_fix_executor_factory=factory,
                review_fix_policy=ReviewFixPolicy(),
            )

            self.assertEqual(
                result.status,
                "succeeded",
                result.review_fix.reason if result.review_fix else result.dispatch.result.payload,
            )
            self.assertIsNotNone(result.review_fix)
            self.assertEqual(result.review_fix.status, "succeeded")
            self.assertEqual(factory.fix_calls, 1)

            events = _events(root)
            grant_events = [
                event
                for event in events
                if event.get("event_type") == "dirty_baseline_adoption_grant_supplied"
            ]
            self.assertEqual(
                len(grant_events),
                1,
                "expected exactly one recorded adoption grant supplied to the fix dispatch",
            )
            AuditJournal.verify(root / "run")


def _snapshot(changed_paths=(), files=None):
    return {
        "head": "abc",
        "branch": "feature",
        "changed_paths": list(changed_paths),
        "files": dict(files or {}),
    }


class CodexSemanticTaskExecutorDirtyBaselineTests(unittest.TestCase):
    """AC-CB05-1 / AC-CB05-2: the executor's own writable preflight."""

    def _run(self, executor: CodexSemanticTaskExecutor, snapshots) -> TaskResult:
        raw = {
            "summary": "Verified.",
            "deliverable_markdown": "Verified.",
            "details_json": "{}",
            "claims": [],
            "findings": [],
            "recommendations": [],
            "unresolved_questions": [],
            "satisfied_criteria": [],
        }

        def run(argv, **kwargs):
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(json.dumps(raw), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("harness_labs.core.controller_live.shutil.which", return_value="codex"),
            patch("harness_labs.core.controller_live.subprocess.run", side_effect=run),
            patch(
                "harness_labs.core.controller_live.workspace_snapshot",
                side_effect=snapshots,
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            return executor.execute(
                TaskAttempt(
                    "fix/attempt-1",
                    "task:fix",
                    "context:fix",
                    "profile:fixer",
                )
            )

    def test_dirty_path_outside_receipted_change_set_is_refused_even_inside_writable_paths(
        self,
    ) -> None:
        # The dirty-baseline flag/grant is engaged, but the only dirty path is
        # NOT within the receipted change set -- even though it IS one of the
        # worker's own writable_paths. AC-CB05-2 requires this refused with
        # the frozen clean-baseline message; the base harness's blanket
        # ``allow_dirty_baseline=True`` accepts it regardless, which is
        # exactly the widened-authority behavior CB-05 replaces.
        evidence = EvidenceCatalog()
        receipt = evidence.add(
            kind="workspace-change-receipt",
            content={"changed_paths": ["covered.txt"]},
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        task = {
            "id": "fix",
            "objective": "Fix",
            "context": "{}",
            "details_schema": "review-fix-fix/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        kwargs = _writable_executor_kwargs(
            CodexSemanticTaskExecutor, dirty_ok=True, receipt_ref=receipt.ref
        )
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("uncovered.txt",),
            **kwargs,
        )
        snapshots = (
            _snapshot(changed_paths=["uncovered.txt"]),
            _snapshot(changed_paths=["uncovered.txt"]),
        )
        result = self._run(executor, snapshots)

        self.assertEqual(
            result.status,
            "failed",
            "a dirty path outside the receipted change set must still be "
            "refused, even though the dirty-baseline flag/grant is engaged",
        )
        error = str(result.payload.get("error", ""))
        self.assertIn("dirty-baseline grant refused", error)
        self.assertIn("uncovered.txt", error)

    def test_dirty_path_covered_by_named_receipt_is_accepted(self) -> None:
        evidence = EvidenceCatalog()
        receipt = evidence.add(
            kind="workspace-change-receipt",
            content={"changed_paths": ["feature.txt"]},
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        task = {
            "id": "fix",
            "objective": "Fix",
            "context": "{}",
            "details_schema": "review-fix-fix/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        kwargs = _writable_executor_kwargs(
            CodexSemanticTaskExecutor, dirty_ok=True, receipt_ref=receipt.ref
        )
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            **kwargs,
        )
        snapshots = (
            _snapshot(changed_paths=["feature.txt"]),
            _snapshot(changed_paths=["feature.txt"]),
        )
        result = self._run(executor, snapshots)

        self.assertEqual(result.status, "succeeded", result.payload)

    def test_dirty_baseline_without_any_grant_is_refused(self) -> None:
        evidence = EvidenceCatalog()
        task = {
            "id": "fix",
            "objective": "Fix",
            "context": "{}",
            "details_schema": "review-fix-fix/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        kwargs = _writable_executor_kwargs(
            CodexSemanticTaskExecutor, dirty_ok=False, receipt_ref=None
        )
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            **kwargs,
        )
        snapshots = (
            _snapshot(changed_paths=["feature.txt"]),
            _snapshot(changed_paths=["feature.txt"]),
        )
        result = self._run(executor, snapshots)

        self.assertEqual(result.status, "failed")
        self.assertIn(
            "clean repository baseline", str(result.payload.get("error", ""))
        )


if __name__ == "__main__":
    unittest.main()
