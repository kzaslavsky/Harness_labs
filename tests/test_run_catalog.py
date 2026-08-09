from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.run_catalog import _snapshot, build_run_catalog, build_run_detail


class RunCatalogTests(unittest.TestCase):
    def _run(self, root: Path, run_id: str, *, terminal: bool = False) -> Path:
        journal = AuditJournal(root / run_id, run_id, actor=AuditActor("a", "r"))
        descriptor = {"protocol": "harness-run-descriptor/1", "run_kind": "feature_run", "run_id": run_id, "created_at": "2026-08-09T00:00:00Z", "objective": "test", "evidence_classification": "production_lifecycle", "repository": {"path": "/repo", "base_branch": "main", "base_commit": "a" * 40}, "approved_plan": None, "parent_correlation": None}
        raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (journal.run_dir / "descriptor.json").write_bytes(raw)
        journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
        journal.checkpoint("running", journal.checkpoint_state())
        if terminal:
            journal.finalize("succeeded", result={"status": "succeeded"})
        return journal.run_dir

    def test_terminal_legacy_and_corrupt_peers_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "done", terminal=True)
            (self._run(root, "old") / "descriptor.json").unlink()
            (root / "bad").mkdir()
            snapshot = build_run_catalog(root)
        records = {record["run_id"]: record for record in snapshot["feature_runs"]}
        self.assertEqual(records["done"]["liveness"]["state"], "terminal")
        self.assertEqual(records["old"]["kind"], "legacy_feature_run")
        self.assertEqual(snapshot["diagnostics"][0]["run_id"], "bad")

    def test_live_requires_fresh_same_process_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "active")
            now = datetime(2026, 8, 9, tzinfo=timezone.utc)
            lease = {"protocol": "harness-controller-liveness/1", "run_id": "active", "controller_instance_id": "instance", "hostname": socket.gethostname(), "pid": 7, "process_start_token": "token", "heartbeat_sequence": 1, "heartbeat_at": now.isoformat(), "controller_kind": "feature_run"}
            (run / "liveness.json").write_text(json.dumps(lease))
            live = build_run_catalog(root, clock=lambda: now, process_probe=lambda pid: "token")
            stale = build_run_catalog(root, clock=lambda: now + timedelta(seconds=31), process_probe=lambda pid: "token")
        self.assertEqual(live["feature_runs"][0]["liveness"]["state"], "live")
        self.assertEqual(stale["feature_runs"][0]["liveness"]["state"], "stale")

    def test_detail_exposes_inspector_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "detail")
            journal = AuditJournal.open_existing(run, actor=AuditActor("a", "r"))
            journal.checkpoint("running", {"controller": {"criteria": ["AC-04"], "tasks": ["task"], "findings": ["finding"], "decisions": ["decision"]}})
            detail = build_run_detail(root, "detail")
        self.assertEqual(detail["criteria"], ["AC-04"])
        self.assertEqual(detail["tasks"], ["task"])
        self.assertIn("git_custody", detail)

    def test_detail_preserves_recorded_empty_evidence_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "empty")
            journal = AuditJournal.open_existing(run, actor=AuditActor("a", "r"))
            journal.checkpoint("running", {"controller": {"criteria": [], "tasks": [], "findings": [], "decisions": []}})
            detail = build_run_detail(root, "empty")
        self.assertEqual(detail["availability"]["criteria"]["state"], "available")
        self.assertEqual(detail["availability"]["findings"]["state"], "available")

    def test_invalid_descriptor_and_unmatched_correlation_are_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = self._run(root, "child")
            descriptor = json.loads((child / "descriptor.json").read_text())
            descriptor["repository"]["base_commit"] = "A" * 40
            raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            (child / "descriptor.json").write_bytes(raw)
            snapshot = build_run_catalog(root)
        self.assertEqual(snapshot["feature_runs"][0]["status"], "corrupt")

    def test_ungrouped_requires_the_full_graph_node_child_correlation(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {"run_id": "graph", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "nodes": [{"node_id": "node", "status": "running", "feature_run_id": "child", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}}]},
            {"run_id": "child", "kind": "feature_run", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "node", "parent_run_id": "other-parent"}},
        ]
        snapshot = _snapshot(Path("/runs"), now, [], records)
        self.assertEqual([record["run_id"] for record in snapshot["ungrouped_feature_runs"]], ["child"])


if __name__ == "__main__":
    unittest.main()
