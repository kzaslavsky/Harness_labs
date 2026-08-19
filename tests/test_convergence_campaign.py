"""Tests for the convergence campaign checkpoint and artifact store (CC-02).

Covers AC-CC02-1 (checkpoint atomicity, monotonic sequence, staleness
refusal), AC-CC02-2 (content-addressed artifact store seal-copy, survives
source deletion), and AC-CC02-3 (target pin/snapshot copy, scopeless
amendment blocked state, repair-node grant refusal, config surface), per the
``tests-ledger`` checklist's ``tests/test_convergence_campaign.py`` entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from harness_labs.plangraph.convergence_campaign import (
    CampaignArtifactStore,
    CampaignCheckpointStaleError,
    CampaignCheckpointSequenceError,
    CampaignCheckpointStore,
    CHECKPOINT_PROTOCOL,
    ConvergenceCampaignError,
    build_campaign_config,
    pin_target,
    reject_target_grant,
)
from harness_labs.plangraph.convergence_ledger import ConvergenceLedger


class CampaignTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"


# -- AC-CC02-1: checkpoint atomicity, monotonic sequence, staleness ---------


class CheckpointAtomicityTests(CampaignTestCase):
    def test_save_writes_no_leftover_temp_files(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc123")
        siblings = list((self.root).iterdir())
        self.assertEqual(siblings, [self.root / "checkpoint.json"])

    def test_save_uses_temp_write_fsync_rename_dir_fsync(self) -> None:
        real_replace = os.replace
        calls: list[str] = []

        def recording_replace(src, dst):
            calls.append("replace")
            self.assertTrue(Path(src).exists())
            return real_replace(src, dst)

        import harness_labs.plangraph.convergence_campaign as module

        original = module.os.replace
        module.os.replace = recording_replace
        try:
            store = CampaignCheckpointStore(self.root / "checkpoint.json")
            store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc123")
        finally:
            module.os.replace = original
        self.assertEqual(calls, ["replace"])
        self.assertTrue((self.root / "checkpoint.json").exists())

    def test_save_fsyncs_the_containing_directory(self) -> None:
        import harness_labs.plangraph.convergence_campaign as module

        calls: list[Path] = []
        original = module._fsync_directory

        def recording_dir_fsync(directory):
            calls.append(Path(directory))
            return original(directory)

        module._fsync_directory = recording_dir_fsync
        try:
            store = CampaignCheckpointStore(self.root / "checkpoint.json")
            store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc123")
        finally:
            module._fsync_directory = original
        self.assertEqual(calls, [self.root])

    def test_failed_write_leaves_prior_checkpoint_and_no_temp_file_behind(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc123")
        original_bytes = (self.root / "checkpoint.json").read_bytes()

        import harness_labs.plangraph.convergence_campaign as module

        def flaky_fsync(fd):
            raise OSError("simulated disk failure")

        original_fsync = module.os.fsync
        module.os.fsync = flaky_fsync
        try:
            with self.assertRaises(OSError):
                store.save(campaign_id="camp-1", lifecycle="measuring", base_commit="abc123")
        finally:
            module.os.fsync = original_fsync

        self.assertEqual((self.root / "checkpoint.json").read_bytes(), original_bytes)
        leftovers = [p for p in self.root.iterdir() if p.name != "checkpoint.json"]
        self.assertEqual(leftovers, [])

    def test_saved_checkpoint_carries_protocol_lifecycle_and_liveness(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        checkpoint = store.save(
            campaign_id="camp-1",
            lifecycle="opened",
            base_commit="abc123",
            owner="worker-7",
            liveness_at="2026-08-19T00:00:00+00:00",
        )
        self.assertEqual(checkpoint.lifecycle, "opened")
        self.assertEqual(checkpoint.owner, "worker-7")
        self.assertEqual(checkpoint.liveness_at, "2026-08-19T00:00:00+00:00")
        on_disk = json.loads((self.root / "checkpoint.json").read_text())
        self.assertEqual(on_disk["protocol"], CHECKPOINT_PROTOCOL)
        self.assertEqual(on_disk["lifecycle"], "opened")
        self.assertEqual(on_disk["owner"], "worker-7")
        self.assertIn("liveness_at", on_disk)

    def test_liveness_stamp_defaults_and_advances_across_saves(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        first = store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc123")
        self.assertTrue(first.liveness_at)
        second = store.save(
            campaign_id="camp-1",
            lifecycle="measuring",
            base_commit="abc123",
            liveness_at="2026-08-20T00:00:00+00:00",
        )
        self.assertNotEqual(first.liveness_at, second.liveness_at)


class CheckpointSequenceTests(CampaignTestCase):
    def test_sequence_increments_by_default_on_each_save(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        first = store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc")
        second = store.save(campaign_id="camp-1", lifecycle="measuring", base_commit="abc")
        third = store.save(campaign_id="camp-1", lifecycle="ingesting", base_commit="abc")
        self.assertEqual((first.sequence, second.sequence, third.sequence), (1, 2, 3))

    def test_save_with_explicit_backwards_sequence_rejected(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc")
        store.save(campaign_id="camp-1", lifecycle="measuring", base_commit="abc")
        with self.assertRaises(CampaignCheckpointSequenceError):
            store.save(
                campaign_id="camp-1", lifecycle="ingesting", base_commit="abc", sequence=1,
            )
        # rejected save must not have touched the file
        on_disk = json.loads((self.root / "checkpoint.json").read_text())
        self.assertEqual(on_disk["sequence"], 2)

    def test_save_with_non_advancing_equal_sequence_rejected(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc")
        with self.assertRaises(CampaignCheckpointSequenceError):
            store.save(
                campaign_id="camp-1", lifecycle="measuring", base_commit="abc", sequence=1,
            )

    def test_load_observing_sequence_regression_rejected(self) -> None:
        path = self.root / "checkpoint.json"
        store = CampaignCheckpointStore(path)
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc")
        store.save(campaign_id="camp-1", lifecycle="measuring", base_commit="abc")
        loaded = store.load()
        self.assertEqual(loaded.sequence, 2)

        # simulate an older checkpoint file swapped in underneath the store
        # (e.g. a stale backup restored out of band)
        stale_payload = dict(loaded.as_dict())
        stale_payload["sequence"] = 1
        path.write_text(json.dumps(stale_payload))

        with self.assertRaises(CampaignCheckpointSequenceError):
            store.load()

    def test_fresh_store_instance_loading_an_old_sequence_is_not_a_regression(self) -> None:
        path = self.root / "checkpoint.json"
        writer = CampaignCheckpointStore(path)
        writer.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc")
        writer.save(campaign_id="camp-1", lifecycle="measuring", base_commit="abc")

        reader = CampaignCheckpointStore(path)
        loaded = reader.load()
        self.assertEqual(loaded.sequence, 2)

    def test_repeated_load_of_unchanged_checkpoint_is_not_a_regression(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="abc")
        first = store.load()
        second = store.load()
        self.assertEqual(first.sequence, second.sequence)


class CheckpointStalenessTests(CampaignTestCase):
    def test_load_with_matching_repository_head_succeeds(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="deadbeef")
        loaded = store.load(repository_head="deadbeef")
        self.assertEqual(loaded.base_commit, "deadbeef")

    def test_load_with_mismatched_repository_head_raises_staleness_error(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="deadbeef")
        with self.assertRaises(CampaignCheckpointStaleError):
            store.load(repository_head="cafef00d")

    def test_staleness_error_is_a_distinct_typed_error(self) -> None:
        self.assertTrue(issubclass(CampaignCheckpointStaleError, ConvergenceCampaignError))
        self.assertFalse(issubclass(CampaignCheckpointStaleError, CampaignCheckpointSequenceError))
        self.assertFalse(issubclass(CampaignCheckpointSequenceError, CampaignCheckpointStaleError))

    def test_load_without_repository_head_skips_staleness_check(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        store.save(campaign_id="camp-1", lifecycle="opened", base_commit="deadbeef")
        loaded = store.load()
        self.assertEqual(loaded.base_commit, "deadbeef")

    def test_load_missing_checkpoint_raises(self) -> None:
        store = CampaignCheckpointStore(self.root / "checkpoint.json")
        with self.assertRaises(ConvergenceCampaignError):
            store.load()


# -- AC-CC02-2: content-addressed artifact store -----------------------------


class ArtifactStoreTests(CampaignTestCase):
    def _write_source(self, name: str, content: bytes) -> Path:
        source_dir = Path(self.temporary.name) / "worktree"
        source_dir.mkdir(exist_ok=True)
        path = source_dir / name
        path.write_bytes(content)
        return path

    def test_seal_copies_bytes_under_content_digest(self) -> None:
        source = self._write_source("evidence.txt", b"capture-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        record = store.seal(source)
        self.assertEqual(record.digest, __import__("hashlib").sha256(b"capture-bytes").hexdigest())
        self.assertEqual(record.size_bytes, len(b"capture-bytes"))
        self.assertTrue(store.contains(record.digest))
        self.assertEqual(store.open_bytes(record.digest), b"capture-bytes")

    def test_seal_records_size_and_media_type(self) -> None:
        source = self._write_source("shot.png", b"\x89PNG-fake-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        record = store.seal(source)
        self.assertEqual(record.size_bytes, len(b"\x89PNG-fake-bytes"))
        self.assertEqual(record.media_type, "image/png")
        self.assertEqual(record.algorithm, "sha256")

        fetched = store.metadata(record.digest)
        self.assertEqual(fetched, record)

    def test_seal_media_type_override_wins_over_guess(self) -> None:
        source = self._write_source("blob.bin", b"opaque")
        store = CampaignArtifactStore(self.root / "artifacts")
        record = store.seal(source, media_type="application/x-custom")
        self.assertEqual(record.media_type, "application/x-custom")

    def test_seal_unknown_extension_falls_back_to_octet_stream(self) -> None:
        source = self._write_source("no_extension", b"bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        record = store.seal(source)
        self.assertEqual(record.media_type, "application/octet-stream")

    def test_lookup_by_digest_survives_source_deletion(self) -> None:
        import shutil

        source_dir = Path(self.temporary.name) / "worktree"
        first = self._write_source("a.txt", b"alpha-bytes")
        second = self._write_source("b.txt", b"beta-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        record_a = store.seal(first)
        record_b = store.seal(second)

        shutil.rmtree(source_dir)
        self.assertFalse(source_dir.exists())

        self.assertTrue(store.contains(record_a.digest))
        self.assertTrue(store.contains(record_b.digest))
        self.assertEqual(store.open_bytes(record_a.digest), b"alpha-bytes")
        self.assertEqual(store.open_bytes(record_b.digest), b"beta-bytes")
        self.assertEqual(store.metadata(record_a.digest).size_bytes, len(b"alpha-bytes"))
        self.assertEqual(store.metadata(record_b.digest).media_type, "text/plain")

    def test_seal_many_copies_every_referenced_evidence_file(self) -> None:
        first = self._write_source("one.txt", b"one")
        second = self._write_source("two.txt", b"two")
        store = CampaignArtifactStore(self.root / "artifacts")
        records = store.seal_many({"evidence:one": first, "evidence:two": second})
        self.assertEqual(set(records), {"evidence:one", "evidence:two"})
        for record in records.values():
            self.assertTrue(store.contains(record.digest))

    def test_seal_is_idempotent_for_identical_content(self) -> None:
        source = self._write_source("dup.txt", b"same-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        first = store.seal(source)
        second = store.seal(source)
        self.assertEqual(first, second)

    def test_seal_writes_no_leftover_temp_files_in_store(self) -> None:
        source = self._write_source("clean.txt", b"clean-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        store.seal(source)
        objects_dir = self.root / "artifacts" / "objects"
        names = [p.name for p in objects_dir.iterdir()]
        self.assertTrue(all(not name.startswith(".") for name in names), names)
        self.assertTrue(all(not name.endswith(".tmp") for name in names), names)

    def test_seal_records_retention_alongside_size_and_media_type(self) -> None:
        source = self._write_source("shot.png", b"\x89PNG-fake-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        record = store.seal(source)
        self.assertTrue(record.retention)
        self.assertEqual(store.metadata(record.digest).retention, record.retention)

    def test_seal_retention_override_is_recorded(self) -> None:
        source = self._write_source("shot.png", b"\x89PNG-fake-bytes")
        store = CampaignArtifactStore(self.root / "artifacts")
        record = store.seal(source, retention="90d")
        self.assertEqual(record.retention, "90d")
        self.assertEqual(store.metadata(record.digest).retention, "90d")

    def test_seal_audit_result_walks_findings_evidence_refs(self) -> None:
        first = self._write_source("one.txt", b"one")
        second = self._write_source("two.txt", b"two")
        store = CampaignArtifactStore(self.root / "artifacts")
        audit_result = {
            "findings": [
                {"file": "a.py", "subject": "s1", "evidence_refs": ["ref-one"]},
                {"file": "b.py", "subject": "s2", "evidence_refs": ["ref-one", "ref-two"]},
            ],
        }
        records = store.seal_audit_result(
            audit_result, evidence_sources={"ref-one": first, "ref-two": second},
        )
        self.assertEqual(set(records), {"ref-one", "ref-two"})
        for record in records.values():
            self.assertTrue(store.contains(record.digest))

    def test_seal_audit_result_missing_evidence_source_raises(self) -> None:
        store = CampaignArtifactStore(self.root / "artifacts")
        audit_result = {
            "findings": [{"file": "a.py", "subject": "s1", "evidence_refs": ["ref-missing"]}],
        }
        with self.assertRaises(ConvergenceCampaignError):
            store.seal_audit_result(audit_result, evidence_sources={})

    def test_seal_audit_result_with_no_evidence_refs_seals_nothing(self) -> None:
        store = CampaignArtifactStore(self.root / "artifacts")
        audit_result = {"findings": [{"file": "a.py", "subject": "s1"}]}
        records = store.seal_audit_result(audit_result, evidence_sources={})
        self.assertEqual(records, {})

    def test_lookup_missing_digest_raises(self) -> None:
        store = CampaignArtifactStore(self.root / "artifacts")
        with self.assertRaises(ConvergenceCampaignError):
            store.lookup("0" * 64)

    def test_metadata_missing_digest_raises(self) -> None:
        store = CampaignArtifactStore(self.root / "artifacts")
        with self.assertRaises(ConvergenceCampaignError):
            store.metadata("0" * 64)


# -- AC-CC02-3: config surface, target pin, amendment scope, grant refusal --


class CampaignConfigTests(unittest.TestCase):
    def test_config_records_sanitizer_hook_and_thresholds(self) -> None:
        config = build_campaign_config(
            pre_journal_sanitizer="harness_labs.sanitizers:redact_secrets",
            recall_threshold=0.9,
            amendment_ratio_threshold=0.2,
        )
        self.assertEqual(
            config,
            {
                "pre_journal_sanitizer": "harness_labs.sanitizers:redact_secrets",
                "inspector_recall_threshold": 0.9,
                "amendment_ratio_threshold": 0.2,
            },
        )

    def test_config_rejects_empty_sanitizer_hook(self) -> None:
        with self.assertRaises(ConvergenceCampaignError):
            build_campaign_config(
                pre_journal_sanitizer="   ",
                recall_threshold=0.9,
                amendment_ratio_threshold=0.2,
            )

    def test_config_rejects_out_of_range_recall_threshold(self) -> None:
        with self.assertRaises(ConvergenceCampaignError):
            build_campaign_config(
                pre_journal_sanitizer="mod:fn",
                recall_threshold=1.5,
                amendment_ratio_threshold=0.2,
            )

    def test_config_rejects_out_of_range_amendment_ratio_threshold(self) -> None:
        with self.assertRaises(ConvergenceCampaignError):
            build_campaign_config(
                pre_journal_sanitizer="mod:fn",
                recall_threshold=0.9,
                amendment_ratio_threshold=-0.1,
            )

    def test_config_rejects_non_numeric_threshold(self) -> None:
        with self.assertRaises(ConvergenceCampaignError):
            build_campaign_config(
                pre_journal_sanitizer="mod:fn",
                recall_threshold="high",
                amendment_ratio_threshold=0.2,
            )


class TargetPinTests(CampaignTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_dir = Path(self.temporary.name) / "product"
        self.source_dir.mkdir()
        self.target_source = self.source_dir / "spec.md"
        self.target_source.write_text("target contents\n", encoding="utf-8")
        self.ledger = ConvergenceLedger(self.root / "ledger.jsonl")

    def _pin(self, **overrides) -> dict:
        kwargs = dict(
            campaign_root=self.root,
            domain="flow-editor",
            source_path=self.target_source,
            target_kind="design-spec",
            snapshot_relative_path="target/spec.md",
            base_commit="abc123",
            pre_journal_sanitizer="mod:fn",
            recall_threshold=0.9,
            amendment_ratio_threshold=0.2,
        )
        kwargs.update(overrides)
        return pin_target(self.ledger, **kwargs)

    def test_pin_copies_target_file_into_campaign_root(self) -> None:
        self._pin()
        snapshot = self.root / "target" / "spec.md"
        self.assertTrue(snapshot.exists())
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "target contents\n")

    def test_pin_records_campaign_opened_with_kind_digest_snapshot_path(self) -> None:
        import hashlib

        record = self._pin()
        self.assertEqual(record["type"], "campaign_opened")
        expected_digest = hashlib.sha256(b"target contents\n").hexdigest()
        self.assertEqual(record["target"]["kind"], "design-spec")
        self.assertEqual(record["target"]["digest"], expected_digest)
        self.assertEqual(record["target"]["snapshot_path"], "target/spec.md")
        self.assertEqual(record["target"]["path"], str(self.target_source))

    def test_pin_records_config_surface_on_the_ledger(self) -> None:
        self._pin()
        state_campaign = self.ledger.records()[0]
        self.assertEqual(state_campaign["config"]["pre_journal_sanitizer"], "mod:fn")
        self.assertEqual(state_campaign["config"]["inspector_recall_threshold"], 0.9)
        self.assertEqual(state_campaign["config"]["amendment_ratio_threshold"], 0.2)

    def test_pin_deleting_source_leaves_campaign_root_copy_intact(self) -> None:
        self._pin()
        self.target_source.unlink()
        snapshot = self.root / "target" / "spec.md"
        self.assertTrue(snapshot.exists())
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "target contents\n")

    def test_pin_missing_source_path_rejected(self) -> None:
        missing = self.source_dir / "missing.md"
        with self.assertRaises(ConvergenceCampaignError):
            self._pin(source_path=missing)

    def test_pin_absolute_snapshot_relative_path_rejected(self) -> None:
        outside_marker = self.root.parent / "escaped.md"
        with self.assertRaises(ConvergenceCampaignError):
            self._pin(snapshot_relative_path=str(outside_marker))
        self.assertFalse(outside_marker.exists())

    def test_pin_snapshot_relative_path_with_dotdot_rejected(self) -> None:
        outside_marker = Path(self.temporary.name) / "outside.md"
        with self.assertRaises(ConvergenceCampaignError):
            self._pin(snapshot_relative_path="../outside.md")
        self.assertFalse(outside_marker.exists())

    def test_pin_snapshot_write_is_atomic_no_leftover_temp_files(self) -> None:
        self._pin()
        target_dir = self.root / "target"
        names = [p.name for p in target_dir.iterdir()]
        self.assertEqual(names, ["spec.md"])


class TargetAmendmentBlockedStateTests(CampaignTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_dir = Path(self.temporary.name) / "product"
        self.source_dir.mkdir()
        self.target_source = self.source_dir / "spec.md"
        self.target_source.write_text("v1\n", encoding="utf-8")
        self.ledger = ConvergenceLedger(self.root / "ledger.jsonl")
        pin_target(
            self.ledger,
            campaign_root=self.root,
            domain="flow-editor",
            source_path=self.target_source,
            target_kind="design-spec",
            snapshot_relative_path="target/spec.md",
            base_commit="abc123",
            pre_journal_sanitizer="mod:fn",
            recall_threshold=0.9,
            amendment_ratio_threshold=0.2,
        )

    def test_amendment_without_scope_sets_blocked_state(self) -> None:
        self.assertFalse(self.ledger.is_blocked())
        self.ledger.record_target_amendment(digest="deadbeef" * 8, invalidation_scope=None)
        self.assertTrue(self.ledger.is_blocked())

    def test_amendment_with_scope_does_not_block(self) -> None:
        self.ledger.record_target_amendment(
            digest="deadbeef" * 8,
            invalidation_scope=[["spec.md", "section-1"]],
        )
        self.assertFalse(self.ledger.is_blocked())

    def test_later_scoped_amendment_clears_a_prior_scopeless_block(self) -> None:
        self.ledger.record_target_amendment(digest="a" * 64, invalidation_scope=None)
        self.assertTrue(self.ledger.is_blocked())
        self.ledger.record_target_amendment(
            digest="b" * 64, invalidation_scope=[["spec.md", "section-1"]],
        )
        self.assertFalse(self.ledger.is_blocked())


class RepairGrantRejectionTests(unittest.TestCase):
    def test_grant_covering_the_target_path_exactly_is_rejected(self) -> None:
        target = {"kind": "design-spec", "digest": "abc", "snapshot_path": "target/spec.md",
                   "path": "docs/design/spec.md"}
        with self.assertRaises(ConvergenceCampaignError):
            reject_target_grant(target, ["docs/design/spec.md"])

    def test_grant_on_a_directory_containing_the_target_path_is_rejected(self) -> None:
        target = {"kind": "design-spec", "digest": "abc", "snapshot_path": "target/spec.md",
                   "path": "docs/design/spec.md"}
        with self.assertRaises(ConvergenceCampaignError):
            reject_target_grant(target, ["docs/design"])

    def test_grant_on_an_unrelated_path_is_allowed(self) -> None:
        target = {"kind": "design-spec", "digest": "abc", "snapshot_path": "target/spec.md",
                   "path": "docs/design/spec.md"}
        reject_target_grant(target, ["harness_labs/plangraph/some_repair.py"])

    def test_grant_set_including_one_unrelated_and_one_target_path_is_rejected(self) -> None:
        target = {"kind": "design-spec", "digest": "abc", "snapshot_path": "target/spec.md",
                   "path": "docs/design/spec.md"}
        with self.assertRaises(ConvergenceCampaignError):
            reject_target_grant(
                target, ["harness_labs/plangraph/some_repair.py", "docs/design/spec.md"],
            )

    def test_target_without_path_field_falls_back_to_snapshot_path(self) -> None:
        target = {"kind": "design-spec", "digest": "abc", "snapshot_path": "target/spec.md"}
        # grant does not cover the fallback snapshot_path: not rejected
        reject_target_grant(target, ["docs/design/spec.md"])

    def test_target_without_path_field_grant_covering_snapshot_path_is_rejected(self) -> None:
        target = {"kind": "design-spec", "digest": "abc", "snapshot_path": "target/spec.md"}
        with self.assertRaises(ConvergenceCampaignError):
            reject_target_grant(target, ["target/spec.md"])

    def test_target_with_neither_path_nor_snapshot_path_raises(self) -> None:
        target = {"kind": "design-spec", "digest": "abc"}
        with self.assertRaises(ConvergenceCampaignError):
            reject_target_grant(target, ["docs/design/spec.md"])


if __name__ == "__main__":
    unittest.main()
