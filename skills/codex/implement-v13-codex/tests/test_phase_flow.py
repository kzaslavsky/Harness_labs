"""Contract tests for the JSON-derived empty-context phase runner."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_phase_flow.py"
_SPEC = importlib.util.spec_from_file_location("run_phase_flow", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
flow = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(flow)


class PhaseFlowTests(unittest.TestCase):
    def _flow(self, count: int = 2) -> dict[str, object]:
        return {
            "protocol": flow.FLOW_PROTOCOL,
            "flow_id": "debug-test",
            "task": "prove orchestration only",
            "context_catalog": {},
            "prompt_catalog": {},
            "phases": [
                {
                    "id": f"unit-{ordinal}",
                    "phase": "TEST",
                    "phase_detail": f"detail-{ordinal}",
                    "model": "test-model",
                    "reasoning": "low",
                    "sandbox": "workspace-write",
                }
                for ordinal in range(count)
            ],
        }

    def _state(self, spec: dict[str, object], run_dir: Path) -> dict[str, object]:
        flow_bytes = (json.dumps(spec) + "\n").encode()
        flow._write_bytes(run_dir / "flow.json", flow_bytes)
        executable = run_dir / "codex"
        executable.write_text("test executable", encoding="utf-8")
        return {
            "protocol": flow.STATE_PROTOCOL,
            "status": "running",
            "mode": "debug",
            "certification_scope": "orchestration_only",
            "run_id": "run-test",
            "run_nonce": "a" * 32,
            "flow_id": spec["flow_id"],
            "flow_spec_sha256": flow._sha_bytes(flow_bytes),
            "catalog_sha256": flow._catalog_hash(spec),
            "codex_executable": str(executable),
            "codex_version": "codex-cli test",
            "codex_executable_sha256": flow._sha_file(executable),
            "controller_sha256": flow._sha_file(flow.Path(flow.__file__)),
            "supervisor_sha256": flow._sha_file(flow.Path(flow.__file__).with_name("supervised_child.py")),
            "next_ordinal": 0,
            "state_revision": 0,
            "created_at": "2026-07-20T00:00:00Z",
            "updated_at": "2026-07-20T00:00:00Z",
        }

    def _succeeded_receipt(
        self, run_dir: Path, state: dict[str, object], spec: dict[str, object], ordinal: int
    ) -> dict[str, object]:
        phase = spec["phases"][ordinal]
        unit_dir = flow._unit_dir(run_dir, ordinal, phase["id"])
        unit_dir.mkdir(parents=True)
        identity = flow._identity(state["run_nonce"], ordinal, f"{ordinal + 1:032x}")
        template, prompt = flow._compile_prompt(identity)
        flow._write_bytes(unit_dir / "prompt.txt", prompt)
        runtime_root = run_dir / ".child-runtime"
        runtime_paths = {
            "root": runtime_root,
            "home": runtime_root / "home",
            "codex_home": runtime_root / "codex-home",
            "workspace": runtime_root / "workspace",
            "temporary": runtime_root / "tmp",
        }
        raw_final = runtime_root / "io" / f"{ordinal:03d}-{identity['unit_nonce']}" / "final.json"
        argv = flow._child_argv(Path(state["codex_executable"]), runtime_paths, phase, raw_final)
        launch = flow._launch_contract(argv, runtime_paths, flow._child_env(runtime_paths))
        flow._write_json(unit_dir / "launch.json", launch)
        for name in ("final.json", "marker.json"):
            flow._write_json(unit_dir / name, identity)
        (unit_dir / "stderr.txt").write_bytes(b"")
        thread_id = f"thread-{ordinal}"
        (unit_dir / "stdout.jsonl").write_text(
            json.dumps({"type": "thread.started", "thread_id": thread_id})
            + "\n"
            + json.dumps({"type": "turn.started"})
            + "\n"
            + json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": f"change-{ordinal}",
                        "type": "file_change",
                        "changes": [
                            {
                                "path": str(
                                    run_dir
                                    / ".child-runtime"
                                    / "workspace"
                                    / "markers"
                                    / "identity.json"
                                ),
                                "kind": "add",
                            }
                        ],
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"change-{ordinal}",
                        "type": "file_change",
                        "changes": [
                            {
                                "path": str(
                                    run_dir
                                    / ".child-runtime"
                                    / "workspace"
                                    / "markers"
                                    / "identity.json"
                                ),
                                "kind": "add",
                            }
                        ],
                    },
                }
            )
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}})
            + "\n",
            encoding="utf-8",
        )
        receipt = {
            "protocol": flow.RECEIPT_PROTOCOL,
            "status": "succeeded",
            "run_id": state["run_id"],
            "ordinal": ordinal,
            "phase_id": phase["id"],
            "phase": phase["phase"],
            "phase_detail": phase["phase_detail"],
            "unit_nonce": identity["unit_nonce"],
            "pid": 1000 + ordinal,
            "process_group_id": 1000 + ordinal,
            "process_start_fingerprint": f"{1000 + ordinal}:test-start",
            "flow_spec_sha256": state["flow_spec_sha256"],
            "catalog_sha256": state["catalog_sha256"],
            "codex_executable_sha256": state["codex_executable_sha256"],
            "controller_sha256": state["controller_sha256"],
            "supervisor_sha256": state["supervisor_sha256"],
            "launch_sha256": flow._sha_file(unit_dir / "launch.json"),
            "output_schema_sha256": flow._sha_file(flow._PACKAGE / "schemas" / "phase-child.schema.json"),
            "template_sha256": flow._sha_bytes(template),
            "compiled_prompt_sha256": flow._sha_bytes(prompt),
            "model": phase["model"],
            "reasoning": phase["reasoning"],
            "sandbox": phase["sandbox"],
            "context_ids": [],
            "context_bytes": 0,
            "command_executions": 0,
            "document_reads": 0,
            "forbidden_evidence_events": 0,
            "unexpected_item_events": 0,
            "thread_id": thread_id,
            "event_types": ["item.completed", "item.started", "thread.started", "turn.completed", "turn.started"],
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 0},
            "stdout_sha256": flow._sha_file(unit_dir / "stdout.jsonl"),
            "stderr_sha256": flow._sha_file(unit_dir / "stderr.txt"),
            "final_sha256": flow._sha_file(unit_dir / "final.json"),
            "marker_sha256": flow._sha_file(unit_dir / "marker.json"),
            "started_at": "2026-07-20T00:00:00Z",
            "completed_at": "2026-07-20T00:00:01Z",
            "status_history": [
                {"status": "prepared", "at": "2026-07-20T00:00:00Z"},
                {"status": "spawned_unconfirmed", "at": "2026-07-20T00:00:00Z"},
                {"status": "running", "at": "2026-07-20T00:00:00Z"},
                {"status": "succeeded", "at": "2026-07-20T00:00:01Z"},
            ],
        }
        flow._write_json(flow._receipt_path(run_dir, ordinal), receipt)
        return receipt

    def test_duplicate_keys_fail_before_schema_validation(self) -> None:
        with self.assertRaisesRegex(flow.PhaseFlowError, "duplicate JSON key"):
            flow._decode_object(b'{"protocol":"one","protocol":"two"}', "test")

    def test_empty_or_absent_context_catalog_derives_debug(self) -> None:
        spec = self._flow()
        self.assertEqual(flow._validate_flow(spec), "debug")
        del spec["context_catalog"]
        self.assertEqual(flow._validate_flow(spec), "debug")
        spec = self._flow()
        spec["context_catalog"] = {
            "requirements": {"path": "requirements.md", "role": "acceptance", "required": True}
        }
        spec["prompt_catalog"] = {
            "work": {
                "template": "prompt.txt",
                "context_ids": ["requirements"],
                "output_schema": "result.schema.json",
            }
        }
        for phase in spec["phases"]:
            phase["prompt_id"] = "work"
        self.assertEqual(flow._validate_flow(spec), "project")

    def test_debug_mode_rejects_named_prompts_and_project_root(self) -> None:
        spec = self._flow()
        spec["project_root"] = "/project"
        with self.assertRaisesRegex(flow.PhaseFlowError, "project_root"):
            flow._validate_flow(spec)
        spec = self._flow()
        spec["prompt_catalog"] = {
            "named": {"template": "x", "context_ids": [], "output_schema": "x"}
        }
        with self.assertRaisesRegex(flow.PhaseFlowError, "prompt_catalog"):
            flow._validate_flow(spec)

    def test_flow_schema_rejects_high_reasoning(self) -> None:
        spec = self._flow()
        spec["phases"][0]["reasoning"] = "high"
        with self.assertRaisesRegex(flow.PhaseFlowError, "schema rejection"):
            flow._validate_flow(spec)

    def test_compiled_prompt_is_neutral(self) -> None:
        identity = flow._identity("a" * 32, 0, "b" * 32)
        _, prompt = flow._compile_prompt(identity)
        lowered = prompt.decode().lower()
        for forbidden in ("implement-v13", "planner_prepare", "planning", "skill.md", "agents.md"):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("do not inspect, list, search, or read any file", lowered)

    def test_event_audit_accepts_patch_and_rejects_commands_or_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            clean = [
                {"type": "thread.started", "thread_id": "thread-clean"},
                {"type": "turn.started"},
                {
                    "type": "item.started",
                    "item": {
                        "id": "file-change",
                        "type": "file_change",
                        "changes": [{"path": "/tmp/workspace/markers/identity.json", "kind": "add"}],
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "file-change",
                        "type": "file_change",
                        "changes": [{"path": "/tmp/workspace/markers/identity.json", "kind": "add"}],
                    },
                },
                {"type": "turn.completed", "usage": {"input_tokens": 4}},
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in clean), encoding="utf-8")
            thread, usage, _ = flow._inspect_events(path)
            self.assertEqual(thread, "thread-clean")
            self.assertEqual(usage["input_tokens"], 4)
            contaminated = clean.copy()
            contaminated.insert(1, {"type": "item.completed", "item": {"type": "command_execution"}})
            path.write_text("".join(json.dumps(item) + "\n" for item in contaminated), encoding="utf-8")
            with self.assertRaisesRegex(flow.PhaseFlowError, "forbidden child item type"):
                flow._inspect_events(path)
            contaminated = clean.copy()
            contaminated.insert(1, {"type": "item.completed", "item": {"type": "agent_message", "text": "read SKILL.md"}})
            path.write_text("".join(json.dumps(item) + "\n" for item in contaminated), encoding="utf-8")
            with self.assertRaisesRegex(flow.PhaseFlowError, "forbidden discovery token"):
                flow._inspect_events(path)

    def test_private_runtime_links_only_auth_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            source = parent / "source"
            source.mkdir()
            auth = source / "auth.json"
            auth.write_text("secret-not-for-artifacts", encoding="utf-8")
            auth.chmod(0o600)
            run_dir = parent / "run"
            run_dir.mkdir(mode=0o700)
            with mock.patch.object(flow, "_source_codex_home", return_value=source):
                paths = flow._prepare_isolation(run_dir)
            linked = paths["codex_home"] / "auth.json"
            self.assertTrue(linked.is_symlink())
            self.assertEqual(linked.resolve(), auth.resolve())
            self.assertEqual(sorted(item.name for item in paths["codex_home"].iterdir()), ["auth.json"])
            self.assertEqual(os.stat(paths["root"]).st_mode & 0o777, 0o700)
            flow._remove_isolation(paths, run_dir)
            self.assertFalse(paths["root"].exists())

    def test_controller_preflight_is_read_only_and_records_unavailable_quota_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flow_path = root / "flow.json"
            flow_path.write_text(json.dumps(self._flow(1)), encoding="utf-8")
            auth_home = root / "codex-home"
            auth_home.mkdir()
            auth = auth_home / "auth.json"
            auth.write_text("opaque", encoding="utf-8")
            auth.chmod(0o600)
            executable = root / "codex"
            executable.write_text("binary", encoding="utf-8")
            identity = (executable.resolve(), "codex-cli test", flow._sha_file(executable))
            run_dir = root / "future-run"
            with (
                mock.patch.object(flow, "_source_codex_home", return_value=auth_home),
                mock.patch.object(flow, "_resolve_codex", return_value=identity),
            ):
                _, _, observed_identity, report = flow._controller_preflight(flow_path, run_dir)
            self.assertEqual(observed_identity, identity)
            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["quota_headroom"], "not_exposed_by_cli")
            self.assertEqual(report["live_model_probe"], "first_child")
            self.assertFalse(run_dir.exists())

    def test_controller_preflight_fails_before_run_state_when_auth_is_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flow_path = root / "flow.json"
            flow_path.write_text(json.dumps(self._flow(1)), encoding="utf-8")
            auth_home = root / "codex-home"
            auth_home.mkdir()
            auth = auth_home / "auth.json"
            auth.write_text("opaque", encoding="utf-8")
            auth.chmod(0o644)
            executable = root / "codex"
            executable.write_text("binary", encoding="utf-8")
            identity = (executable.resolve(), "codex-cli test", flow._sha_file(executable))
            run_dir = root / "future-run"
            with (
                mock.patch.object(flow, "_source_codex_home", return_value=auth_home),
                mock.patch.object(flow, "_resolve_codex", return_value=identity),
            ):
                with self.assertRaisesRegex(flow.PhaseFlowError, "broader than 0600"):
                    flow._controller_preflight(flow_path, run_dir)
            self.assertFalse(run_dir.exists())

    def test_load_rejects_tampered_preflight_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = self._flow()
            state = self._state(spec, run_dir)
            preflight = {"status": "ready"}
            flow._write_json(run_dir / "controller-preflight.json", preflight)
            state["controller_preflight_sha256"] = flow._sha_file(run_dir / "controller-preflight.json")
            flow._write_json(run_dir / "checkpoint.json", state)
            flow._write_json(run_dir / "controller-preflight.json", {"status": "changed"})
            with self.assertRaisesRegex(flow.PhaseFlowError, "preflight hash mismatch"):
                flow._load_run(run_dir)

    def test_child_environment_drops_unrelated_controller_secrets(self) -> None:
        paths = {
            "home": Path("/private/home"),
            "codex_home": Path("/private/codex"),
            "temporary": Path("/private/tmp"),
        }
        with mock.patch.dict(os.environ, {"PATH": "/bin", "HARNESS_SECRET": "do-not-forward"}, clear=True):
            environment = flow._child_env(paths)
        self.assertEqual(environment["PATH"], "/bin")
        self.assertNotIn("HARNESS_SECRET", environment)
        self.assertEqual(environment["TMPDIR"], "/private/tmp")

    def test_run_directory_inside_git_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            (repository / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(flow.PhaseFlowError, "outside every Git repository"):
                flow._assert_outside_repository(repository / "nested" / "run")

    def test_runtime_cleanup_rejects_symlink_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            units = run_dir / "units"
            units.mkdir()
            (units / "evidence.txt").write_text("preserve", encoding="utf-8")
            (run_dir / ".child-runtime").symlink_to(units, target_is_directory=True)
            with self.assertRaisesRegex(flow.PhaseFlowError, "symlink"):
                flow._remove_isolation({"root": run_dir / ".child-runtime"}, run_dir)
            self.assertEqual((units / "evidence.txt").read_text(encoding="utf-8"), "preserve")

    def test_event_lifecycle_requires_turn_start_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            invalid = [
                {"type": "thread.started", "thread_id": "thread"},
                {
                    "type": "item.started",
                    "item": {
                        "id": "change",
                        "type": "file_change",
                        "changes": [{"path": "/tmp/identity.json", "kind": "add"}],
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "change",
                        "type": "file_change",
                        "changes": [{"path": "/tmp/identity.json", "kind": "add"}],
                    },
                },
                {"type": "turn.completed"},
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in invalid), encoding="utf-8")
            with self.assertRaisesRegex(flow.PhaseFlowError, "cardinality"):
                flow._inspect_events(path)

    def test_explicit_resume_archives_failed_attempt_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = self._flow()
            state = self._state(spec, run_dir)
            state.update({"status": "blocked", "blocked_ordinal": 0, "last_error_type": "PhaseFlowError"})
            flow._write_json(run_dir / "checkpoint.json", state)
            unit_dir = flow._unit_dir(run_dir, 0, "unit-0")
            unit_dir.mkdir(parents=True)
            (unit_dir / "prompt.txt").write_text("safe prompt", encoding="utf-8")
            receipt = {
                "protocol": flow.RECEIPT_PROTOCOL,
                "status": "failed",
                "ordinal": 0,
                "command_executions": 1,
                "document_reads": 1,
                "forbidden_evidence_events": 0,
                "unexpected_item_events": 0,
                "status_history": [{"status": "failed", "at": "2026-07-20T00:00:00Z"}],
            }
            flow._write_json(flow._receipt_path(run_dir, 0), receipt)
            recovered = flow._recover_for_resume(state, spec, run_dir)
            self.assertEqual(recovered["status"], "running")
            self.assertFalse(flow._receipt_path(run_dir, 0).exists())
            archived = list((run_dir / "failed-attempts").glob("*/receipt.json"))
            self.assertEqual(len(archived), 1)
            inspected = flow._inspect(run_dir)
            self.assertTrue(inspected["contamination_detected"])
            self.assertEqual(inspected["command_executions"], 1)

    def test_crash_window_reconciles_terminal_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = self._flow()
            state = self._state(spec, run_dir)
            flow._write_json(run_dir / "checkpoint.json", state)
            self._succeeded_receipt(run_dir, state, spec, 0)
            state = flow._reconcile(state, spec, run_dir)
            self.assertEqual(state["next_ordinal"], 1)

    def test_resume_at_existing_stop_bound_launches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = self._flow()
            state = self._state(spec, run_dir)
            self._succeeded_receipt(run_dir, state, spec, 0)
            state["next_ordinal"] = 1
            flow._write_json(run_dir / "checkpoint.json", state)
            with mock.patch.object(flow, "_prepare_isolation", side_effect=AssertionError("must not launch")):
                flow._drive(state, spec, run_dir, stop_after=1, timeout=1)
            state = flow._reconcile(state, spec, run_dir)
            self.assertEqual(state["next_ordinal"], 1)

    def test_finish_and_verify_revalidate_all_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = self._flow()
            state = self._state(spec, run_dir)
            for ordinal in range(2):
                self._succeeded_receipt(run_dir, state, spec, ordinal)
            state["next_ordinal"] = 2
            flow._write_json(run_dir / "checkpoint.json", state)
            result = flow._finish(state, spec, run_dir)
            self.assertEqual(result["units_validated"], 2)
            codex_identity = (
                Path(state["codex_executable"]),
                state["codex_version"],
                state["codex_executable_sha256"],
            )
            with mock.patch.object(flow, "_resolve_codex", return_value=codex_identity):
                self.assertEqual(flow._verify(run_dir)["distinct_thread_ids"], 2)
            receipt_path = flow._receipt_path(run_dir, 0)
            receipt = flow._read_object(receipt_path)
            original_receipt = json.loads(json.dumps(receipt))
            receipt["usage"]["input_tokens"] += 1
            flow._write_json(receipt_path, receipt)
            with mock.patch.object(flow, "_resolve_codex", return_value=codex_identity):
                with self.assertRaisesRegex(flow.PhaseFlowError, "usage metrics mismatch"):
                    flow._verify(run_dir)
            flow._write_json(receipt_path, original_receipt)
            marker = flow._unit_dir(run_dir, 0, "unit-0") / "marker.json"
            marker.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(flow, "_resolve_codex", return_value=codex_identity):
                with self.assertRaisesRegex(flow.PhaseFlowError, "hash mismatch"):
                    flow._verify(run_dir)


if __name__ == "__main__":
    unittest.main()
