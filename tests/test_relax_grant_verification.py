"""Finding test for CB3-02: one verifier for the dirty-baseline grant.

Reproduces the CB2-03 attempt-1 specimen (item 20 of
``docs/development/contract-burden-reduction.md``): the journal records
``dirty_baseline_adoption_grant_supplied | granted``, immediately followed by
the executor preflight refusing the writable worker on the same
dirty-baseline precondition the grant exists to waive.

At the frozen base commit, ``feature_run.py``'s review-fix issuer
(``_dirty_baseline_receipt_ref`` / ``_attach_dirty_baseline_grant``) selects
and journals a covering receipt by *changed-path coverage alone*. The
enforcer -- ``CodexSemanticTaskExecutor._resolve_dirty_baseline_grant``,
already content-aware at the base commit -- then refuses the very same
grant because the workspace's on-disk content no longer matches what the
receipt recorded (a genuine drift between issue time and preflight time).
This test drives that drift directly: the builder task's receipt records the
content it just wrote, then the same dispatch further mutates the file
before the review-fix "fix" stage runs, so the issuer's snapshot at
grant-issue time is already stale by the time the (real, base-commit)
enforcer method re-checks it.

The "fix" stage here constructs a real ``CodexSemanticTaskExecutor`` (the
enforcer that already exists at the base commit) and calls its own
``_resolve_dirty_baseline_grant`` directly -- the actual production
preflight check, not a re-implementation -- so the divergence this test
proves is the base harness's own behavior.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness_labs.attempts import TaskAttempt, TaskResult
from harness_labs.audit import AuditJournal
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_kernel import RunContract
from harness_labs.controller_live import CodexSemanticTaskExecutor, LiveExecutionError
from harness_labs.controller_results import semantic_payload
from harness_labs.controller_scheduler import RoleProfile
from harness_labs.coordinator_schema import CoordinatorDispatchSchema, CoordinatorSegment
from harness_labs.feature_run import ReviewFixPolicy, run_feature_worktree
from harness_labs.git_transaction import workspace_snapshot
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


def _events(root: Path) -> list:
    return [
        json.loads(line)
        for line in (root / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


class _DriftingBuildExecutor:
    """A writable build task whose receipt goes stale within the same dispatch.

    Writes ``feature.txt``, mints a workspace-change receipt recording the
    real on-disk content at that moment (exactly as the live executors do),
    then mutates the file again -- modeling a workspace that drifts between
    when a grant is issued and when it is enforced.
    """

    def __init__(self, worktree: Path, evidence: EvidenceCatalog) -> None:
        self.worktree = worktree
        self.evidence = evidence

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        path = self.worktree / "feature.txt"
        path.write_text("built\n", encoding="utf-8")
        snapshot = workspace_snapshot(self.worktree)
        artifact = self.evidence.add(
            kind="implementation-summary",
            content="Built the candidate with a declared change.\n",
            media_type="text/markdown",
            producer_task_id="build",
        )
        receipt = self.evidence.add(
            kind="workspace-change-receipt",
            content={
                "protocol": "workspace-change-receipt/2",
                "changed_paths": ["feature.txt"],
                "files": {"feature.txt": snapshot["files"].get("feature.txt")},
            },
            media_type="application/json",
            producer_task_id="build",
        )
        # The drift: on-disk content moves on after the receipt already
        # attested the pre-drift content.
        path.write_text("drifted\n", encoding="utf-8")
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Built.",
                details_schema="build/1",
                details={"paths": ["feature.txt"]},
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


class _RealEnforcerFixExecutor:
    """The review-fix "fix" stage, preflighted by the real base-commit enforcer.

    ``dirty_baseline_grant`` is populated externally by
    ``feature_run._attach_dirty_baseline_grant`` before ``execute`` runs,
    exactly as it is for the live semantic executors. This calls the real
    ``CodexSemanticTaskExecutor._resolve_dirty_baseline_grant`` -- the
    production enforcer -- instead of re-implementing its check, so a
    refusal here is the base harness's own behavior.
    """

    dirty_baseline_grant = None

    def __init__(self, worktree: Path, evidence: EvidenceCatalog) -> None:
        self.worktree = worktree
        self.evidence = evidence

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        executor = CodexSemanticTaskExecutor(
            task={
                "id": "fix",
                "objective": "Fix",
                "context": "{}",
                "details_schema": "review-fix-fix/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            },
            repository=self.worktree,
            evidence=self.evidence,
            role_instructions="Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            dirty_baseline_grant=self.dirty_baseline_grant,
        )
        initial_workspace = workspace_snapshot(self.worktree)
        try:
            executor._resolve_dirty_baseline_grant(initial_workspace)
        except LiveExecutionError as exc:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=semantic_payload(
                summary="Fix complete.",
                details_schema="review-fix-fix/1",
                details={"addressed_finding_keys": ["feature.txt:review-fix"]},
            ),
        )


class _ReviewOnceFixFactory:
    """Review reports one finding on cycle 1 only; fix uses the real enforcer."""

    def __init__(self) -> None:
        self.worktree: Path | None = None
        self.evidence: EvidenceCatalog | None = None
        self.review_calls = 0
        self.fix_executor: _RealEnforcerFixExecutor | None = None

    def __call__(self, stage: str, attempt: TaskAttempt):
        factory = self
        if stage == "review":

            class _Review:
                def execute(self, current_attempt: TaskAttempt) -> TaskResult:
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

            return _Review()
        # stage == "fix": reuse one instance so feature_run's grant-attach
        # wrapper mutates the same object ``execute`` reads from.
        if factory.fix_executor is None:
            factory.fix_executor = _RealEnforcerFixExecutor(
                factory.worktree, factory.evidence
            )
        return factory.fix_executor


class GrantIssuedThenRefusedTest(unittest.TestCase):
    """AC-CB302-4: a journaled 'granted' grant must never be refused at preflight."""

    def test_review_fix_grant_supplied_and_granted_yet_preflight_refuses(
        self,
    ) -> None:
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
                "grant-verification-test/1",
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
                    run_id="grant-verification-run",
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

            factory = _ReviewOnceFixFactory()

            def profile_builder(candidate, evidence):
                factory.worktree = candidate
                factory.evidence = evidence
                return (
                    RoleProfile(
                        "builder",
                        "builder",
                        frozenset({"repo.write"}),
                        lambda task: _DriftingBuildExecutor(candidate, evidence),
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
                allowed_paths=("feature.txt",),
                commit_message="Build and address review finding despite drift",
                merge=False,
                review_fix_executor_factory=factory,
                review_fix_policy=ReviewFixPolicy(),
            )

            events = _events(root)
            granted = [
                event
                for event in events
                if event.get("event_type") == "dirty_baseline_adoption_grant_supplied"
                and event.get("status") == "granted"
            ]

            if granted:
                # A grant was journaled as granted. Per AC-CB302-1/2, that
                # decision must be the same one the executor preflight
                # enforces for the identical workspace state -- the granted
                # dispatch must therefore succeed, never be refused. The
                # base harness journals the grant from path coverage alone
                # and then refuses it at content-aware preflight; this is
                # exactly the CB2-03 attempt-1
                # 'dirty_baseline_adoption_grant_supplied | granted' ->
                # preflight-refusal divergence.
                self.assertEqual(
                    result.review_fix.status if result.review_fix else result.status,
                    "succeeded",
                    "a dirty-baseline grant journaled as 'granted' must not "
                    "then be refused by the same-state executor preflight: "
                    + (
                        result.review_fix.reason
                        if result.review_fix
                        else str(result.dispatch.result.payload)
                    ),
                )
            else:
                # No grant was journaled as granted: the issuer correctly
                # declined a receipt whose content no longer matches the
                # drifted workspace. The dispatch must still be refused --
                # unification never widens who may adopt a dirty baseline.
                self.assertEqual(
                    result.review_fix.status if result.review_fix else result.status,
                    "failed",
                )
                reason = (
                    result.review_fix.reason
                    if result.review_fix
                    else str(result.dispatch.result.payload)
                )
                self.assertIn("clean repository baseline", reason)

                # The decline itself must still be diagnosable from the
                # journal: the issuer names the drifted path as
                # content-mismatched instead of failing silently.
                refused = [
                    event
                    for event in events
                    if event.get("event_type")
                    == "dirty_baseline_adoption_grant_supplied"
                    and event.get("status") == "refused"
                ]
                self.assertTrue(
                    refused, "issuer decline was not journaled at all"
                )
                self.assertIn(
                    "feature.txt", refused[0]["payload"]["mismatched_paths"]
                )

            AuditJournal.verify(root / "run")


if __name__ == "__main__":
    unittest.main()
