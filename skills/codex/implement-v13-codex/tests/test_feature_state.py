from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from feature_state import (  # noqa: E402
    StateError,
    advance_transaction,
    block_checkpoint,
    build_inputs,
    initialize_checkpoint,
    initialize_transaction,
    invalidate_certification,
    resume_blocked_checkpoint,
    transition,
    validate_reconciliation,
    write_feature_result,
)
from state_io import atomic_write_json, read_json, sha256_file  # noqa: E402


class FeatureStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _checkpoint_payload(self) -> dict[str, object]:
        return {
            "task": "synthetic feature",
            "base_branch": "feature/base",
            "worktree_name": "impl-test",
            "branch": "codex/impl-test",
            "feature_index": "Q1",
            "queue_run_id": "qr_test",
            "feature_run_id": "fr_test",
        }

    def test_checkpoint_cas_preserves_unknown_fields(self) -> None:
        path = self.root / "checkpoint.json"
        current = initialize_checkpoint(path, self._checkpoint_payload())
        current["foreign_field"] = {"keep": True}
        atomic_write_json(path, current)
        updated = transition(path, 0, "PLANNING", "planner_prepare", "running")
        self.assertEqual(updated["foreign_field"], {"keep": True})
        self.assertEqual(updated["state_revision"], 1)
        with self.assertRaises(StateError):
            transition(path, 0, "PLANNING", "planner_prepare", "validating")

    def test_transition_rejects_skips(self) -> None:
        path = self.root / "checkpoint.json"
        initialize_checkpoint(path, self._checkpoint_payload())
        with self.assertRaises(StateError):
            transition(path, 0, "PLAN_REVIEW", "review_dispatch", "ready")
        with self.assertRaises(StateError):
            transition(path, 0, "PLANNING", "planner_run", "ready")

    def test_plan_review_findings_cannot_block_before_revision(self) -> None:
        path = self.root / "checkpoint.json"
        checkpoint = initialize_checkpoint(path, self._checkpoint_payload())
        checkpoint.update(
            phase="PLAN_REVIEW",
            phase_detail="review_dispatch",
            phase_state="running",
        )
        atomic_write_json(path, checkpoint)
        with self.assertRaisesRegex(
            StateError, "must advance through review_collect, revise"
        ):
            transition(
                path,
                checkpoint["state_revision"],
                "PLAN_REVIEW",
                "review_dispatch",
                "blocked",
            )

        failed = self.root / "failed-review.json"
        atomic_write_json(
            failed,
            {
                "status": "failed",
                "phase": "PLAN_REVIEW",
                "phase_detail": "review_dispatch",
            },
        )
        blocked = transition(
            path,
            checkpoint["state_revision"],
            "PLAN_REVIEW",
            "review_dispatch",
            "blocked",
            failed,
        )
        self.assertEqual(blocked["phase_state"], "blocked")

        resumed = resume_blocked_checkpoint(path, blocked["state_revision"], {
            "authorization_sha256": "a" * 64,
            "authorized_at": "2026-07-22T00:00:00Z",
            "resolution_evidence": {"decision": "operator authorized option 2"},
        })
        self.assertEqual(resumed["phase_state"], "ready")
        self.assertEqual(resumed["phase_detail"], "review_dispatch")
        self.assertEqual(
            resumed["resolution_evidence"][-1]["authorization_sha256"], "a" * 64
        )

    def test_completion_requires_receipt_before_advancing(self) -> None:
        path = self.root / "checkpoint.json"
        current = initialize_checkpoint(path, self._checkpoint_payload())
        current = transition(path, current["state_revision"], "PLANNING", "planner_prepare", "running")
        current = transition(path, current["state_revision"], "PLANNING", "planner_prepare", "validating")
        with self.assertRaises(StateError):
            transition(path, current["state_revision"], "PLANNING", "planner_prepare", "complete")
        receipt = self.root / "receipt.json"
        atomic_write_json(receipt, {"status": "succeeded", "phase": "PLANNING", "phase_detail": "planner_prepare"})
        current = transition(path, current["state_revision"], "PLANNING", "planner_prepare", "complete", receipt)
        current = transition(path, current["state_revision"], "PLANNING", "planner_run", "ready")
        self.assertEqual(current["phase_detail"], "planner_run")

    def test_coordinator_block_is_one_atomic_checkpoint_transition(self) -> None:
        path = self.root / "checkpoint.json"
        current = initialize_checkpoint(path, self._checkpoint_payload())
        blocked = block_checkpoint(path, current["state_revision"], {
            "blocker_class": "architectural_design_contradiction",
            "reason": "two immutable contracts conflict",
            "resume_condition": "operator must reconcile the contracts",
        })
        self.assertEqual(blocked["phase_state"], "blocked")
        self.assertEqual(blocked["state_revision"], 1)
        self.assertEqual(
            blocked["active_blocker"]["reason"],
            "two immutable contracts conflict",
        )
        self.assertEqual(blocked["blocked_history"], [blocked["active_blocker"]])
        with self.assertRaisesRegex(StateError, "active checkpoint detail"):
            block_checkpoint(path, blocked["state_revision"], {
                "blocker_class": "duplicate",
                "reason": "duplicate",
                "resume_condition": "none",
            })

    def test_planning_inputs_are_allowlisted_hashed_and_no_arbitrary_docs(self) -> None:
        worktree = self.root / "worktree"
        (worktree / "docs/development").mkdir(parents=True)
        (worktree / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        (worktree / "CLAUDE.md").write_text("# foreign rules\n", encoding="utf-8")
        (worktree / "docs/development/NEXT_STEPS.md").write_text("# next\n", encoding="utf-8")
        (worktree / "docs/development/INDEX.md").write_text("# index\n", encoding="utf-8")
        (worktree / "docs/development/declared.md").write_text("# declared\n", encoding="utf-8")
        (worktree / "docs/development/not-declared.md").write_text("# invisible\n", encoding="utf-8")
        declared = self.root / "declared.json"
        declared.write_text(
            json.dumps([
                {
                    "id": "custom",
                    "path": "docs/development/declared.md",
                    "role": "seed_plan",
                    "required": True,
                    "revision": "latest_on_base",
                    "update_policy": "reconcile_if_affected",
                }
            ]),
            encoding="utf-8",
        )
        output = self.root / "artifacts/inputs.json"
        manifest = build_inputs(worktree, self.root / "artifacts", declared, output)
        paths = {Path(item["resolved_path"]).name for item in manifest["inputs"]}
        self.assertIn("declared.md", paths)
        self.assertIn("AGENTS.md", paths)
        self.assertNotIn("CLAUDE.md", paths)
        self.assertIn("NEXT_STEPS.md", paths)
        self.assertIn("INDEX.md", paths)
        self.assertNotIn("not-declared.md", paths)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["inputs"]))

    def test_empty_declared_inputs_use_next_steps_as_seed_plan(self) -> None:
        worktree = self.root / "worktree"
        (worktree / "docs/development").mkdir(parents=True)
        (worktree / "AGENTS.md").write_text("rules", encoding="utf-8")
        (worktree / "CLAUDE.md").write_text("foreign rules", encoding="utf-8")
        (worktree / "docs/development/NEXT_STEPS.md").write_text("Q12 plan", encoding="utf-8")
        (worktree / "docs/development/INDEX.md").write_text("plans", encoding="utf-8")
        manifest = build_inputs(
            worktree,
            self.root / "artifacts",
            None,
            self.root / "planning-inputs.json",
        )
        by_id = {item["id"]: item for item in manifest["inputs"]}
        self.assertEqual(set(by_id), {"agents", "next_steps", "development_index"})
        self.assertEqual(by_id["next_steps"]["role"], "seed_plan")

    def test_agents_is_required_even_when_missing(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        with self.assertRaisesRegex(StateError, "AGENTS.md"):
            build_inputs(worktree, self.root / "artifacts", None, self.root / "inputs.json")

    def test_relevant_module_context_is_ingested_and_reconciled(self) -> None:
        worktree = self.root / "worktree"
        (worktree / "retinology/web").mkdir(parents=True)
        (worktree / "docs/development").mkdir(parents=True)
        (worktree / "AGENTS.md").write_text("rules", encoding="utf-8")
        (worktree / "docs/development/NEXT_STEPS.md").write_text("next", encoding="utf-8")
        (worktree / "docs/development/INDEX.md").write_text("index", encoding="utf-8")
        (worktree / "retinology/web/context.md").write_text("context", encoding="utf-8")
        paths = self.root / "paths.json"
        paths.write_text(json.dumps(["retinology/web/routes.py"]), encoding="utf-8")
        output = self.root / "inputs.json"
        manifest = build_inputs(worktree, self.root / "artifacts", None, output, paths)
        context = next(item for item in manifest["inputs"] if item["id"] == "module_context_web")
        for relative in ("AGENTS.md", "docs/development/NEXT_STEPS.md", "docs/development/INDEX.md"):
            target = worktree / relative
            target.write_text(target.read_text(encoding="utf-8") + "\nupdated", encoding="utf-8")
        reconciliation = self.root / "reconciliation.json"
        entries = []
        for item in manifest["inputs"]:
            if item.get("update_policy") == "reconcile_if_affected" or item["id"] == "next_steps":
                required_update = item["id"] in {"agents", "next_steps", "development_index"}
                entries.append({
                    "input_id": item["id"],
                    "path": item["path"],
                    "input_sha256": item["sha256"],
                    "disposition": "updated" if required_update else "verified_current",
                    "evidence": "checked",
                    **({"output_sha256": sha256_file(worktree / item["path"])} if required_update else {}),
                })
        atomic_write_json(reconciliation, {"protocol": "implement-v13-codex/context-reconciliation/1", "entries": entries})
        validate_reconciliation(output, reconciliation, worktree, paths)
        self.assertEqual(context["path"], "retinology/web/context.md")

    def test_external_input_requires_snapshot_authorization(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        external = self.root / "external.md"
        external.write_text("external", encoding="utf-8")
        declared = self.root / "declared.json"
        declared.write_text(json.dumps([{"id": "x", "path": str(external), "role": "background", "revision": "latest_on_base"}]), encoding="utf-8")
        with self.assertRaises(StateError):
            build_inputs(worktree, self.root / "artifacts", declared, self.root / "inputs.json")

    def test_snapshot_input_id_cannot_escape_artifact_root(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        external = self.root / "external.md"
        external.write_text("external", encoding="utf-8")
        declared = self.root / "declared.json"
        declared.write_text(json.dumps([{"id": "../../escape", "path": str(external), "role": "background", "revision": "snapshot", "allow_external_snapshot": True}]), encoding="utf-8")
        with self.assertRaisesRegex(StateError, "path-safe"):
            build_inputs(worktree, self.root / "artifacts", declared, self.root / "inputs.json")

    def test_reconciliation_requires_next_steps(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / "NEXT").write_text("updated", encoding="utf-8")
        inputs = self.root / "inputs.json"
        atomic_write_json(inputs, {"inputs": [{"id": "next_steps", "path": "NEXT", "sha256": "a" * 64, "update_policy": "reconcile_if_affected"}]})
        reconciliation = self.root / "reconcile.json"
        atomic_write_json(reconciliation, {"protocol": "implement-v13-codex/context-reconciliation/1", "entries": []})
        with self.assertRaises(StateError):
            validate_reconciliation(inputs, reconciliation, worktree)
        atomic_write_json(
            reconciliation,
            {
                "protocol": "implement-v13-codex/context-reconciliation/1",
                "entries": [{"input_id": "next_steps", "path": "NEXT", "input_sha256": "a" * 64, "disposition": "updated", "evidence": "priority text updated", "output_sha256": sha256_file(worktree / "NEXT")}],
            },
        )
        self.assertEqual(validate_reconciliation(inputs, reconciliation, worktree)["entries"][0]["disposition"], "updated")

    def test_terminal_transaction_and_immutable_result(self) -> None:
        checkpoint_path = self.root / "checkpoint.json"
        checkpoint = initialize_checkpoint(checkpoint_path, self._checkpoint_payload())
        transaction_path = self.root / "transaction.json"
        transaction = initialize_transaction(transaction_path, checkpoint)
        for target in ("feature_committed", "manifest_committed", "merge_prepared", "merged", "cleanup_complete"):
            transaction = advance_transaction(transaction_path, transaction["state_revision"], target, {"proof": target})
        with self.assertRaisesRegex(StateError, "reserved"):
            advance_transaction(transaction_path, transaction["state_revision"], "feature_result_written", {"state": "dispatcher_ack"})
        result_payload = {
            "manifest": "docs/development/runs/test.md",
            "merge_receipt": "artifacts/merge.json",
            "clearance_report": "artifacts/clearance.md",
            "base_head": "abc",
            "cleanup_proof": "clean",
        }
        result_path = self.root / "feature-result.json"
        result = write_feature_result(transaction_path, result_path, result_payload)
        self.assertEqual(result["status"], "done")
        self.assertEqual(read_json(result_path), result)
        self.assertEqual(write_feature_result(transaction_path, result_path, result_payload), result)
        with self.assertRaisesRegex(StateError, "reserved"):
            write_feature_result(transaction_path, self.root / "forged.json", {**result_payload, "status": "done"})
        with self.assertRaises(StateError):
            write_feature_result(transaction_path, result_path, {**result_payload, "base_head": "different"})

    def test_post_review_fix_can_invalidate_certification(self) -> None:
        path = self.root / "checkpoint.json"
        current = initialize_checkpoint(path, self._checkpoint_payload())
        current.update(phase="COMMITTING", phase_detail="smoke_b_fix", phase_state="complete", state_revision=5)
        atomic_write_json(path, current)
        evidence = self.root / "fix.json"
        atomic_write_json(evidence, {"status": "passed"})
        rewound = invalidate_certification(path, 5, evidence)
        self.assertEqual((rewound["phase"], rewound["phase_detail"]), ("REVIEWING", "review_dispatch"))
        self.assertEqual(rewound["certification_cycle"], 1)


if __name__ == "__main__":
    unittest.main()
