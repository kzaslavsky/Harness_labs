"""Finding test for CB3-04: journaled baseline restoration after failed
writable attempts.

Reproduces item 19 of ``docs/development/contract-burden-reduction.md`` (the
CB2-05 shape): a writable attempt writes outside its declared
``writable_paths``, fails preflight with "writable worker changed paths
outside its grant" *before* any workspace-change receipt is ever written,
and leaves its residue sitting on disk. At the frozen base commit no
in-system actor is authorized to clean that residue: the workspace stays
dirty, and the very next writable dispatch in the same workspace -- through
the exact same production entry point, ``CodexSemanticTaskExecutor.execute``
-- fails its own preflight with the generic "writable worker requires a
clean repository baseline" message, with no restoration bookkeeping anywhere
in the audit journal.

Every entry point this test drives (``CodexSemanticTaskExecutor.execute``,
``harness_labs.core.git_transaction.workspace_snapshot``, ``AuditJournal``) already
exists at the frozen base commit; the failure this test exercises is a
behavioral gap (no restoration occurs, the tree stays dirty, the follow-up
dispatch is stranded), not an import-time one.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.core.attempts import TaskAttempt
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_live import CodexSemanticTaskExecutor
from harness_labs.core.git_transaction import workspace_snapshot

_RESTORATION_EVENT_TYPE = "attempt_start_baseline_restoration"


def _git(repository: Path, *args: str) -> str:
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


def _raw_result(**overrides: object) -> dict:
    raw = {
        "summary": "Built.",
        "deliverable_markdown": "# Build\nWrote the feature file.",
        "details_json": json.dumps({"paths": ["feature.txt"]}),
        "claims": [],
        "findings": [],
        "recommendations": [],
        "unresolved_questions": [],
        "satisfied_criteria": [],
    }
    raw.update(overrides)
    return raw


def _read_events(run_dir: Path) -> list[dict]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class AttemptStartBaselineRestorationTest(unittest.TestCase):
    """AC-CB304-1/2/4: unreceipted out-of-grant residue no longer deadlocks
    the node behind a permanent clean-baseline refusal."""

    def test_unreceipted_out_of_grant_residue_is_restored_and_unblocks_followup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Harness Tests")
            _git(repository, "config", "user.email", "harness@example.invalid")
            (repository / "README.md").write_text("base\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "--no-gpg-sign", "-m", "Base")
            base_commit = _git(repository, "rev-parse", "HEAD")

            run_dir = Path(temporary) / "audit"
            journal = AuditJournal(
                run_dir,
                "cb304-restoration-run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="fabricated_fixture",
            )
            evidence = EvidenceCatalog()

            task = {
                "id": "build",
                "objective": "Build the feature file",
                "context": "{}",
                "details_schema": "implementation/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Build only feature.txt.",
                sandbox="workspace-write",
                writable_paths=("feature.txt",),
                audit=journal,
            )

            real_run = subprocess.run
            raw = _raw_result()

            def run(argv, **kwargs):
                if argv[0] != "codex":
                    # Let CB3-04 restoration's own git subprocess calls run
                    # for real -- only the codex CLI invocation itself is
                    # faked, exactly like a genuine attempt that dispatched,
                    # wrote outside its grant, and then failed preflight.
                    return real_run(argv, **kwargs)
                (repository / "escape.txt").write_text("leaked\n", encoding="utf-8")
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch("harness_labs.core.controller_live.subprocess.run", side_effect=run),
            ):
                result = executor.execute(
                    TaskAttempt(
                        "build/attempt-1",
                        "task:build",
                        "context:build",
                        "profile:builder",
                    )
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("outside its grant", result.payload["error"])

            # The live CB2-05 dead end this program closes: the workspace
            # must not be left dirty behind an attempt no in-system actor
            # could clean.
            post_attempt_snapshot = workspace_snapshot(repository)
            self.assertEqual(
                post_attempt_snapshot["changed_paths"],
                [],
                "the failed attempt's unreceipted residue must be restored, "
                "not left dirty behind a permanent clean-baseline refusal",
            )
            self.assertFalse((repository / "escape.txt").exists())
            self.assertTrue((repository / "README.md").is_file())

            # The typed restoration event: baseline commit, per-path actions,
            # and the trigger conditions' evaluations.
            events = _read_events(run_dir)
            restoration_events = [
                event
                for event in events
                if event.get("event_type") == _RESTORATION_EVENT_TYPE
            ]
            self.assertEqual(len(restoration_events), 1, events)
            event = restoration_events[0]
            self.assertEqual(event["status"], "restored")
            self.assertEqual(event["payload"]["baseline_commit"], base_commit)
            self.assertEqual(event["payload"]["actions"], {"escape.txt": "removed"})
            self.assertTrue(event["payload"]["conditions"]["attempt_started_clean"])
            self.assertTrue(event["payload"]["conditions"]["no_covering_receipt"])
            self.assertIsNone(event["payload"]["receipt_ref"])

            # The follow-up writable dispatch this residue used to strand:
            # it must now run through its own preflight and succeed, instead
            # of failing on "writable worker requires a clean repository
            # baseline".
            followup_task = {**task, "id": "followup"}
            followup = CodexSemanticTaskExecutor(
                followup_task,
                repository,
                evidence,
                "Follow up.",
                sandbox="workspace-write",
                writable_paths=("feature.txt",),
                audit=journal,
            )

            def run_followup(argv, **kwargs):
                if argv[0] != "codex":
                    return real_run(argv, **kwargs)
                (repository / "feature.txt").write_text("built\n", encoding="utf-8")
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(_raw_result()), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch(
                    "harness_labs.core.controller_live.subprocess.run",
                    side_effect=run_followup,
                ),
            ):
                followup_result = followup.execute(
                    TaskAttempt(
                        "followup/attempt-1",
                        "task:followup",
                        "context:followup",
                        "profile:builder",
                    )
                )
            self.assertEqual(
                followup_result.status,
                "succeeded",
                followup_result.payload,
            )


class AttemptStartedDirtyIsNeverRestoredTest(unittest.TestCase):
    """AC-CB304-1/3: restoration never fires for a workspace the attempt did
    not itself dirty -- a legitimately adopted dirty baseline (CB3-03) that
    later fails must be left exactly as it was, since it is adoption
    material, not this attempt's own residue."""

    def test_failed_attempt_over_a_pre_existing_receipted_dirty_baseline_is_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Harness Tests")
            _git(repository, "config", "user.email", "harness@example.invalid")
            (repository / "README.md").write_text("base\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "--no-gpg-sign", "-m", "Base")

            # A prior successful attempt's genuine, receipted dirty residue.
            (repository / "feature.txt").write_text("prior work\n", encoding="utf-8")

            run_dir = Path(temporary) / "audit"
            journal = AuditJournal(
                run_dir,
                "cb304-no-restore-run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="fabricated_fixture",
            )
            evidence = EvidenceCatalog()
            snapshot = workspace_snapshot(repository)
            receipt = evidence.add(
                kind="workspace-change-receipt",
                content={
                    "protocol": "workspace-change-receipt/2",
                    "changed_paths": snapshot["changed_paths"],
                    "files": snapshot["files"],
                },
                media_type="application/json",
                producer_task_id="prior-attempt",
            )

            task = {
                "id": "fix",
                "objective": "Fix on top of the receipted baseline",
                "context": "{}",
                "details_schema": "implementation/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Fix only feature.txt.",
                sandbox="workspace-write",
                writable_paths=("feature.txt",),
                dirty_baseline_grant={"receipt_ref": receipt.ref},
                audit=journal,
            )

            real_run = subprocess.run
            raw = _raw_result()

            def run(argv, **kwargs):
                if argv[0] != "codex":
                    return real_run(argv, **kwargs)
                # Writes outside the grant -- fails, exactly as the CB2-05
                # specimen does, but this time over an already-dirty,
                # receipted baseline the attempt itself did not create.
                (repository / "escape.txt").write_text("leaked\n", encoding="utf-8")
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch("harness_labs.core.controller_live.subprocess.run", side_effect=run),
            ):
                result = executor.execute(
                    TaskAttempt(
                        "fix/attempt-1",
                        "task:fix",
                        "context:fix",
                        "profile:fixer",
                    )
                )

            self.assertEqual(result.status, "failed")

            # Nothing is reverted: the pre-existing receipted work survives,
            # and the attempt's own out-of-grant write is left in place too,
            # since restoration must never touch a workspace it did not
            # itself dirty from clean.
            self.assertEqual(
                (repository / "feature.txt").read_text(encoding="utf-8"),
                "prior work\n",
            )
            self.assertTrue((repository / "escape.txt").is_file())

            events = _read_events(run_dir)
            restoration_events = [
                event
                for event in events
                if event.get("event_type") == _RESTORATION_EVENT_TYPE
            ]
            self.assertEqual(len(restoration_events), 1, events)
            event = restoration_events[0]
            self.assertEqual(event["status"], "declined")
            self.assertFalse(event["payload"]["conditions"]["attempt_started_clean"])
            self.assertNotIn("actions", event["payload"])


class NoNewerAttemptDeclinesRestorationTest(unittest.TestCase):
    """AC-CB304-1: restoration must decline once a newer writable attempt
    has started against the same repository -- otherwise it would delete
    that sibling attempt's own in-flight files."""

    def test_restoration_declines_when_a_newer_writable_attempt_has_started(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Harness Tests")
            _git(repository, "config", "user.email", "harness@example.invalid")
            (repository / "README.md").write_text("base\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "--no-gpg-sign", "-m", "Base")

            run_dir = Path(temporary) / "audit"
            journal = AuditJournal(
                run_dir,
                "cb304-newer-attempt-run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="fabricated_fixture",
            )
            evidence = EvidenceCatalog()

            task_a = {
                "id": "build-a",
                "objective": "Build A",
                "context": "{}",
                "details_schema": "implementation/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            executor_a = CodexSemanticTaskExecutor(
                task_a,
                repository,
                evidence,
                "Build only feature-a.txt.",
                sandbox="workspace-write",
                writable_paths=("feature-a.txt",),
                audit=journal,
            )
            task_b = {
                "id": "build-b",
                "objective": "Build B",
                "context": "{}",
                "details_schema": "implementation/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            executor_b = CodexSemanticTaskExecutor(
                task_b,
                repository,
                evidence,
                "Build only feature-b.txt.",
                sandbox="workspace-write",
                writable_paths=("feature-b.txt",),
                audit=journal,
            )

            real_run = subprocess.run
            raw_a = _raw_result()
            raw_b = _raw_result()

            def run_b(argv, **kwargs):
                if argv[0] != "codex":
                    return real_run(argv, **kwargs)
                (repository / "feature-b.txt").write_text(
                    "b built\n", encoding="utf-8"
                )
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw_b), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            def run_a(argv, **kwargs):
                if argv[0] != "codex":
                    return real_run(argv, **kwargs)
                # A newer writable attempt (B) starts and completes against
                # the same repository while A's own worker is still
                # running -- B's residue must survive whatever A's own
                # restoration later decides.
                with patch(
                    "harness_labs.core.controller_live.subprocess.run",
                    side_effect=run_b,
                ):
                    b_result = executor_b.execute(
                        TaskAttempt(
                            "build-b/attempt-1",
                            "task:build-b",
                            "context:build-b",
                            "profile:builder",
                        )
                    )
                self.assertEqual(b_result.status, "succeeded", b_result.payload)
                # A writes outside its own grant, exactly like the CB2-05
                # specimen, and will fail preflight below.
                (repository / "escape-a.txt").write_text(
                    "leaked\n", encoding="utf-8"
                )
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw_a), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch(
                    "harness_labs.core.controller_live.subprocess.run",
                    side_effect=run_a,
                ),
            ):
                result_a = executor_a.execute(
                    TaskAttempt(
                        "build-a/attempt-1",
                        "task:build-a",
                        "context:build-a",
                        "profile:builder",
                    )
                )

            self.assertEqual(result_a.status, "failed")
            self.assertIn("outside its grant", result_a.payload["error"])

            # B's in-flight residue must never be touched by A's
            # restoration, since B started after A did.
            self.assertTrue((repository / "feature-b.txt").is_file())
            self.assertEqual(
                (repository / "feature-b.txt").read_text(encoding="utf-8"),
                "b built\n",
            )

            events = _read_events(run_dir)
            a_restoration_events = [
                event
                for event in events
                if event.get("event_type") == _RESTORATION_EVENT_TYPE
                and event.get("attempt_id") == "build-a/attempt-1"
            ]
            self.assertEqual(len(a_restoration_events), 1, events)
            event = a_restoration_events[0]
            self.assertEqual(event["status"], "declined")
            self.assertFalse(
                event["payload"]["conditions"]["no_newer_attempt_started"]
            )


class HeadOrBranchMovedDuringAttemptDeclinesRestorationTest(unittest.TestCase):
    """AC-CB304-2: if the worker moved HEAD or the branch before failing,
    restoring against the pre-attempt baseline commit would leave a
    partial revert -- restoration must decline instead."""

    def test_restoration_declines_when_worker_moves_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Harness Tests")
            _git(repository, "config", "user.email", "harness@example.invalid")
            (repository / "README.md").write_text("base\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "--no-gpg-sign", "-m", "Base")

            run_dir = Path(temporary) / "audit"
            journal = AuditJournal(
                run_dir,
                "cb304-head-moved-run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="fabricated_fixture",
            )
            evidence = EvidenceCatalog()

            task = {
                "id": "build",
                "objective": "Build the feature file",
                "context": "{}",
                "details_schema": "implementation/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Build only feature.txt.",
                sandbox="workspace-write",
                writable_paths=("feature.txt",),
                audit=journal,
            )

            real_run = subprocess.run
            raw = _raw_result()

            def run(argv, **kwargs):
                if argv[0] != "codex":
                    return real_run(argv, **kwargs)
                # The worker commits a tracked file (moving HEAD) and also
                # leaves an untracked out-of-grant file dirty -- the
                # pre-attempt baseline commit is now stale, and reverting
                # the still-dirty path against it would corrupt history
                # instead of restoring a clean tree.
                (repository / "committed.txt").write_text(
                    "mid-attempt\n", encoding="utf-8"
                )
                _git(repository, "add", "committed.txt")
                _git(
                    repository,
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    "worker committed mid-attempt",
                )
                (repository / "escape.txt").write_text(
                    "leaked\n", encoding="utf-8"
                )
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch(
                    "harness_labs.core.controller_live.subprocess.run", side_effect=run
                ),
            ):
                result = executor.execute(
                    TaskAttempt(
                        "build/attempt-1",
                        "task:build",
                        "context:build",
                        "profile:builder",
                    )
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("changed repository HEAD", result.payload["error"])

            # The worker's commit must survive: restoration must decline
            # rather than revert against the now-stale pre-attempt baseline.
            self.assertEqual(
                _git(repository, "log", "-1", "--pretty=%s"),
                "worker committed mid-attempt",
            )
            self.assertTrue((repository / "committed.txt").is_file())
            self.assertTrue((repository / "escape.txt").is_file())

            events = _read_events(run_dir)
            restoration_events = [
                event
                for event in events
                if event.get("event_type") == _RESTORATION_EVENT_TYPE
            ]
            self.assertEqual(len(restoration_events), 1, events)
            event = restoration_events[0]
            self.assertEqual(event["status"], "declined")
            self.assertFalse(event["payload"]["conditions"]["head_unchanged"])


class RestorationSkippedBeforePostWorkerSnapshotTest(unittest.TestCase):
    """AC-CB304-1/2: restoration must still be reachable for an attempt
    that dirtied the workspace and then failed before the post-worker
    workspace snapshot was ever taken (a nonzero worker exit, in this
    case) -- the exact live CB2-05 shape."""

    def test_restoration_still_fires_when_worker_exits_nonzero_after_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Harness Tests")
            _git(repository, "config", "user.email", "harness@example.invalid")
            (repository / "README.md").write_text("base\n", encoding="utf-8")
            _git(repository, "add", "README.md")
            _git(repository, "commit", "--no-gpg-sign", "-m", "Base")
            base_commit = _git(repository, "rev-parse", "HEAD")

            run_dir = Path(temporary) / "audit"
            journal = AuditJournal(
                run_dir,
                "cb304-nonzero-exit-run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="fabricated_fixture",
            )
            evidence = EvidenceCatalog()

            task = {
                "id": "build",
                "objective": "Build the feature file",
                "context": "{}",
                "details_schema": "implementation/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Build only feature.txt.",
                sandbox="workspace-write",
                writable_paths=("feature.txt",),
                audit=journal,
            )

            real_run = subprocess.run

            def run(argv, **kwargs):
                if argv[0] != "codex":
                    return real_run(argv, **kwargs)
                # The worker wrote residue and then the codex process
                # itself exited nonzero -- ``_execute`` raises before ever
                # reaching the post-worker workspace snapshot at all.
                (repository / "feature.txt").write_text(
                    "half-written\n", encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="codex crashed"
                )

            with (
                patch(
                    "harness_labs.core.controller_live.shutil.which",
                    return_value="codex",
                ),
                patch(
                    "harness_labs.core.controller_live.subprocess.run", side_effect=run
                ),
            ):
                result = executor.execute(
                    TaskAttempt(
                        "build/attempt-1",
                        "task:build",
                        "context:build",
                        "profile:builder",
                    )
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("exited with status", result.payload["error"])

            post_attempt_snapshot = workspace_snapshot(repository)
            self.assertEqual(
                post_attempt_snapshot["changed_paths"],
                [],
                "residue left by a worker that failed before the "
                "post-worker snapshot was ever taken must still be "
                "restored",
            )

            events = _read_events(run_dir)
            restoration_events = [
                event
                for event in events
                if event.get("event_type") == _RESTORATION_EVENT_TYPE
            ]
            self.assertEqual(len(restoration_events), 1, events)
            event = restoration_events[0]
            self.assertEqual(event["status"], "restored")
            self.assertEqual(event["payload"]["baseline_commit"], base_commit)
            self.assertEqual(
                event["payload"]["actions"], {"feature.txt": "removed"}
            )


if __name__ == "__main__":
    unittest.main()
