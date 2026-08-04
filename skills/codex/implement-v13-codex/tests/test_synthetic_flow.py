"""Deterministic tests for the production synthetic phase coordinator."""

from __future__ import annotations

from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_synthetic_flow.py"
_SPEC = importlib.util.spec_from_file_location("run_synthetic_flow", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
flow = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(flow)

_SERIAL_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "serial-implement-codex"
    / "scripts"
    / "serial_state.py"
)
_SERIAL_SPEC = importlib.util.spec_from_file_location("serial_state_for_integration", _SERIAL_SCRIPT)
assert _SERIAL_SPEC is not None and _SERIAL_SPEC.loader is not None
serial_state = importlib.util.module_from_spec(_SERIAL_SPEC)
_SERIAL_SPEC.loader.exec_module(serial_state)


class SyntheticFlowTests(unittest.TestCase):
    def _dispatch(self) -> dict[str, object]:
        return {
            "protocol_version": "1.0",
            "queue_run_id": "qr_test",
            "feature_run_id": "fr_test",
            "feature_index": 2,
            "description": "certify the phase coordinator",
            "base_branch": "integration",
            "engine": "v13-codex",
            "runner": "implement-v13-codex",
            "dispatch_action": "launch",
            "coordinator_id": "coordinator-test",
            "lease_id": "lease-test",
            "decision_key": "Q3",
            "decision_record": "docs/development/decisions/2026-07-q3-decisions.md",
            "planning_inputs": [{"id": "seed", "path": "plans/seed.md"}],
            "run_directives": ["synthetic certification"],
            "branch": "codex/q3-fr-test",
            "worktree_name": "impl-codex-fr-test",
            "worktree_path": ".claude/worktrees/impl-codex-fr-test",
            "artifact_dir": "handoff/serial-runs/qr_test/fr_test",
            "artifact_root": "handoff/serial-runs/qr_test",
            "checkpoint": "docs/development/current_implementation_checkpoint.json",
            "checkpoint_path": ".claude/worktrees/impl-codex-fr-test/docs/development/current_implementation_checkpoint.json",
            "transaction_path": "handoff/serial-runs/qr_test/fr_test/feature-transaction.v1.json",
            "feature_result_path": "handoff/serial-runs/qr_test/fr_test/feature-result.v1.json",
            "merge_receipt": "handoff/serial-runs/qr_test/fr_test/merge-receipt.json",
            "cleanup_proof": "handoff/serial-runs/qr_test/fr_test/cleanup-proof.json",
            "clearance_report": "handoff/serial-reports/q3.md",
            "future_serial_field": {"preserve": True},
        }

    def _new_run(self, parent: Path) -> tuple[Path, dict[str, object], Path]:
        dispatch_path = parent / "input.json"
        dispatch_path.write_text(json.dumps(self._dispatch()), encoding="utf-8")
        executable = parent / "codex"
        executable.write_text("codex executable witness", encoding="utf-8")
        run_dir = parent / "run"
        args = Namespace(
            dispatch=str(dispatch_path),
            run_dir=str(run_dir),
            model="gpt-5.6-sol",
            effort="low",
        )
        created, state = flow._start(args, executable.resolve(), "codex-cli test")
        return created, state, executable.resolve()

    def _receipt(self, run_dir: Path, state: dict[str, object], ordinal: int) -> dict[str, object]:
        phase, detail = flow.PHASE_CATALOG[ordinal]
        nonce = f"{ordinal + 1:032x}"
        phase_dir = run_dir / "phases" / f"{ordinal:02d}-{detail}"
        marker_dir = phase_dir / "workspace" / "markers"
        marker_dir.mkdir(parents=True)
        identity = {
            "protocol": flow.PHASE_PROTOCOL,
            "queue_run_id": state["queue_run_id"],
            "feature_run_id": state["feature_run_id"],
            "dispatch_sha256": state["dispatch_sha256"],
            "ordinal": ordinal,
            "phase": phase,
            "phase_detail": detail,
            "role": f"synthetic_{detail}",
            "nonce": nonce,
            "statement": f"this was written by the {detail} agent",
        }
        final = phase_dir / "final.json"
        marker = marker_dir / "phase-marker.json"
        stdout = phase_dir / "stdout.jsonl"
        serialized = json.dumps(identity) + "\n"
        final.write_text(serialized, encoding="utf-8")
        marker.write_text(serialized, encoding="utf-8")
        thread_id = f"thread-{ordinal}"
        stdout.write_text(
            json.dumps({"type": "thread.started", "thread_id": thread_id})
            + "\n"
            + json.dumps({"type": "turn.completed"})
            + "\n",
            encoding="utf-8",
        )
        receipt = {
            "protocol": flow.RECEIPT_PROTOCOL,
            "status": "succeeded",
            "exit_code": 0,
            "ordinal": ordinal,
            "phase": phase,
            "phase_detail": detail,
            "nonce": nonce,
            "thread_id": thread_id,
            "final_path": str(final),
            "marker_path": str(marker),
            "stdout_path": str(stdout),
            "final_sha256": flow._sha_file(final),
            "marker_sha256": flow._sha_file(marker),
            "stdout_sha256": flow._sha_file(stdout),
        }
        flow._write_json(run_dir / "receipts" / f"{ordinal:02d}.json", receipt)
        return receipt

    def test_catalog_matches_production_32_detail_sequence(self) -> None:
        self.assertEqual(len(flow.PHASE_CATALOG), 32)
        self.assertEqual(flow.PHASE_CATALOG[0], ("PLANNING", "planner_prepare"))
        self.assertEqual(flow.PHASE_CATALOG[-1], ("COMMITTING", "cleanup"))
        self.assertEqual(len(set(flow.PHASE_CATALOG)), 32)

    def test_dispatch_accepts_serial_payload_and_preserves_extensions(self) -> None:
        flow._validate_dispatch(self._dispatch())
        missing = self._dispatch()
        del missing["planning_inputs"]
        with self.assertRaisesRegex(flow.SyntheticFlowError, "planning_inputs"):
            flow._validate_dispatch(missing)
        wrong_engine = self._dispatch()
        wrong_engine["engine"] = "other"
        with self.assertRaisesRegex(flow.SyntheticFlowError, "engine"):
            flow._validate_dispatch(wrong_engine)

    def test_accepts_actual_serial_state_dispatch_output_unchanged(self) -> None:
        queue = {
            "base_branch": "integration",
            "features": [
                {
                    "index": 0,
                    "description": "serial integration proof",
                    "status": "pending",
                    "engine": "solo",
                    "codex_engine": "v13-codex",
                }
            ],
            "results": [],
            "planning_inputs": [],
            "run_directives": ["do not mutate the repository"],
            "state_revision": 0,
        }
        identifiers = iter(("qr_actual", "fr_actual", "lease_actual"))
        _, payload = serial_state.prepare_dispatch(
            queue,
            base_worktree_path="/absolute/base-worktree",
            coordinator_id="coordinator-actual",
            now="2026-07-19T00:00:00Z",
            new_id=lambda _prefix: next(identifiers),
        )
        flow._validate_dispatch(payload)
        self.assertEqual(queue["features"][0]["engine"], "solo")
        self.assertEqual(payload["runner"], "implement-v13-codex")
        self.assertEqual(payload["dispatch_action"], "launch")

    def test_prompt_requires_agent_authored_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, state, _ = self._new_run(Path(directory))
            prompt = flow._prompt(state, 0, "a" * 32)
            self.assertIn("Use apply_patch", prompt)
            self.assertIn("must be authored by this Codex process", prompt)
            self.assertNotIn(str(Path.cwd()), prompt)
            self.assertTrue((run_dir / "dispatch.json").is_file())
            copied = flow._read_json(run_dir / "dispatch.json")
            self.assertEqual(copied["future_serial_field"], {"preserve": True})
            self.assertEqual(state["task"], "certify the phase coordinator")

    def test_terminal_receipt_requires_agent_marker_and_event_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, state, _ = self._new_run(Path(directory))
            receipt = self._receipt(run_dir, state, 0)
            self.assertEqual(flow._artifact_proof(run_dir, state, receipt), "thread-0")
            marker = Path(str(receipt["marker_path"]))
            marker.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(flow.SyntheticFlowError, "hash mismatch"):
                flow._artifact_proof(run_dir, state, receipt)

    def test_crash_window_reconciles_terminal_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, state, _ = self._new_run(Path(directory))
            self._receipt(run_dir, state, 0)
            flow._reconcile_next(run_dir, state)
            self.assertEqual(state["next_ordinal"], 1)
            self.assertEqual(state["thread_ids"], ["thread-0"])
            flow._reconcile_next(run_dir, state)
            self.assertEqual(state["next_ordinal"], 1)
            self.assertEqual(state["thread_ids"], ["thread-0"])

    def test_finish_and_verify_revalidate_all_32_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir, state, executable = self._new_run(Path(directory))
            for ordinal in range(32):
                receipt = self._receipt(run_dir, state, ordinal)
                state["thread_ids"].append(receipt["thread_id"])
            state["next_ordinal"] = 32
            flow._finish(run_dir, state)
            verified = flow._verify(
                Namespace(run_dir=str(run_dir)), executable, "codex-cli test"
            )
            self.assertEqual(verified, run_dir.resolve())
            result = flow._read_json(run_dir / "synthetic-feature-result.json")
            self.assertEqual(result["phase_details_validated"], 32)
            self.assertEqual(result["distinct_thread_ids"], 32)
            self.assertFalse(result["repository_mutated"])
            self.assertEqual(result["git_operations"], 0)

    def test_live_cli_has_no_executable_injection_or_git_operation(self) -> None:
        help_text = flow._parser().format_help()
        start_parser = flow._parser()._subparsers._group_actions[0].choices["start"]
        start_help = start_parser.format_help()
        effort = next(action for action in start_parser._actions if action.dest == "effort")
        self.assertEqual(effort.choices, ("low", "medium"))
        self.assertNotIn("codex-bin", help_text + start_help)
        self.assertNotIn("--git", help_text.lower())
        argv = flow._argv(
            Path("/opt/codex"), Path("/tmp/workspace"), Path("/tmp/final"),
            "gpt-5.6-sol", "low",
        )
        self.assertEqual(argv[:2], ["/opt/codex", "exec"])
        self.assertNotIn("resume", argv)
        self.assertIn("--skip-git-repo-check", argv)


if __name__ == "__main__":
    unittest.main()
