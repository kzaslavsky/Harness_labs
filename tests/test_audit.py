"""Durability, tamper detection, and recovery tests for audit journals."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs.audit import AuditActor, AuditError, AuditJournal


class AuditJournalTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
