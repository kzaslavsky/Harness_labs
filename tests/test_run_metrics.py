from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.audit import AuditError
from harness_labs.run_metrics import project_run_metrics


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


if __name__ == "__main__":
    unittest.main()
