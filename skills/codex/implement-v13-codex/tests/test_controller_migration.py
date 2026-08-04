from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import hashlib
import importlib.util
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE_PARENT = PACKAGE.parent
sys.path.insert(0, str(PACKAGE / "scripts"))

from controller_package import (  # noqa: E402
    copy_controller_package,
    validate_committed_migration,
    verify_controller_package,
)
from state_io import StateError, atomic_write_json, sha256_file  # noqa: E402


class ControllerMigrationTests(unittest.TestCase):
    @staticmethod
    def _load_module(path: Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _fixture(self) -> dict[str, object]:
        temporary = tempfile.TemporaryDirectory()
        base = Path(temporary.name).resolve()
        artifact_dir = base / "handoff/serial-runs/qr-test/fr-test"
        worktree = base / ".claude/worktrees/impl-codex-fr-test"
        worktree.mkdir(parents=True)
        package_root = artifact_dir / "controller-package"
        manifest = copy_controller_package(SOURCE_PARENT, package_root)
        paths = {
            "queue": base / "queue.json",
            "dispatch": artifact_dir / "dispatch.v1.json",
            "checkpoint": worktree / "docs/development/current_implementation_checkpoint.json",
            "transaction": artifact_dir / "feature-transaction.v1.json",
            "ledger": artifact_dir / "review-closure-ledger.v1.json",
            "journal": artifact_dir / "controller-migration.v1.json",
            "authorization": artifact_dir / "authorization.json",
            "proposal": artifact_dir / "migration-proposal.json",
            "rollover": artifact_dir / "coordinator-rollover.v1.json",
        }
        identity = {
            "queue_run_id": "qr-test",
            "feature_run_id": "fr-test",
            "feature_index": 4,
            "base_branch": "main",
        }
        queue = {
            "protocol_version": "1.0",
            "dispatcher": "serial-implement-codex",
            "queue_run_id": "qr-test",
            "base_branch": "main",
            "state_revision": 3,
            "features": [
                {
                    "index": 4,
                    "description": "migrate",
                    "status": "blocked",
                    "codex_engine": "v13-codex",
                    "runner": "implement-v13-codex",
                    "feature_run_id": "fr-test",
                    "started_at": "2026-07-22T00:00:00Z",
                    "branch": "impl-codex-fr-test",
                    "worktree_name": "impl-codex-fr-test",
                    "worktree_path": ".claude/worktrees/impl-codex-fr-test",
                    "artifact_dir": "handoff/serial-runs/qr-test/fr-test",
                    "artifact_root": "handoff/serial-runs/qr-test/fr-test",
                    "checkpoint": "docs/development/current_implementation_checkpoint.json",
                    "checkpoint_path": ".claude/worktrees/impl-codex-fr-test/docs/development/current_implementation_checkpoint.json",
                    "transaction_path": "handoff/serial-runs/qr-test/fr-test/feature-transaction.v1.json",
                    "feature_result_path": "handoff/serial-runs/qr-test/fr-test/feature-result.v1.json",
                    "merge_receipt": "handoff/serial-runs/qr-test/fr-test/merge-receipt.v1.json",
                    "cleanup_proof": "handoff/serial-runs/qr-test/fr-test/cleanup-proof.v1.json",
                    "clearance_report": "handoff/serial-reports/2026-07-22-f4-fr-test.md",
                    "dispatch_lease": {
                        "coordinator_id": "coordinator-old",
                        "lease_id": "lease-old",
                        "state": "blocked",
                    },
                    "resume_token_sha256": hashlib.sha256(b"resume-token").hexdigest(),
                }
            ],
            "results": [],
        }
        paths["queue"].parent.mkdir(parents=True, exist_ok=True)
        paths["queue"].write_text(
            json.dumps(queue, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        atomic_write_json(
            paths["dispatch"],
            {
                "dispatch_action": "launch",
                **identity,
                "description": "migrate",
                "base_worktree_path": str(base),
                "branch": "impl-codex-fr-test",
                "worktree_name": "impl-codex-fr-test",
                "worktree_path": ".claude/worktrees/impl-codex-fr-test",
                "artifact_dir": "handoff/serial-runs/qr-test/fr-test",
                "checkpoint_path": ".claude/worktrees/impl-codex-fr-test/docs/development/current_implementation_checkpoint.json",
                "transaction_path": "handoff/serial-runs/qr-test/fr-test/feature-transaction.v1.json",
                "feature_result_path": "handoff/serial-runs/qr-test/fr-test/feature-result.v1.json",
                "queue_path": str(paths["queue"]),
                "coordinator_id": "coordinator-old",
                "lease_id": "lease-old",
                "planning_inputs": [],
                "run_directives": [],
            },
        )
        atomic_write_json(
            paths["checkpoint"],
            {
                **identity,
                "phase": "REVIEWING",
                "phase_detail": "fix",
                "phase_state": "blocked",
                "state_revision": 5,
            },
        )
        atomic_write_json(
            paths["transaction"],
            {
                **identity,
                "protocol": "implement-v13-codex/feature-transaction/1",
                "state": "prepared",
                "state_revision": 2,
                "history": [{"state": "prepared"}],
            },
        )
        atomic_write_json(
            paths["ledger"],
            {
                "protocol": "implement-v13-codex/review-closure-ledger/1",
                "feature_run_id": "fr-test",
                "state_revision": 7,
                "closures": [{"closure_id": "closure-active", "attempts": []}],
                "unknown_history": [{"preserved": True}],
            },
        )
        atomic_write_json(paths["authorization"], {"operator": "fixture", "approved": True})
        atomic_write_json(
            paths["proposal"],
            {
                "protocol": "implement-v13-codex/controller-migration-proposal/1",
                "identity": identity,
                "old_package_identity": "legacy_unfrozen",
                "new_package_digest": manifest["manifest_digest"],
                "authorization_evidence_sha256": sha256_file(paths["authorization"]),
                "rollover_summary_path": str(paths["rollover"]),
                "old_coordinator_thread": "thread-historical",
                "reason": "certified controller repair",
                "state_schema_versions": {
                    "queue": "1.0",
                    "checkpoint": "1.0",
                    "transaction": "1",
                    "ledger": "1",
                },
            },
        )
        command = [
            sys.executable,
            str(package_root / "implement-v13-codex/scripts/controller_package.py"),
            "migrate-run",
            "--proposal",
            str(paths["proposal"]),
            "--queue",
            str(paths["queue"]),
            "--dispatch",
            str(paths["dispatch"]),
            "--checkpoint",
            str(paths["checkpoint"]),
            "--transaction",
            str(paths["transaction"]),
            "--ledger",
            str(paths["ledger"]),
            "--journal",
            str(paths["journal"]),
            "--expected-queue-revision",
            "3",
            "--expected-dispatch-sha256",
            sha256_file(paths["dispatch"]),
            "--expected-checkpoint-revision",
            "5",
            "--expected-transaction-revision",
            "2",
            "--expected-ledger-revision",
            "7",
            "--certified-package-digest",
            manifest["manifest_digest"],
            "--authorization-evidence",
            str(paths["authorization"]),
        ]
        return {
            "temporary": temporary,
            "base": base,
            "artifact_dir": artifact_dir,
            "worktree": worktree,
            "package_root": package_root,
            "manifest": manifest,
            "paths": paths,
            "command": command,
        }

    def test_package_manifest_rejects_byte_and_mode_drift(self) -> None:
        fixture = self._fixture()
        self.addCleanup(fixture["temporary"].cleanup)
        package_root = fixture["package_root"]
        manifest = fixture["manifest"]
        verify_controller_package(package_root, manifest["manifest_digest"])
        target = package_root / "implement-v13-codex/scripts/run_exec.py"
        os.chmod(target, 0o700)
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaisesRegex(StateError, "hash mismatch"):
            verify_controller_package(package_root, manifest["manifest_digest"])

    def test_dry_run_is_read_only_and_commit_is_idempotent_with_immutable_dispatch(self) -> None:
        fixture = self._fixture()
        self.addCleanup(fixture["temporary"].cleanup)
        paths = fixture["paths"]
        before = {
            name: sha256_file(paths[name])
            for name in ("queue", "dispatch", "checkpoint", "transaction", "ledger")
        }
        dry = subprocess.run(
            [*fixture["command"], "--dry-run"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(paths["journal"].exists())
        self.assertEqual(
            before,
            {
                name: sha256_file(paths[name])
                for name in ("queue", "dispatch", "checkpoint", "transaction", "ledger")
            },
        )
        committed = subprocess.run(
            [*fixture["command"], "--commit"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)
        result = json.loads(committed.stdout)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(sha256_file(paths["dispatch"]), before["dispatch"])
        ledger = json.loads(paths["ledger"].read_text())
        self.assertEqual(ledger["unknown_history"], [{"preserved": True}])
        self.assertEqual(ledger["state_revision"], 8)
        second = subprocess.run(
            [*fixture["command"], "--commit"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout)["no_op"])
        self.assertEqual(
            json.loads(second.stdout)["migration_receipt_sha256"],
            result["migration_receipt_sha256"],
        )

    def test_every_durable_write_prefix_recovers_forward_and_is_not_authoritative_early(self) -> None:
        crash_labels = (
            "prepared_journal",
            "queue_write",
            "queue_ack",
            "dispatch_ack",
            "checkpoint_write",
            "checkpoint_ack",
            "transaction_write",
            "transaction_ack",
            "ledger_write",
            "ledger_ack",
            "rollover_write",
            "rollover_ack",
            "validated_journal",
            "committed_journal",
        )
        for label in crash_labels:
            with self.subTest(label=label):
                fixture = self._fixture()
                try:
                    environment = os.environ.copy()
                    environment["IMPLEMENT_V13_MIGRATION_CRASH_AFTER"] = label
                    crashed = subprocess.run(
                        [*fixture["command"], "--commit"],
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertEqual(crashed.returncode, 2)
                    if label != "committed_journal":
                        with self.assertRaisesRegex(StateError, "not committed"):
                            validate_committed_migration(fixture["paths"]["journal"])
                    recovered = subprocess.run(
                        [*fixture["command"], "--commit"],
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(
                        recovered.returncode, 0, recovered.stdout + recovered.stderr
                    )
                    journal = validate_committed_migration(fixture["paths"]["journal"])
                    self.assertEqual(journal["state"], "committed")
                    self.assertTrue(
                        all(
                            row["acknowledged"]
                            for row in journal["authorities"].values()
                        )
                    )
                finally:
                    fixture["temporary"].cleanup()

    def test_blocked_review_fix_resumes_only_through_run_feature_post_migration_gate(self) -> None:
        fixture = self._fixture()
        self.addCleanup(fixture["temporary"].cleanup)
        committed = subprocess.run(
            [*fixture["command"], "--commit"], text=True, capture_output=True
        )
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)
        migration_result = json.loads(committed.stdout)
        package_root = fixture["package_root"]
        serial = self._load_module(
            package_root / "serial-implement-codex/scripts/serial_state.py",
            "serial_state_migrated_e2e",
        )
        queue_path = fixture["paths"]["queue"]
        queue = serial.read_json(queue_path)
        feature = queue["features"][0]
        evidence = {
            "identity": serial._expected_resume_identity(queue, feature),
            "artifacts": {
                "checkpoint_sha256": sha256_file(fixture["paths"]["checkpoint"]),
                "transaction_sha256": sha256_file(fixture["paths"]["transaction"]),
            },
            "migration": {
                "journal_path": str(fixture["paths"]["journal"]),
                "migration_id": feature["controller_migration_id"],
                "controller_package_digest": feature["controller_package_digest"],
                "migration_receipt_sha256": migration_result[
                    "migration_receipt_sha256"
                ],
            },
        }
        serial.atomic_mutate(
            queue_path,
            lambda current: serial.resume_blocked_feature(
                current,
                index=4,
                token="resume-token",
                resolution_evidence=evidence,
                coordinator_id="coordinator-new",
                lease_id="lease-new",
                base_root=fixture["base"],
                require_controller_migration=True,
            ),
            expected_revision=4,
        )
        payload = {}

        def prepare(current):
            updated, dispatch = serial.prepare_dispatch(
                current,
                base_worktree_path=fixture["base"],
                coordinator_id="coordinator-new",
                lease_id="lease-new",
            )
            payload.update(dispatch)
            return updated

        serial.atomic_mutate(queue_path, prepare, expected_revision=5)
        payload["queue_path"] = str(queue_path)
        resume_dispatch = fixture["artifact_dir"] / "resume-dispatch.v1.json"
        atomic_write_json(resume_dispatch, payload)
        self.assertEqual(payload["dispatch_action"], "resume_existing_run")
        self.assertTrue(payload["controller_entrypoint"].endswith("/run_feature.py"))

        start_planning = self._load_module(
            package_root / "implement-v13-codex/scripts/start_planning.py",
            "start_planning_migrated_e2e",
        )
        with self.assertRaisesRegex(StateError, "rejects resume_existing_run"):
            start_planning.start(resume_dispatch)

        run_feature = self._load_module(
            package_root / "implement-v13-codex/scripts/run_feature.py",
            "run_feature_migrated_e2e",
        )
        run_feature.PACKAGE = package_root / "implement-v13-codex"
        run_feature.SERIAL_SCRIPT = (
            package_root / "serial-implement-codex/scripts/serial_state.py"
        )
        preflight = run_feature._prepare_resumed_run(
            dispatch=payload,
            base=fixture["base"],
            queue_path=queue_path,
            checkpoint_path=fixture["paths"]["checkpoint"],
            artifact_dir=fixture["artifact_dir"],
            expected_migration_sha256=migration_result[
                "migration_receipt_sha256"
            ],
            expected_package_digest=fixture["manifest"]["manifest_digest"],
            coordinator_id="coordinator-new",
            lease_id="lease-new",
        )
        self.assertEqual(preflight["status"], "passed")
        self.assertFalse(preflight["attempt_identity_created"])
        self.assertFalse(preflight["child_launched"])
        self.assertGreaterEqual(len(preflight["schemas"]), 16)
        checkpoint = json.loads(fixture["paths"]["checkpoint"].read_text())
        self.assertEqual(
            (checkpoint["phase"], checkpoint["phase_detail"], checkpoint["phase_state"]),
            ("REVIEWING", "fix", "ready"),
        )
        consumed = serial.read_json(queue_path)["features"][0]["dispatch_lease"]
        self.assertTrue(consumed["launch_consumed"])
        validate_committed_migration(
            fixture["paths"]["journal"],
            expected_package_digest=fixture["manifest"]["manifest_digest"],
            expected_receipt_sha256=migration_result["migration_receipt_sha256"],
            allow_queue_advance=True,
        )
        with self.assertRaisesRegex(StateError, "checkpoint"):
            validate_committed_migration(
                fixture["paths"]["journal"],
                expected_package_digest=fixture["manifest"]["manifest_digest"],
                expected_receipt_sha256=migration_result["migration_receipt_sha256"],
            )
        self.assertEqual(
            run_feature._recover_coordinator_position(
                fixture["artifact_dir"],
                "fr-test",
                run_feature._coordinator_generation(payload),
            ),
            (1, None),
        )
        self.assertFalse(
            list(fixture["artifact_dir"].glob("*.receipt.json")),
            "post-migration deterministic gate must run before any child receipt",
        )


if __name__ == "__main__":
    unittest.main()
