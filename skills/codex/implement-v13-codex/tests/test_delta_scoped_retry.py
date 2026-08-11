from __future__ import annotations

import copy
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1]
SCRIPTS = PACKAGE / "scripts"
SERIAL_SCRIPT = SCRIPTS / "feature_queue_state.py"
SERIAL_TEST = PACKAGE / "tests" / "test_feature_queue_state.py"
sys.path.insert(0, str(SCRIPTS))

from feature_state import (  # noqa: E402
    initialize_checkpoint,
    resume_checkpoint_delta_scoped,
)
from review_closure import (  # noqa: E402
    DELTA_SCOPE_PROTOCOL,
    PROTOCOL as LEDGER_PROTOCOL,
    freeze_delta_scope,
    validate_delta_scope,
)
from run_feature import _resume_checkpoint_with_authorization  # noqa: E402
from state_io import StateError, atomic_write_json, read_json, sha256_file  # noqa: E402

SPEC = importlib.util.spec_from_file_location("serial_state_fixture", SERIAL_SCRIPT)
assert SPEC and SPEC.loader
serial_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serial_state)
TEST_SPEC = importlib.util.spec_from_file_location("serial_state_test_fixture", SERIAL_TEST)
assert TEST_SPEC and TEST_SPEC.loader
serial_fixture = importlib.util.module_from_spec(TEST_SPEC)
TEST_SPEC.loader.exec_module(serial_fixture)


CANDIDATE_SHA = "1f" * 20
AUTHORIZATION = {
    "authorization_sha256": "a" * 64,
    "resolution_evidence": {"record": "operator-resolution"},
    "authorized_at": "2026-08-11T00:00:00Z",
}


def _ledger_document(feature_run_id: str = "fr_fixed") -> dict:
    return {
        "protocol": LEDGER_PROTOCOL,
        "feature_run_id": feature_run_id,
        "state_revision": 4,
        "attempts_before_escalation": 3,
        "active_closure_id": "closure-open",
        "closures": [
            {
                "closure_id": "closure-closed",
                "status": "closed",
                "fingerprints": ["fp-closed-1"],
                "immutable_test_nodes": [
                    {
                        "node_id": "test-closed",
                        "command": ["python3", "-m", "pytest", "tests/test_closed.py"],
                    }
                ],
            },
            {
                "closure_id": "closure-open",
                "status": "escalation_required",
                "fingerprints": ["fp-open-1", "fp-open-2"],
                "immutable_test_nodes": [
                    {
                        "node_id": "test-open",
                        "command": ["python3", "-m", "pytest", "tests/test_open.py"],
                    }
                ],
            },
        ],
    }


def _checkpoint_payload() -> dict:
    return {
        "task": "delta retry",
        "base_branch": "main",
        "worktree_name": "impl-codex-fr_fixed",
        "branch": "impl-codex-fr_fixed",
        "feature_index": 0,
        "queue_run_id": "qr_fixed",
        "feature_run_id": "fr_fixed",
    }


def _blocked_checkpoint(path: Path, phase: str, detail: str) -> dict:
    checkpoint = initialize_checkpoint(path, _checkpoint_payload())
    checkpoint.update(
        phase=phase,
        phase_detail=detail,
        phase_state="blocked",
        active_blocker={
            "phase": phase,
            "phase_detail": detail,
            "blocker_class": "review_cycle_exhausted",
            "reason": "open findings remained after the cycle limit",
            "resume_condition": "delta-scoped retry with the frozen ledger",
            "resolution_evidence": [],
            "at": "2026-08-11T00:00:00Z",
        },
    )
    atomic_write_json(path, checkpoint)
    return checkpoint


class FreezeDeltaScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger_path = self.root / "review-closure-ledger.v1.json"
        atomic_write_json(self.ledger_path, _ledger_document())

    def test_freeze_collects_only_open_findings_and_slice(self) -> None:
        scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)
        self.assertEqual(scope["protocol"], DELTA_SCOPE_PROTOCOL)
        self.assertEqual(scope["open_fingerprints"], ["fp-open-1", "fp-open-2"])
        self.assertEqual([item["closure_id"] for item in scope["open_closures"]], ["closure-open"])
        self.assertEqual(scope["verification_slice"]["test_nodes"], ["test-open"])
        self.assertEqual(
            scope["verification_slice"]["commands"],
            [["python3", "-m", "pytest", "tests/test_open.py"]],
        )
        self.assertEqual(scope["ledger_sha256"], sha256_file(self.ledger_path))
        self.assertEqual(scope["candidate_commit_sha"], CANDIDATE_SHA)

    def test_freeze_rejects_ledger_without_open_closures(self) -> None:
        document = _ledger_document()
        for closure in document["closures"]:
            closure["status"] = "closed"
        atomic_write_json(self.ledger_path, document)
        with self.assertRaisesRegex(StateError, "at least one open closure"):
            freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)

    def test_freeze_rejects_open_closure_without_verification_slice(self) -> None:
        document = _ledger_document()
        document["closures"][1]["immutable_test_nodes"] = []
        atomic_write_json(self.ledger_path, document)
        with self.assertRaisesRegex(StateError, "bound verification slice"):
            freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)

    def test_freeze_rejects_invalid_candidate_sha(self) -> None:
        with self.assertRaisesRegex(StateError, "40-hex candidate commit sha"):
            freeze_delta_scope(self.ledger_path, candidate_commit_sha="not-a-sha")

    def test_validate_accepts_fresh_scope(self) -> None:
        scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)
        self.assertEqual(validate_delta_scope(self.ledger_path, scope), scope)

    def test_validate_rejects_drifted_ledger(self) -> None:
        scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)
        document = _ledger_document()
        document["closures"][1]["status"] = "ready_for_fix"
        atomic_write_json(self.ledger_path, document)
        with self.assertRaisesRegex(StateError, "ledger hash does not match"):
            validate_delta_scope(self.ledger_path, scope)

    def test_validate_rejects_tampered_open_fingerprints(self) -> None:
        scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)
        tampered = copy.deepcopy(scope)
        tampered["open_fingerprints"] = ["fp-open-1"]
        with self.assertRaisesRegex(StateError, "open fingerprints do not match"):
            validate_delta_scope(self.ledger_path, tampered)


class DeltaScopedCheckpointResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkpoint_path = self.root / "checkpoint.json"
        self.ledger_path = self.root / "review-closure-ledger.v1.json"
        atomic_write_json(self.ledger_path, _ledger_document())
        self.scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha=CANDIDATE_SHA)

    def test_delta_resume_rewinds_blocked_committing_to_reviewing_fix(self) -> None:
        _blocked_checkpoint(self.checkpoint_path, "COMMITTING", "final_gates")
        updated = resume_checkpoint_delta_scoped(
            self.checkpoint_path, 0, AUTHORIZATION, self.scope
        )
        self.assertEqual(updated["phase"], "REVIEWING")
        self.assertEqual(updated["phase_detail"], "fix")
        self.assertEqual(updated["phase_state"], "ready")
        self.assertIsNone(updated["active_blocker"])
        self.assertEqual(updated["delta_resume_scope"], self.scope)
        entry = updated["blocked_history"][-1]
        self.assertEqual(entry["resume_mode"], "delta_scoped")
        self.assertEqual(entry["phase"], "COMMITTING")
        self.assertEqual(entry["phase_detail"], "final_gates")
        self.assertEqual(entry["candidate_commit_sha"], CANDIDATE_SHA)
        self.assertEqual(entry["ledger_sha256"], self.scope["ledger_sha256"])

    def test_delta_resume_reopens_blocked_reviewing_fix_in_place(self) -> None:
        _blocked_checkpoint(self.checkpoint_path, "REVIEWING", "fix")
        updated = resume_checkpoint_delta_scoped(
            self.checkpoint_path, 0, AUTHORIZATION, self.scope
        )
        self.assertEqual((updated["phase"], updated["phase_detail"]), ("REVIEWING", "fix"))
        self.assertEqual(updated["phase_state"], "ready")

    def test_delta_resume_rejects_pre_review_positions(self) -> None:
        _blocked_checkpoint(self.checkpoint_path, "IMPLEMENTING", "workers_dispatch")
        with self.assertRaisesRegex(StateError, "at or after REVIEWING/fix"):
            resume_checkpoint_delta_scoped(self.checkpoint_path, 0, AUTHORIZATION, self.scope)

    def test_delta_resume_rejects_unblocked_checkpoint(self) -> None:
        initialize_checkpoint(self.checkpoint_path, _checkpoint_payload())
        with self.assertRaisesRegex(StateError, "only a blocked checkpoint"):
            resume_checkpoint_delta_scoped(self.checkpoint_path, 0, AUTHORIZATION, self.scope)

    def test_delta_resume_rejects_malformed_scope(self) -> None:
        _blocked_checkpoint(self.checkpoint_path, "COMMITTING", "final_gates")
        for mutation in (
            {"protocol": "implement-v13-codex/other/1"},
            {"candidate_commit_sha": "zz" * 20},
            {"open_fingerprints": []},
            {"verification_slice": {"test_nodes": [], "commands": []}},
        ):
            scope = {**copy.deepcopy(self.scope), **mutation}
            with self.assertRaises(StateError):
                resume_checkpoint_delta_scoped(self.checkpoint_path, 0, AUTHORIZATION, scope)

    def test_delta_resume_requires_dispatcher_authorization(self) -> None:
        _blocked_checkpoint(self.checkpoint_path, "COMMITTING", "final_gates")
        with self.assertRaisesRegex(StateError, "authorization digest is invalid"):
            resume_checkpoint_delta_scoped(
                self.checkpoint_path,
                0,
                {**AUTHORIZATION, "authorization_sha256": "short"},
                self.scope,
            )


class QueueDeltaScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def _blocked_queue_with_evidence(self) -> tuple[dict, dict, str]:
        active, _ = serial_fixture.prepared_queue()
        token = "operator-resolution-1"
        blocked = serial_state.block_feature(
            active,
            index=0,
            coordinator_id="coordinator-1",
            lease_id="lease_fixed",
            blocker={
                "blocker_class": "review_cycle_exhausted",
                "reason": "open findings remained",
                "resume_condition": "delta-scoped retry",
            },
            resume_token=token,
        )
        feature = blocked["features"][0]
        identity_document = {
            "queue_run_id": blocked["queue_run_id"],
            "feature_run_id": feature["feature_run_id"],
            "feature_index": feature["index"],
            "base_branch": blocked["base_branch"],
        }
        checkpoint_path = self.root / feature["checkpoint_path"]
        transaction_path = self.root / feature["transaction_path"]
        serial_fixture.write_json(checkpoint_path, {**identity_document, "phase": "REVIEWING"})
        serial_fixture.write_json(transaction_path, {**identity_document, "state": "prepared"})
        evidence = {
            "identity": serial_state._expected_resume_identity(blocked, feature),
            "record": "decision-1",
            "artifacts": {
                "checkpoint_sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
                "transaction_sha256": hashlib.sha256(transaction_path.read_bytes()).hexdigest(),
            },
        }
        return blocked, evidence, token

    def _scope_for(self, feature: dict) -> dict:
        ledger_path = self.root / "review-closure-ledger.v1.json"
        atomic_write_json(ledger_path, _ledger_document(feature["feature_run_id"]))
        return freeze_delta_scope(ledger_path, candidate_commit_sha=CANDIDATE_SHA)

    def test_resume_stores_validated_delta_scope_in_authorization(self) -> None:
        blocked, evidence, token = self._blocked_queue_with_evidence()
        scope = self._scope_for(blocked["features"][0])
        resumed = serial_state.resume_blocked_feature(
            blocked,
            index=0,
            token=token,
            resolution_evidence=evidence,
            coordinator_id="coordinator-2",
            lease_id="lease-2",
            base_root=self.root,
            delta_scope=scope,
        )
        authorization = resumed["features"][0]["resume_authorization"]
        self.assertEqual(authorization["delta_scope"], scope)

    def test_resume_rejects_delta_scope_with_drifted_ledger(self) -> None:
        blocked, evidence, token = self._blocked_queue_with_evidence()
        scope = self._scope_for(blocked["features"][0])
        ledger_path = self.root / "review-closure-ledger.v1.json"
        document = read_json(ledger_path)
        document["closures"][1]["status"] = "ready_for_fix"
        atomic_write_json(ledger_path, document)
        with self.assertRaisesRegex(
            serial_state.AuthorizationError, "ledger hash does not match"
        ):
            serial_state.resume_blocked_feature(
                blocked,
                index=0,
                token=token,
                resolution_evidence=evidence,
                coordinator_id="coordinator-2",
                lease_id="lease-2",
                base_root=self.root,
                delta_scope=scope,
            )

    def test_resume_rejects_delta_scope_for_another_run_or_escaping_path(self) -> None:
        blocked, evidence, token = self._blocked_queue_with_evidence()
        scope = self._scope_for(blocked["features"][0])
        for mutation, message in (
            ({"feature_run_id": "fr_other"}, "another feature run"),
            ({"ledger_path": "/etc/passwd"}, "escapes the base checkout"),
        ):
            with self.assertRaisesRegex(serial_state.AuthorizationError, message):
                serial_state.resume_blocked_feature(
                    copy.deepcopy(blocked),
                    index=0,
                    token=token,
                    resolution_evidence=evidence,
                    coordinator_id="coordinator-2",
                    lease_id="lease-2",
                    base_root=self.root,
                    delta_scope={**copy.deepcopy(scope), **mutation},
                )


class ControllerDeltaResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.worktree, check=True)
        (self.worktree / "candidate.txt").write_text("verified candidate\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.worktree, check=True)
        subprocess.run(
            [
                "git",
                "-c", "user.email=test@example.com",
                "-c", "user.name=Test",
                "commit", "-q", "-m", "verified candidate",
            ],
            cwd=self.worktree,
            check=True,
        )
        self.head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.checkpoint_path = self.root / "checkpoint.json"
        self.ledger_path = self.root / "review-closure-ledger.v1.json"
        atomic_write_json(self.ledger_path, _ledger_document())

    def test_delta_authorization_verifies_candidate_and_reopens_reviewing_fix(self) -> None:
        checkpoint = _blocked_checkpoint(self.checkpoint_path, "COMMITTING", "final_gates")
        scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha=self.head)
        updated = _resume_checkpoint_with_authorization(
            self.checkpoint_path,
            checkpoint,
            {**AUTHORIZATION, "delta_scope": scope},
            self.worktree,
        )
        self.assertEqual((updated["phase"], updated["phase_detail"]), ("REVIEWING", "fix"))
        self.assertEqual(updated["phase_state"], "ready")
        self.assertEqual(updated["delta_resume_scope"], scope)

    def test_delta_authorization_rejects_wrong_worktree_head(self) -> None:
        checkpoint = _blocked_checkpoint(self.checkpoint_path, "COMMITTING", "final_gates")
        scope = freeze_delta_scope(self.ledger_path, candidate_commit_sha="0" * 40)
        with self.assertRaisesRegex(StateError, "worktree HEAD to equal the verified candidate"):
            _resume_checkpoint_with_authorization(
                self.checkpoint_path,
                checkpoint,
                {**AUTHORIZATION, "delta_scope": scope},
                self.worktree,
            )

    def test_plain_authorization_reopens_same_detail(self) -> None:
        checkpoint = _blocked_checkpoint(self.checkpoint_path, "REVIEWING", "fix")
        updated = _resume_checkpoint_with_authorization(
            self.checkpoint_path,
            checkpoint,
            dict(AUTHORIZATION),
            self.worktree,
        )
        self.assertEqual((updated["phase"], updated["phase_detail"]), ("REVIEWING", "fix"))
        self.assertEqual(updated["phase_state"], "ready")
        self.assertNotIn("delta_resume_scope", updated)


if __name__ == "__main__":
    unittest.main()
