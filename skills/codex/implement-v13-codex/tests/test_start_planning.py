from __future__ import annotations

import json
import io
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from start_planning import (  # noqa: E402
    MAX_PLANNER_CONTEXT_BYTES,
    PLANNER_PROCESS_LEAK_SAFETY_CEILING_SECONDS,
    start,
    start_and_drive,
)
from state_io import StateError, atomic_write_json, read_json  # noqa: E402


class StartPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "base"
        self.base.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.base, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.base, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.base, check=True)
        (self.base / "docs/development").mkdir(parents=True)
        (self.base / "AGENTS.md").write_text("Codex rules\n", encoding="utf-8")
        (self.base / "CLAUDE.md").write_text("Foreign rules\n", encoding="utf-8")
        (self.base / "docs/development/NEXT_STEPS.md").write_text("Q12 plan\n", encoding="utf-8")
        (self.base / "docs/development/INDEX.md").write_text("Development plans\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.base, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.base, check=True, capture_output=True)

        plan = {
            "protocol": "implement-v13-codex/1",
            "task": "Implement Q12",
            "scope": {"in": ["Q12"], "out": []},
            "governing_contracts": ["AGENTS.md"],
            "source_evidence": ["docs/development/NEXT_STEPS.md"],
            "input_acknowledgements": [],
            "complexity": "low",
            "ui_impact": False,
            "runtime_contracts": [],
            "steps": [
                {
                    "id": "q12",
                    "title": "Implement Q12",
                    "effort": 1,
                    "dependencies": [],
                    "write_paths": ["q12.py"],
                    "targeted_tests": ["test q12"],
                }
            ],
            "task_dag": {"nodes": ["q12"], "edges": []},
            "critical_path_effort": 1,
            "total_effort": 1,
            "critical_path_share": 1,
            "parallelization": {"recommended": False, "worker_groups": [], "shared_file_owner": None},
            "testing_strategy": ["targeted"],
            "risks": [],
            "review_lenses": [
                {
                    "id": "l1_l2_contract_boundary",
                    "charge": "Challenge layer ownership, dependency direction, and contract boundaries.",
                    "reason": "contract",
                    "must_read": ["AGENTS.md"],
                },
                {
                    "id": "security_privacy_destructive_behavior",
                    "charge": "Challenge trust boundaries, sensitive-data handling, and destructive operations.",
                    "reason": "safety",
                    "must_read": ["AGENTS.md"],
                },
                {
                    "id": "correctness",
                    "charge": "Challenge state invariants, edge cases, and failure handling.",
                    "reason": "correctness",
                    "must_read": ["docs/development/NEXT_STEPS.md"],
                },
            ],
        }
        self.fake_codex = self.root / "codex"
        self.fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "if '--version' in sys.argv:\n"
            " print('codex-cli 0.test')\n"
            " raise SystemExit(0)\n"
            f"plan={plan!r}\n"
            "args=sys.argv[1:]\n"
            "pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(plan))\n"
            "print(json.dumps({'type':'thread.started','thread_id':'planner-test'}), flush=True)\n"
            "print(json.dumps({'type':'turn.completed'}), flush=True)\n",
            encoding="utf-8",
        )
        self.fake_codex.chmod(self.fake_codex.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.base / ".worktrees/q12")],
            cwd=self.base,
            check=False,
            capture_output=True,
        )
        self.temp.cleanup()

    def _dispatch(
        self,
        *,
        description: str = "Implement Q12",
        planning_inputs: list[dict[str, object]] | None = None,
    ) -> Path:
        path = self.root / "dispatch.json"
        atomic_write_json(
            path,
            {
                "queue_run_id": "qr_test",
                "feature_run_id": "fr_q12",
                "feature_index": 11,
                "description": description,
                "base_branch": "main",
                "base_worktree_path": str(self.base.resolve()),
                "dispatch_action": "launch",
                "branch": "codex/q12",
                "worktree_name": "q12",
                "worktree_path": ".worktrees/q12",
                "artifact_dir": "artifacts/q12",
                "checkpoint_path": ".worktrees/q12/docs/development/current_implementation_checkpoint.json",
                "transaction_path": "artifacts/q12/feature-transaction.v1.json",
                "planning_inputs": planning_inputs or [],
                "run_directives": [],
            },
        )
        return path

    def test_fresh_dispatch_launches_codex_planner_within_one_minute(self) -> None:
        before = time.monotonic()
        with patch("run_exec.shutil.which", return_value=str(self.fake_codex)):
            result = start(self._dispatch())
        self.assertLess(time.monotonic() - before, 60)
        self.assertLessEqual(result["planner_launched_after_seconds"], 60)
        self.assertTrue((self.base / ".worktrees/q12").is_dir())
        manifest = read_json(self.base / "artifacts/q12/planning-inputs.v1.json")
        self.assertEqual(
            {item["id"] for item in manifest["inputs"]},
            {"agents", "next_steps", "development_index"},
        )
        receipt = read_json(
            self.base / "artifacts/q12/fr_q12-PLANNING-planner_run-planner-0-1.receipt.json"
        )
        capability = read_json(
            self.base / "artifacts/q12/capability-manifest.v2.json"
        )
        self.assertEqual(capability["status"], "ready")
        self.assertFalse(capability["simulation_only"])
        self.assertTrue(all(item["production_real"] for item in capability["probes"]))
        self.assertEqual(
            receipt["capability_manifest_sha256"],
            result["capability_manifest_sha256"],
        )
        spec = read_json(self.base / "artifacts/q12/planner-run.spec.json")
        self.assertEqual(spec["reasoning"], "medium")
        self.assertEqual(
            spec["wall_timeout_seconds"],
            PLANNER_PROCESS_LEAK_SAFETY_CEILING_SECONDS,
        )
        self.assertEqual(Path(receipt["argv"][0]), self.fake_codex.resolve())
        self.assertNotIn("claude", [Path(value).name for value in receipt["argv"]])
        prompt = (self.base / "artifacts/q12/planner-run.prompt.md").read_text(encoding="utf-8")
        self.assertTrue(
            prompt.startswith(
                "You are the read-only repository implementation planner for this feature."
            )
        )
        self.assertNotIn("planner for implement-v13-codex", prompt)
        self.assertIn("Do not load installed skills", prompt)
        self.assertIn("EXECUTION_ENVIRONMENT=macOS BSD userland; shell=zsh", prompt)
        self.assertIn("never assign zsh's read-only status", prompt)
        self.assertIn("GNU find -printf is unavailable", prompt)
        self.assertIn("optional rg discovery separately", prompt)
        self.assertIn(
            "l1_l2_contract_boundary: Challenge layer ownership, dependency direction, and contract boundaries.",
            prompt,
        )
        self.assertIn(
            "security_privacy_destructive_behavior: Challenge trust boundaries, sensitive-data handling, and destructive operations.",
            prompt,
        )
        self.assertIn(
            "correctness: Challenge state invariants, edge cases, and failure handling.",
            prompt,
        )
        self.assertIn(
            "parallelization.recommended to false whenever critical_path_share is greater than 0.60",
            prompt,
        )
        self.assertNotIn("Foreign rules", prompt)
        self.assertEqual(prompt.count("Codex rules"), 1)
        self.assertEqual(prompt.count("Q12 plan"), 1)
        self.assertEqual(prompt.count("Development plans"), 1)
        schema = read_json(self.base / "artifacts/q12/planner-run.schema.json")
        self.assertEqual(schema["properties"]["task"], {"type": "string", "const": "Implement Q12"})
        checkpoint = read_json(
            self.base / ".worktrees/q12/docs/development/current_implementation_checkpoint.json"
        )
        self.assertEqual(
            (checkpoint["phase"], checkpoint["phase_detail"], checkpoint["phase_state"]),
            ("PLANNING", "plan_validate", "ready"),
        )
        self.assertEqual(
            result["planner_context_bytes"],
            len(b"Codex rules\n") + len(b"Q12 plan\n") + len(b"Development plans\n"),
        )
        self.assertEqual(
            result["planner_context_input_ids"],
            ["agents", "next_steps", "development_index"],
        )

    def test_foreground_handoff_reports_durable_phase_not_process_identity(self) -> None:
        dispatch_path = self._dispatch()
        artifact_dir = self.base / "artifacts/q12"
        checkpoint_path = self.base / ".worktrees/q12/docs/development/current_implementation_checkpoint.json"
        artifact_dir.mkdir(parents=True)
        checkpoint_path.parent.mkdir(parents=True)
        dispatch = read_json(dispatch_path)
        atomic_write_json(artifact_dir / "dispatch.v1.json", dispatch)
        atomic_write_json(checkpoint_path, {
            "phase": "PLAN_REVIEW", "phase_detail": "review_dispatch",
            "phase_state": "ready", "state_revision": 7,
        })
        output = io.StringIO()
        with patch("start_planning.start", return_value={"status": "succeeded"}), redirect_stderr(output):
            result = start_and_drive(dispatch_path, drive_fn=lambda _path: {"status": "blocked"})
        event = json.loads(output.getvalue().strip())
        self.assertEqual(event["type"], "controller.phase")
        self.assertEqual((event["phase"], event["phase_detail"]), ("PLAN_REVIEW", "review_dispatch"))
        self.assertEqual(event["phase_authority"], "durable_checkpoint")
        self.assertTrue(event["process_liveness_only"])
        self.assertEqual(result["feature"]["status"], "blocked")

    def test_missed_start_target_is_logged_and_does_not_block(self) -> None:
        with patch("run_exec.shutil.which", return_value=str(self.fake_codex)), patch(
            "start_planning._planner_launch_elapsed", return_value=75.0
        ):
            result = start(self._dispatch())
        benchmark = read_json(
            self.base / "artifacts/q12/planner-start-benchmark.v1.json"
        )
        self.assertEqual(benchmark["observed_seconds"], 75.0)
        self.assertFalse(benchmark["met"])
        self.assertEqual(benchmark["action"], "continue")
        self.assertEqual(result["planner_start_benchmark"], benchmark)
        checkpoint = read_json(
            self.base / ".worktrees/q12/docs/development/current_implementation_checkpoint.json"
        )
        self.assertEqual(
            (checkpoint["phase"], checkpoint["phase_detail"], checkpoint["phase_state"]),
            ("PLANNING", "plan_validate", "ready"),
        )

    def test_declared_input_is_embedded_once_and_counted(self) -> None:
        (self.base / "q12-acceptance.md").write_text("UNIQUE-Q12-ACCEPTANCE\n", encoding="utf-8")
        subprocess.run(["git", "add", "q12-acceptance.md"], cwd=self.base, check=True)
        subprocess.run(["git", "commit", "-m", "acceptance"], cwd=self.base, check=True, capture_output=True)
        with patch("run_exec.shutil.which", return_value=str(self.fake_codex)):
            result = start(
                self._dispatch(
                    planning_inputs=[
                        {
                            "id": "q12_acceptance",
                            "path": "q12-acceptance.md",
                            "role": "acceptance",
                            "revision": "latest_on_base",
                        }
                    ]
                )
            )
        prompt = (self.base / "artifacts/q12/planner-run.prompt.md").read_text(encoding="utf-8")
        self.assertEqual(prompt.count("UNIQUE-Q12-ACCEPTANCE"), 1)
        self.assertEqual(result["planner_context_input_ids"][-1], "q12_acceptance")

    def test_explicit_q12_context_packet_above_256k_is_supported(self) -> None:
        declared = []
        for module, size in (("intake", 18099), ("extraction", 19644), ("web", 116943)):
            relative = f"retinology/{module}/context.md"
            target = self.base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(module.encode("utf-8") + b"x" * (size - len(module)))
            declared.append(
                {
                    "id": f"module_context_{module}",
                    "path": relative,
                    "role": "governing",
                    "revision": "latest_on_base",
                    "update_policy": "reconcile_if_affected",
                }
            )
        (self.base / "docs/development/NEXT_STEPS.md").write_bytes(b"n" * 78225)
        (self.base / "docs/development/INDEX.md").write_bytes(b"i" * 20026)
        (self.base / "AGENTS.md").write_bytes(b"a" * 14628)
        subprocess.run(["git", "add", "."], cwd=self.base, check=True)
        subprocess.run(["git", "commit", "-m", "q12 contexts"], cwd=self.base, check=True, capture_output=True)

        with patch("run_exec.shutil.which", return_value=str(self.fake_codex)):
            result = start(self._dispatch(planning_inputs=declared))

        self.assertGreater(result["planner_context_bytes"], 256 * 1024)
        self.assertLess(result["planner_context_bytes"], MAX_PLANNER_CONTEXT_BYTES)
        self.assertEqual(
            result["planner_context_input_ids"][-3:],
            ["module_context_intake", "module_context_extraction", "module_context_web"],
        )

    def test_task_mismatch_blocks_checkpoint_with_terminal_receipt_evidence(self) -> None:
        with patch("run_exec.shutil.which", return_value=str(self.fake_codex)):
            with self.assertRaisesRegex(StateError, "task"):
                start(self._dispatch(description="Implement the complete Q12 feature"))
        receipt = read_json(
            self.base / "artifacts/q12/fr_q12-PLANNING-planner_run-planner-0-1.receipt.json"
        )
        self.assertEqual(receipt["status"], "failed")
        checkpoint = read_json(
            self.base / ".worktrees/q12/docs/development/current_implementation_checkpoint.json"
        )
        self.assertEqual(checkpoint["phase_state"], "blocked")
        self.assertEqual(checkpoint["active_blocker"]["blocker_class"], "planner_process_failure")
        failure = read_json(self.base / "artifacts/q12/planner-run.failure.json")
        self.assertEqual(failure["receipt_status"], "failed")
        self.assertTrue(failure["validation_errors"])

    def test_context_over_limit_blocks_before_child_launch(self) -> None:
        (self.base / "oversized.md").write_bytes(b"x" * (MAX_PLANNER_CONTEXT_BYTES + 1))
        subprocess.run(["git", "add", "oversized.md"], cwd=self.base, check=True)
        subprocess.run(["git", "commit", "-m", "oversized"], cwd=self.base, check=True, capture_output=True)
        with self.assertRaisesRegex(StateError, "planner context exceeds"):
            start(
                self._dispatch(
                    planning_inputs=[
                        {
                            "id": "oversized",
                            "path": "oversized.md",
                            "role": "background",
                            "revision": "latest_on_base",
                        }
                    ]
                )
            )
        checkpoint = read_json(
            self.base / ".worktrees/q12/docs/development/current_implementation_checkpoint.json"
        )
        self.assertEqual(checkpoint["phase_state"], "blocked")
        self.assertFalse(
            (self.base / "artifacts/q12/fr_q12-PLANNING-planner_run-planner-0-1.receipt.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
