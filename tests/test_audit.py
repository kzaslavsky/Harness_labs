"""Durability, tamper detection, and recovery tests for audit journals."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.audit import AuditActor, AuditError, AuditJournal


class AuditJournalTests(unittest.TestCase):
    def test_artifact_suffix_follows_declared_media_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = AuditJournal(
                Path(temporary) / "media-types",
                "media-types",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )

            markdown = journal.write_artifact(
                "report",
                b"# Report\n",
                media_type="text/markdown; charset=utf-8",
            )
            ndjson = journal.write_artifact(
                "events",
                b'{"ok":true}\n',
                media_type="application/x-ndjson",
            )
            png = journal.write_artifact(
                "screenshot",
                b"\x89PNG\r\n\x1a\n",
                media_type="image/png",
            )

            self.assertTrue(markdown.path.endswith(".md"))
            self.assertTrue(ndjson.path.endswith(".jsonl"))
            self.assertTrue(png.path.endswith(".png"))

    def test_finalized_journal_verifies_hash_chain_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            journal = AuditJournal(
                run_dir,
                "run-1",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            artifact = journal.write_artifact(
                "model-request",
                {"task": "PHI-free fixture"},
            )
            journal.append(
                "model_request",
                status="started",
                payload={"backend": "test"},
                artifacts=(artifact,),
            )
            journal.checkpoint(
                "running",
                {"active_children": ["child-1"], "active_sessions": []},
            )
            manifest = journal.finalize(
                "succeeded",
                result={"status": "succeeded"},
            )

            verification = AuditJournal.verify(run_dir)

            self.assertEqual(verification["event_count"], 3)
            self.assertEqual(manifest["head_hash"], verification["head_hash"])
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (run_dir / "events.jsonl").stat().st_mode & 0o777,
                0o600,
            )

    def test_event_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-2"
            journal = AuditJournal(
                run_dir,
                "run-2",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            journal.finalize("succeeded", result={"status": "succeeded"})
            rows = (run_dir / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            event = json.loads(rows[0])
            event["status"] = "tampered"
            rows[0] = json.dumps(event)
            (run_dir / "events.jsonl").write_text(
                "\n".join(rows) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AuditError, "hash does not match"):
                AuditJournal.verify(run_dir)

    def test_artifact_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-3"
            journal = AuditJournal(
                run_dir,
                "run-3",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            artifact = journal.write_artifact("raw-output", "original")
            journal.append(
                "model_event",
                status="succeeded",
                payload={},
                artifacts=(artifact,),
            )
            journal.checkpoint("running", {})
            artifact_path = run_dir / artifact.path
            artifact_path.write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(AuditError, "hash does not match"):
                AuditJournal.verify(run_dir)

    def test_recovery_terminalizes_an_interrupted_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-4"
            journal = AuditJournal(
                run_dir,
                "run-4",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            journal.append(
                "child_dispatched",
                status="started",
                payload={"child_attempt_id": "child-1"},
            )
            journal.checkpoint(
                "running",
                {
                    "active_children": ["child-1"],
                    "active_sessions": ["session-1"],
                },
            )

            manifest = AuditJournal.recover_interrupted(
                run_dir,
                actor=AuditActor("recovery-1", "recovery"),
                reason="test process disappeared",
            )

            self.assertEqual(manifest["status"], "interrupted")
            checkpoint = json.loads(
                (run_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "interrupted")
            self.assertEqual(checkpoint["state"]["active_children"], [])
            AuditJournal.verify(run_dir)

    def test_finalized_run_rejects_further_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-5"
            journal = AuditJournal(
                run_dir,
                "run-5",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            journal.finalize("succeeded", result={"status": "succeeded"})

            with self.assertRaisesRegex(AuditError, "already finalized"):
                journal.append("late_event", status="started", payload={})
            reopened = AuditJournal.open_existing(
                run_dir,
                actor=AuditActor("controller-2", "controller"),
            )
            with self.assertRaisesRegex(AuditError, "already finalized"):
                reopened.write_artifact("late", "not allowed")

    def test_uninventoried_artifact_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-6"
            journal = AuditJournal(
                run_dir,
                "run-6",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            journal.finalize("succeeded", result={"status": "succeeded"})
            (run_dir / "artifacts" / "untracked.txt").write_text(
                "not in the manifest",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AuditError, "inventory is incomplete"):
                AuditJournal.verify(run_dir)

    def test_open_existing_reconciles_valid_events_ahead_of_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-7"
            journal = AuditJournal(
                run_dir,
                "run-7",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            journal.append(
                "child_completed",
                status="succeeded",
                payload={"child_attempt_id": "child-1"},
            )

            with self.assertRaisesRegex(
                AuditError,
                "checkpoint does not bind the journal head",
            ):
                AuditJournal.verify(run_dir)

            reopened = AuditJournal.open_existing(
                run_dir,
                actor=AuditActor("recovery-1", "recovery"),
            )
            verification = AuditJournal.verify(run_dir)
            rows = [
                json.loads(line)
                for line in reopened.events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertEqual(
                rows[-1]["event_type"],
                "checkpoint_reconciled",
            )
            self.assertEqual(verification["event_count"], 3)
            checkpoint = json.loads(
                reopened.checkpoint_path.read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "running")
            self.assertEqual(
                checkpoint["state"]["reconciled_event_count"],
                1,
            )

    def test_append_retries_transient_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-8"
            journal = AuditJournal(
                run_dir,
                "run-8",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            original_open = __import__("os").open
            attempts = 0

            def transient_open(path, flags, mode=0o777):
                nonlocal attempts
                if Path(path) == journal.events_path and attempts < 2:
                    attempts += 1
                    raise PermissionError(1, "transient denial", str(path))
                return original_open(path, flags, mode)

            with patch("harness_labs.audit.os.open", side_effect=transient_open):
                journal.append(
                    "retry_probe",
                    status="succeeded",
                    payload={},
                )
                journal.merge_checkpoint(updates={})

            self.assertEqual(attempts, 2)
            AuditJournal.verify(run_dir)

    def test_terminal_checkpoint_without_manifest_is_not_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-9"
            journal = AuditJournal(
                run_dir,
                "run-9",
                actor=AuditActor("controller-1", "controller"),
                evidence_classification="fabricated_fixture",
            )
            journal.checkpoint(
                "failed",
                {"active_children": [], "active_sessions": []},
            )
            journal.append(
                "late_event",
                status="failed",
                payload={},
            )

            with self.assertRaisesRegex(
                AuditError,
                "terminal checkpoint is missing its manifest",
            ):
                AuditJournal.verify(run_dir)
            with self.assertRaisesRegex(
                AuditError,
                "terminal checkpoint is missing its manifest",
            ):
                AuditJournal.open_existing(
                    run_dir,
                    actor=AuditActor("recovery-1", "recovery"),
                )


if __name__ == "__main__":
    unittest.main()
