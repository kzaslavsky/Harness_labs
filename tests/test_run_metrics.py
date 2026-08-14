from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.audit import AuditError
from harness_labs.observability.run_metrics import project_run_metrics


class RunMetricsTests(unittest.TestCase):
    def test_verified_terminal_run_has_explicit_manifest_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = AuditJournal(Path(directory) / "run", "run", actor=AuditActor("a", "r"))
            journal.finalize("succeeded", result={"status": "succeeded"})
            metrics = project_run_metrics(journal.run_dir)
        self.assertTrue(metrics["terminal"])
        self.assertEqual(metrics["availability"]["manifest"]["state"], "available")

    def test_core_audit_files_must_not_be_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = AuditJournal(root / "run", "run", actor=AuditActor("a", "r"))
            journal.finalize("succeeded", result={"status": "succeeded"})
            target = root / "outside-checkpoint.json"
            target.write_bytes((journal.run_dir / "checkpoint.json").read_bytes())
            (journal.run_dir / "checkpoint.json").unlink()
            (journal.run_dir / "checkpoint.json").symlink_to(target)
            with self.assertRaises(AuditError):
                project_run_metrics(journal.run_dir)

    def test_nonterminal_verified_event_ahead_of_checkpoint_is_projected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = AuditJournal(Path(directory) / "run", "run", actor=AuditActor("a", "r"))
            journal.append("deterministic_verification_completed", status="succeeded", payload={"stage": "post_implementation", "attempt": 1, "duration_ms": 5, "exit_code": 0, "timed_out": False})
            metrics = project_run_metrics(journal.run_dir)
        self.assertEqual(metrics["checkpoint_lag"], 1)
        self.assertEqual(metrics["availability"]["journal"]["state"], "partial")
        self.assertEqual(metrics["events"][-1]["event_type"], "deterministic_verification_completed")


if __name__ == "__main__":
    unittest.main()
