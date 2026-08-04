from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "serial_state.py"
BASE_WORKTREE_PATH = Path("/absolute/base-worktree")
SPEC = importlib.util.spec_from_file_location("serial_state", SCRIPT)
assert SPEC and SPEC.loader
serial_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serial_state)


def queue_with(*features: dict) -> dict:
    return {
        "base_branch": "feature/base",
        "features": list(features),
        "current_index": 0,
        "started": "2026-07-19T00:00:00Z",
        "results": [],
        "state_revision": 0,
        "unknown_top": {"preserve": True},
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def prepared_queue(index: object = 0) -> tuple[dict, dict]:
    queue = queue_with(
        {
            "index": index,
            "description": "feature",
            "status": "pending",
            "engine": "v13-codex",
            "feature_unknown": 9,
        }
    )
    ids = iter(("qr_fixed", "fr_fixed", "lease_fixed"))
    return serial_state.prepare_dispatch(
        queue,
        base_worktree_path=BASE_WORKTREE_PATH,
        coordinator_id="coordinator-1",
        now="2026-07-19T02:00:00Z",
        new_id=lambda _prefix: next(ids),
    )


def terminal_fixture(root: Path) -> tuple[Path, Path, Path, dict, dict, dict]:
    queue, _ = prepared_queue(0)
    feature = queue["features"][0]
    queue_path = root / "queue.json"
    transaction_path = root / feature["transaction_path"]
    result_path = root / feature["feature_result_path"]
    manifest_path = root / "docs/development/runs/feature.md"
    merge_path = root / feature["merge_receipt"]
    clearance_path = root / feature["clearance_report"]
    cleanup_path = root / feature["cleanup_proof"]
    for path, content in (
        (manifest_path, "manifest\n"),
        (clearance_path, "clear\n"),
        (cleanup_path, '{"clean":true}\n'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    write_json(
        merge_path,
        {
            "protocol": "implement-v13-codex/merge-receipt/1",
            "queue_run_id": queue["queue_run_id"],
            "feature_run_id": feature["feature_run_id"],
            "base_head_before": "before",
            "base_head_after": "abc123",
            "merge_commit": "merge123",
            "manifest": "docs/development/runs/feature.md",
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "ancestry_verified": True,
            "target_checkout": str(root),
            "cleanup": {"complete": True},
            "guards": {"passed": True},
        },
    )
    required_states = (
        "prepared",
        "feature_committed",
        "manifest_committed",
        "merge_prepared",
        "merged",
        "cleanup_complete",
        "feature_result_written",
    )
    transaction = {
        "protocol": "implement-v13-codex/feature-transaction/1",
        "queue_run_id": queue["queue_run_id"],
        "feature_run_id": feature["feature_run_id"],
        "feature_index": feature["index"],
        "base_branch": queue["base_branch"],
        "base_head": "abc123",
        "state": "feature_result_written",
        "state_revision": 6,
        "history": [{"state": state, "at": "2026-07-19T03:00:00Z"} for state in required_states],
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "merge_receipt_sha256": hashlib.sha256(merge_path.read_bytes()).hexdigest(),
        "transaction_unknown": "keep",
    }
    result = {
        "protocol": "implement-v13-codex/feature-result/1",
        "status": "done",
        "queue_run_id": queue["queue_run_id"],
        "feature_run_id": feature["feature_run_id"],
        "feature_index": feature["index"],
        "completed_at": "2026-07-19T04:00:00Z",
        "manifest": "docs/development/runs/feature.md",
        "merge_receipt": feature["merge_receipt"],
        "clearance_report": feature["clearance_report"],
        "base_head": "abc123",
        "cleanup_proof": feature["cleanup_proof"],
    }
    write_json(queue_path, queue)
    write_json(transaction_path, transaction)
    write_json(result_path, result)
    transaction["feature_result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
    write_json(transaction_path, transaction)
    return queue_path, transaction_path, result_path, queue, transaction, result


class QueueTests(unittest.TestCase):
    def _git_base(self, branch: str = "feature/base") -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-b", branch], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=root, check=True, capture_output=True)
        return root

    def test_dispatch_cli_persists_exact_payload(self) -> None:
        root = self._git_base()
        queue_path = root / "queue.json"
        output_path = root / "dispatch.json"
        queue = queue_with(
            {
                "index": 0,
                "description": "feature",
                "status": "pending",
                "engine": "v13-codex",
            }
        )
        queue.update({"paused": True, "pause_reason": "operator stop"})
        write_json(queue_path, queue)
        with contextlib.redirect_stdout(io.StringIO()):
            result = serial_state.main(
                [
                    "dispatch",
                    str(queue_path),
                    "--base-worktree-path",
                    str(root),
                    "--coordinator-id",
                    "coordinator",
                    "--clear-pause",
                    "--output",
                    str(output_path),
                ]
            )
        self.assertEqual(result, 0)
        persisted = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["dispatch_action"], "launch")
        self.assertEqual(persisted["base_worktree_path"], str(root))
        self.assertEqual(persisted["queue_path"], str(queue_path.resolve()))
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        self.assertFalse(queue["paused"])
        self.assertIsNone(queue["pause_reason"])
        self.assertEqual(queue["pause_history"][-1]["clearance"], "explicit_operator_start")

    def test_custom_seed_plan_can_be_added_before_dispatch(self) -> None:
        original = queue_with(
            {"index": 0, "description": "feature", "status": "pending", "engine": "v13-codex"}
        )
        planning_input = {
            "id": "operator_plan",
            "path": "handoff/operator-plan.md",
            "role": "seed_plan",
            "revision": "snapshot",
            "update_policy": "verify_only",
            "allow_external_snapshot": True,
        }
        updated = serial_state.add_planning_input(original, planning_input, feature_index=0)
        self.assertEqual(updated["features"][0]["planning_inputs"], [planning_input])
        _, payload = serial_state.prepare_dispatch(
            updated,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator",
            new_id=lambda prefix: f"{prefix}_fixed",
        )
        self.assertEqual(payload["planning_inputs"], [planning_input])
        active = copy.deepcopy(updated)
        active["features"][0]["status"] = "in_progress"
        with self.assertRaisesRegex(serial_state.SerialStateError, "active"):
            serial_state.add_planning_input(active, planning_input)

    def test_pause_is_hard_gate_before_any_identity_assignment(self) -> None:
        original = queue_with({"index": 0, "description": "one", "status": "pending", "engine": "v13-codex"})
        original.update({"paused": True, "pause_reason": "operator stop"})
        with self.assertRaises(serial_state.QueuePausedError):
            serial_state.prepare_dispatch(
                original,
                base_worktree_path=BASE_WORKTREE_PATH,
                coordinator_id="coordinator",
                new_id=lambda prefix: f"{prefix}_new",
            )
        self.assertNotIn("queue_run_id", original)
        self.assertEqual(original["features"][0]["status"], "pending")

    def test_clear_pause_dispatches_current_pending_feature_atomically(self) -> None:
        original = queue_with(
            {"index": 0, "description": "one", "status": "pending", "engine": "v13-codex"}
        )
        original.update({"paused": True, "pause_reason": "post-feature stop"})
        updated, payload = serial_state.prepare_dispatch(
            original,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator",
            clear_pause=True,
            now="2026-07-21T03:00:00Z",
            new_id=lambda prefix: f"{prefix}_fixed",
        )
        self.assertTrue(original["paused"])
        self.assertFalse(updated["paused"])
        self.assertIsNone(updated["pause_reason"])
        self.assertEqual(updated["features"][0]["status"], "in_progress")
        self.assertEqual(updated["pause_history"][-1]["pause_reason"], "post-feature stop")
        self.assertEqual(updated["pause_history"][-1]["feature_index"], 0)
        self.assertEqual(payload["dispatch_action"], "launch")

    def test_clear_pause_rejects_unpaused_active_or_blocked_queue(self) -> None:
        unpaused = queue_with(
            {"index": 0, "description": "one", "status": "pending", "engine": "v13-codex"}
        )
        with self.assertRaisesRegex(serial_state.QueuePausedError, "requires a paused"):
            serial_state.prepare_dispatch(
                unpaused,
                base_worktree_path=BASE_WORKTREE_PATH,
                coordinator_id="coordinator",
                clear_pause=True,
            )
        for status in ("in_progress", "blocked"):
            queue = queue_with(
                {"index": 0, "description": "one", "status": status, "engine": "v13-codex"}
            )
            queue.update({"paused": True, "pause_reason": "stop"})
            with self.assertRaisesRegex(serial_state.QueuePausedError, "current feature to be pending"):
                serial_state.prepare_dispatch(
                    queue,
                    base_worktree_path=BASE_WORKTREE_PATH,
                    coordinator_id="coordinator",
                    clear_pause=True,
                )

    def test_dispatch_cli_rejects_detached_base_without_mutation_or_payload(self) -> None:
        root = self._git_base()
        queue_path = root / "queue.json"
        output_path = root / "dispatch.json"
        queue = queue_with(
            {"index": 0, "description": "feature", "status": "pending", "engine": "v13-codex"}
        )
        queue.update({"paused": True, "pause_reason": "operator stop"})
        write_json(queue_path, queue)
        before = queue_path.read_bytes()
        subprocess.run(["git", "checkout", "--detach"], cwd=root, check=True, capture_output=True)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = serial_state.main(
                [
                    "dispatch",
                    str(queue_path),
                    "--base-worktree-path",
                    str(root),
                    "--coordinator-id",
                    "coordinator",
                    "--clear-pause",
                    "--output",
                    str(output_path),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("detached", stdout.getvalue())
        self.assertEqual(queue_path.read_bytes(), before)
        self.assertFalse(output_path.exists())

    def test_dispatch_cli_rejects_wrong_branch_without_mutation(self) -> None:
        root = self._git_base(branch="other")
        queue_path = root / "queue.json"
        queue = queue_with(
            {"index": 0, "description": "feature", "status": "pending", "engine": "v13-codex"}
        )
        write_json(queue_path, queue)
        before = queue_path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            result = serial_state.main(
                [
                    "dispatch",
                    str(queue_path),
                    "--base-worktree-path",
                    str(root),
                    "--coordinator-id",
                    "coordinator",
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(queue_path.read_bytes(), before)

    def test_explicit_adoption_changes_pending_only_and_preserves_unknowns(self) -> None:
        original = queue_with(
            {"index": "done", "description": "done", "status": "done", "engine": "solo", "x": 1},
            {"index": "next", "description": "next", "status": "pending", "engine": "solo", "y": 2},
        )
        updated = serial_state.adopt_pending_engine(
            original,
            from_engine="solo",
            token="adopt-pending:solo:v13-codex",
            now="2026-07-19T01:00:00Z",
            new_id=lambda _prefix: "qr_adopt",
        )
        self.assertEqual(updated["features"][0], original["features"][0])
        self.assertEqual(updated["features"][1]["engine"], "solo")
        self.assertEqual(updated["features"][1]["codex_engine"], "v13-codex")
        self.assertEqual(updated["features"][1]["y"], 2)
        self.assertEqual(updated["unknown_top"], {"preserve": True})
        self.assertNotIn("adopt-pending:solo:v13-codex", json.dumps(updated))

    def test_adoption_rejects_wrong_token_and_foreign_active_feature(self) -> None:
        pending = queue_with({"index": 0, "description": "one", "status": "pending", "engine": "solo"})
        with self.assertRaises(serial_state.AuthorizationError):
            serial_state.adopt_pending_engine(pending, from_engine="solo", token="yes")
        active = queue_with(
            {"index": 0, "description": "active", "status": "in_progress", "engine": "solo"},
            {"index": 1, "description": "next", "status": "pending", "engine": "solo"},
        )
        with self.assertRaises(serial_state.ForeignEngineError):
            serial_state.adopt_pending_engine(
                active, from_engine="solo", token="adopt-pending:solo:v13-codex"
            )

    def test_dispatch_assigns_paths_metadata_q1_and_inputs(self) -> None:
        original = queue_with(
            {
                "index": 0,
                "description": "feature",
                "status": "pending",
                "engine": "v13-codex",
                "custom": "keep",
                "planning_inputs": [
                    {"id": "module", "path": "module/context.md", "role": "governing"},
                    {"id": "seed", "path": "plans/seed.md", "role": "seed_plan"},
                ],
            }
        )
        original["planning_inputs"] = [
            {"id": "module", "path": "default/context.md", "role": "background"},
            {"id": "priority", "path": "NEXT_STEPS.md", "role": "governing"},
        ]
        original["run_directives"] = ["obsolete historical directive"]
        original["active_run_directives"] = ["active queue directive"]
        original["features"][0]["run_directives"] = ["feature directive"]
        ids = iter(("qr_fixed", "fr_fixed", "lease_fixed"))
        updated, payload = serial_state.prepare_dispatch(
            original,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator-1",
            now="2026-07-19T02:00:00Z",
            new_id=lambda _prefix: next(ids),
        )
        feature = updated["features"][0]
        self.assertEqual(feature["decision_key"], "Q1")
        self.assertEqual(feature["decision_record"], "docs/development/decisions/2026-07-q1-decisions.md")
        self.assertEqual(payload["dispatch_action"], "launch")
        self.assertEqual(payload["coordinator_id"], "coordinator-1")
        self.assertEqual(payload["lease_id"], "lease_fixed")
        self.assertEqual(payload["base_worktree_path"], str(BASE_WORKTREE_PATH))
        self.assertEqual(payload["worktree_path"], ".claude/worktrees/impl-codex-fr_fixed")
        self.assertEqual(payload["transaction_path"], "handoff/serial-runs/qr_fixed/fr_fixed/feature-transaction.v1.json")
        self.assertEqual(feature["custom"], "keep")
        self.assertEqual(
            [item["path"] for item in payload["planning_inputs"]],
            ["module/context.md", "NEXT_STEPS.md", "plans/seed.md"],
        )
        self.assertEqual(payload["run_directives"], ["active queue directive", "feature directive"])

    def test_dispatch_does_not_forward_legacy_cumulative_run_directives(self) -> None:
        original = queue_with(
            {"index": 0, "description": "feature", "status": "pending", "engine": "v13-codex"}
        )
        original["run_directives"] = ["old hard stop", "use obsolete Claude skill"]
        _, payload = serial_state.prepare_dispatch(
            original,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator",
            new_id=lambda prefix: f"{prefix}_fixed",
        )
        self.assertEqual(payload["run_directives"], [])

    def test_dispatch_rejects_invalid_active_run_directives(self) -> None:
        original = queue_with(
            {"index": 0, "description": "feature", "status": "pending", "engine": "v13-codex"}
        )
        original["active_run_directives"] = [""]
        with self.assertRaisesRegex(serial_state.SerialStateError, "active_run_directives"):
            serial_state.prepare_dispatch(
                original,
                base_worktree_path=BASE_WORKTREE_PATH,
                coordinator_id="coordinator",
                new_id=lambda prefix: f"{prefix}_fixed",
            )

    def test_codex_engine_extension_dispatches_without_changing_claude_engine(self) -> None:
        original = queue_with(
            {
                "index": 10,
                "description": "shared queue feature",
                "status": "pending",
                "engine": "solo",
                "codex_engine": "v13-codex",
            }
        )
        inspection_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "queue.json"
        write_json(inspection_path, original)
        inspection = serial_state.inspect_queue(inspection_path)
        self.assertEqual(inspection["codex_enabled_indexes"], [10])
        self.assertEqual(inspection["adoption_tokens"], [])
        updated, payload = serial_state.prepare_dispatch(
            original,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator",
            new_id=lambda prefix: f"{prefix}_fixed",
        )
        self.assertEqual(updated["features"][0]["engine"], "solo")
        self.assertEqual(updated["features"][0]["codex_engine"], "v13-codex")
        self.assertEqual(payload["engine"], "v13-codex")

    def test_codex_engine_extension_rejects_unknown_value(self) -> None:
        queue = queue_with(
            {
                "index": 0,
                "description": "feature",
                "status": "pending",
                "engine": "solo",
                "codex_engine": "future-engine",
            }
        )
        with self.assertRaisesRegex(serial_state.SerialStateError, "unsupported codex_engine"):
            serial_state.validate_queue(queue)

    def test_dispatch_lease_prevents_duplicate_launch(self) -> None:
        updated, first = prepared_queue()
        self.assertEqual(first["dispatch_action"], "launch")
        with self.assertRaises(serial_state.DispatchLeaseError):
            serial_state.prepare_dispatch(
                updated, base_worktree_path=BASE_WORKTREE_PATH, coordinator_id="coordinator-2"
            )
        with self.assertRaises(serial_state.DispatchLeaseError):
            serial_state.prepare_dispatch(
                updated, base_worktree_path=BASE_WORKTREE_PATH, coordinator_id="coordinator-1"
            )
        _, retry = serial_state.prepare_dispatch(
            updated,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator-1",
            lease_id="lease_fixed",
        )
        self.assertEqual(retry["dispatch_action"], "reattach")

    def test_dispatch_lease_id_cannot_be_reused_across_features(self) -> None:
        queue = queue_with(
            {
                "index": 0,
                "description": "done",
                "status": "done",
                "dispatch_lease": {"lease_id": "lease-used", "state": "complete"},
            },
            {"index": 1, "description": "next", "status": "pending", "engine": "v13-codex"},
        )
        with self.assertRaisesRegex(serial_state.DispatchLeaseError, "already assigned"):
            serial_state.prepare_dispatch(
                queue,
                base_worktree_path=BASE_WORKTREE_PATH,
                coordinator_id="coordinator",
                lease_id="lease-used",
                new_id=lambda prefix: f"{prefix}_fixed",
            )

    def test_dispatch_refuses_foreign_engine_and_does_not_skip_blocked(self) -> None:
        foreign = queue_with({"index": 0, "description": "one", "status": "pending", "engine": "solo"})
        with self.assertRaises(serial_state.ForeignEngineError):
            serial_state.prepare_dispatch(
                foreign, base_worktree_path=BASE_WORKTREE_PATH, coordinator_id="coordinator"
            )
        blocked = queue_with(
            {"index": 0, "description": "blocked", "status": "blocked", "engine": "v13-codex"},
            {"index": 1, "description": "later", "status": "pending", "engine": "v13-codex"},
        )
        with self.assertRaises(serial_state.QueueBlockedError):
            serial_state.prepare_dispatch(
                blocked, base_worktree_path=BASE_WORKTREE_PATH, coordinator_id="coordinator"
            )

    def test_dispatch_rejects_relative_base_worktree_path(self) -> None:
        queue = queue_with(
            {"index": 0, "description": "one", "status": "pending", "engine": "v13-codex"}
        )
        with self.assertRaisesRegex(serial_state.SerialStateError, "must be absolute"):
            serial_state.prepare_dispatch(
                queue, base_worktree_path="relative/base", coordinator_id="coordinator"
            )

    def test_block_operation_requires_active_lease(self) -> None:
        active, _ = prepared_queue()
        blocker = {
            "blocker_class": "operator_decision",
            "reason": "choice required",
            "resume_condition": "record choice",
            "unknown": "keep",
        }
        with self.assertRaises(serial_state.DispatchLeaseError):
            serial_state.block_feature(
                active,
                index=0,
                coordinator_id="other",
                lease_id="lease_fixed",
                blocker=blocker,
                resume_token="token",
            )
        blocked = serial_state.block_feature(
            active,
            index=0,
            coordinator_id="coordinator-1",
            lease_id="lease_fixed",
            blocker=blocker,
            resume_token="token",
            now="2026-07-19T03:00:00Z",
        )
        feature = blocked["features"][0]
        self.assertEqual(feature["status"], "blocked")
        self.assertEqual(feature["dispatch_lease"]["state"], "blocked")
        self.assertEqual(feature["blocker"]["unknown"], "keep")
        self.assertEqual(feature["resume_token_sha256"], hashlib.sha256(b"token").hexdigest())
        self.assertNotEqual(feature["resume_token_sha256"], "token")

    def test_model_coordinator_child_cannot_block_queue(self) -> None:
        active, _ = prepared_queue()
        with patch.dict(
            "os.environ", {serial_state.CONTROLLER_CHILD_ENV: "1"}, clear=False
        ):
            with self.assertRaisesRegex(
                serial_state.SerialStateError, "may not mutate the serial queue"
            ):
                serial_state.block_feature(
                    active,
                    index=0,
                    coordinator_id="coordinator-1",
                    lease_id="lease_fixed",
                    blocker={
                        "blocker_class": "debug",
                        "reason": "must be rejected",
                        "resume_condition": "outer controller settles",
                    },
                    resume_token="token",
                )

    def test_blocked_resume_requires_first_position_no_active_and_exact_identity(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        active, _ = prepared_queue()
        token = "operator-resolution-1"
        blocked = serial_state.block_feature(
            active,
            index=0,
            coordinator_id="coordinator-1",
            lease_id="lease_fixed",
            blocker={
                "blocker_class": "operator_decision",
                "reason": "choice required",
                "resume_condition": "record choice",
            },
            resume_token=token,
        )
        identity = serial_state._expected_resume_identity(blocked, blocked["features"][0])
        feature = blocked["features"][0]
        identity_document = {
            "queue_run_id": blocked["queue_run_id"],
            "feature_run_id": feature["feature_run_id"],
            "feature_index": feature["index"],
            "base_branch": blocked["base_branch"],
        }
        checkpoint_path = root / feature["checkpoint_path"]
        transaction_path = root / feature["transaction_path"]
        write_json(checkpoint_path, {**identity_document, "phase": "PLAN_REVIEW"})
        write_json(transaction_path, {**identity_document, "state": "prepared"})
        evidence = {
            "identity": identity,
            "record": "decision-1",
            "artifacts": {
                "checkpoint_sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
                "transaction_sha256": hashlib.sha256(transaction_path.read_bytes()).hexdigest(),
            },
        }
        with self.assertRaises(serial_state.AuthorizationError):
            serial_state.resume_blocked_feature(
                blocked,
                index=0,
                token=token,
                resolution_evidence={**evidence, "identity": {**identity, "base_branch": "wrong"}},
                coordinator_id="coordinator-2",
                lease_id="lease-2",
                base_root=root,
            )
        with self.assertRaisesRegex(serial_state.AuthorizationError, "checkpoint identity hash"):
            serial_state.resume_blocked_feature(
                blocked,
                index=0,
                token=token,
                resolution_evidence={
                    **evidence,
                    "artifacts": {**evidence["artifacts"], "checkpoint_sha256": "0" * 64},
                },
                coordinator_id="coordinator-2",
                lease_id="lease-2",
                base_root=root,
            )
        later = copy.deepcopy(blocked)
        later["features"].insert(0, {"index": "earlier", "description": "earlier", "status": "pending"})
        with self.assertRaisesRegex(serial_state.SerialStateError, "first unfinished"):
            serial_state.resume_blocked_feature(
                later,
                index=0,
                token=token,
                resolution_evidence=evidence,
                coordinator_id="coordinator-2",
                lease_id="lease-2",
                base_root=root,
            )
        another_active = copy.deepcopy(blocked)
        another_active["features"].append(
            {"index": 1, "description": "active", "status": "in_progress", "engine": "v13-codex"}
        )
        with self.assertRaisesRegex(serial_state.SerialStateError, "another feature"):
            serial_state.resume_blocked_feature(
                another_active,
                index=0,
                token=token,
                resolution_evidence=evidence,
                coordinator_id="coordinator-2",
                lease_id="lease-2",
                base_root=root,
            )
        resumed = serial_state.resume_blocked_feature(
            blocked,
            index=0,
            token=token,
            resolution_evidence=evidence,
            coordinator_id="coordinator-2",
            lease_id="lease-2",
            base_root=root,
            now="2026-07-19T04:00:00Z",
        )
        feature = resumed["features"][0]
        self.assertEqual(feature["feature_run_id"], "fr_fixed")
        self.assertEqual(feature["dispatch_lease"]["coordinator_id"], "coordinator-2")
        self.assertEqual(feature["blocked_history"][0]["block_reason"], "choice required")
        _, payload = serial_state.prepare_dispatch(
            resumed,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator-2",
            lease_id="lease-2",
        )
        self.assertEqual(payload["dispatch_action"], "launch")

    def test_blocked_resume_allows_identity_bound_post_migration_runtime_advance(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        journal_path = root / "migration.json"
        write_json(journal_path, {"state": "committed"})
        feature = {
            "status": "blocked",
            "controller_package_digest": "a" * 64,
            "controller_migration_id": "migration-1",
        }
        evidence = {
            "migration": {
                "journal_path": str(journal_path),
                "controller_package_digest": "a" * 64,
                "migration_receipt_sha256": "b" * 64,
                "migration_id": "migration-1",
            }
        }
        committed = {
            "migration_id": "migration-1",
            "new_package_digest": "a" * 64,
        }

        with patch.object(serial_state, "_controller_module") as controller_factory:
            controller_factory.return_value.validate_committed_migration.return_value = committed
            migration, journal = serial_state._migration_evidence(root, feature, evidence)

        self.assertEqual(migration, evidence["migration"])
        self.assertEqual(journal, committed)
        controller_factory.return_value.validate_committed_migration.assert_called_once_with(
            journal_path,
            expected_package_digest="a" * 64,
            expected_receipt_sha256="b" * 64,
            allow_queue_advance=True,
        )

    def test_queue_identity_fields_are_immutable(self) -> None:
        initialized, _ = prepared_queue()
        initialized["queue_identity"]["unknown_identity_field"] = "preserve"
        _, payload = serial_state.prepare_dispatch(
            initialized,
            base_worktree_path=BASE_WORKTREE_PATH,
            coordinator_id="coordinator-1",
            lease_id="lease_fixed",
        )
        self.assertEqual(payload["dispatch_action"], "reattach")
        for field, value in (
            ("base_branch", "other"),
            ("protocol_version", "2.0"),
            ("dispatcher", "other-dispatcher"),
            ("queue_run_id", "other-run"),
        ):
            changed = copy.deepcopy(initialized)
            changed[field] = value
            with self.assertRaises(serial_state.SerialStateError):
                serial_state.validate_queue(changed)


class PersistenceTests(unittest.TestCase):
    def test_wait_emits_snapshot_then_tiny_timeout_packet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue, _ = prepared_queue()
            queue_path = root / "queue.json"
            write_json(queue_path, queue)
            first = serial_state.wait_for_queue_change(
                queue_path, since=None, timeout_seconds=0
            )
            self.assertEqual(first["status"], "snapshot")
            clock_values = iter((0.0, 0.0, 0.0, 1.0))
            timed_out = serial_state.wait_for_queue_change(
                queue_path,
                since=first["fingerprint"],
                timeout_seconds=1,
                monotonic=lambda: next(clock_values),
                sleeper=lambda _seconds: None,
            )
            self.assertEqual(timed_out["status"], "timeout")
            self.assertNotIn("observed", timed_out)
            self.assertEqual(timed_out["feature_run_id"], "fr_fixed")

    def test_wait_reports_checkpoint_change_and_terminal_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue, _ = prepared_queue()
            queue_path = root / "queue.json"
            write_json(queue_path, queue)
            baseline = serial_state.wait_for_queue_change(queue_path, since=None, timeout_seconds=0)
            feature = queue["features"][0]
            checkpoint = root / feature["checkpoint_path"]
            write_json(checkpoint, {"phase": "IMPLEMENTING", "phase_state": "running", "state_revision": 3})
            changed = serial_state.wait_for_queue_change(
                queue_path, since=baseline["fingerprint"], timeout_seconds=0
            )
            self.assertEqual(changed["status"], "changed")
            self.assertEqual(changed["observed"]["feature"]["checkpoint"]["phase"], "IMPLEMENTING")
            transaction = root / feature["transaction_path"]
            write_json(transaction, {"protocol": "feature-transaction/1", "state": "feature_result_written", "state_revision": 9})
            terminal = serial_state.wait_for_queue_change(
                queue_path, since=changed["fingerprint"], timeout_seconds=0
            )
            self.assertEqual(terminal["status"], "terminal")
            self.assertTrue(terminal["terminal"])

    def test_atomic_mutate_enforces_cas_and_preserves_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            write_json(path, queue_with({"index": 0, "description": "one", "status": "pending"}))
            path.chmod(0o640)
            updated = serial_state.atomic_mutate(
                path, lambda value: {**value, "marker": True}, expected_revision=0
            )
            self.assertEqual(updated["state_revision"], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            with self.assertRaises(serial_state.ConcurrentUpdateError):
                serial_state.atomic_mutate(path, lambda value: value, expected_revision=0)

    def test_ack_matches_feature_contract_checks_hashes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path, transaction_path, result_path, _, _, _ = terminal_fixture(Path(directory))
            first_queue, first_transaction = serial_state.acknowledge_feature(
                queue_path, transaction_path, result_path, now="2026-07-19T05:00:00Z"
            )
            self.assertEqual(first_queue["features"][0]["status"], "done")
            self.assertEqual(first_queue["features"][0]["dispatch_lease"]["state"], "complete")
            self.assertEqual(first_queue["features"][0]["feature_unknown"], 9)
            self.assertEqual(len(first_queue["results"]), 1)
            self.assertEqual(first_transaction["state"], "dispatcher_ack")
            self.assertEqual(first_transaction["transaction_unknown"], "keep")
            self.assertEqual(first_transaction["history"][-1]["state"], "dispatcher_ack")
            second_queue, second_transaction = serial_state.acknowledge_feature(
                queue_path, transaction_path, result_path
            )
            self.assertEqual(len(second_queue["results"]), 1)
            self.assertEqual(second_queue["state_revision"], first_queue["state_revision"])
            self.assertEqual(second_transaction["state_revision"], first_transaction["state_revision"])

    def test_ack_recovers_queue_written_before_transaction_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path, transaction_path, result_path, queue, _, result = terminal_fixture(Path(directory))
            queue["features"][0]["status"] = "done"
            queue["features"][0]["dispatch_lease"]["state"] = "complete"
            queue["features"][0]["dispatch_lease"]["completed_at"] = result["completed_at"]
            queue["results"] = [copy.deepcopy(result)]
            write_json(queue_path, queue)
            updated_queue, updated_transaction = serial_state.acknowledge_feature(
                queue_path, transaction_path, result_path
            )
            self.assertEqual(updated_queue["state_revision"], 0)
            self.assertEqual(updated_transaction["state"], "dispatcher_ack")

    def test_ack_rejects_hash_mismatch_and_wrong_artifact_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path, transaction_path, result_path, _, transaction, result = terminal_fixture(Path(directory))
            transaction["manifest_sha256"] = "0" * 64
            write_json(transaction_path, transaction)
            with self.assertRaisesRegex(serial_state.SerialStateError, "manifest hash"):
                serial_state.acknowledge_feature(queue_path, transaction_path, result_path)
            transaction["manifest_sha256"] = hashlib.sha256(
                (Path(directory) / result["manifest"]).read_bytes()
            ).hexdigest()
            write_json(transaction_path, transaction)
            result["merge_receipt"] = "handoff/wrong.json"
            write_json(result_path, result)
            with self.assertRaisesRegex(serial_state.SerialStateError, "merge_receipt"):
                serial_state.acknowledge_feature(queue_path, transaction_path, result_path)

    def test_ack_rejects_mismatched_identity_and_paused_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path, transaction_path, result_path, queue, transaction, result = terminal_fixture(Path(directory))
            transaction["feature_run_id"] = "other"
            write_json(transaction_path, transaction)
            with self.assertRaisesRegex(serial_state.SerialStateError, "feature_run_id mismatch"):
                serial_state.acknowledge_feature(queue_path, transaction_path, result_path)
            transaction["feature_run_id"] = result["feature_run_id"]
            write_json(transaction_path, transaction)
            queue["paused"] = True
            queue["pause_reason"] = "stop"
            write_json(queue_path, queue)
            before = queue_path.read_bytes()
            with self.assertRaises(serial_state.QueuePausedError):
                serial_state.acknowledge_feature(queue_path, transaction_path, result_path)
            self.assertEqual(queue_path.read_bytes(), before)

    def test_ack_rejects_debug_phase_flow_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path, transaction_path, result_path, _, transaction, result = terminal_fixture(Path(directory))
            result.update(
                protocol="codex-phase-flow/debug-result/1",
                status="done",
                certification_scope="orchestration_only",
            )
            write_json(result_path, result)
            transaction["feature_result_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
            write_json(transaction_path, transaction)
            with self.assertRaisesRegex(serial_state.SerialStateError, "feature result protocol"):
                serial_state.acknowledge_feature(queue_path, transaction_path, result_path)


if __name__ == "__main__":
    unittest.main()
