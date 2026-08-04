from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_exec import (  # noqa: E402
    VALIDATOR_UNAVAILABLE_ERROR,
    StateError,
    _codex_argv,
    _provider_usage,
    _scratch_contents_sha256,
    run,
    terminal_retry_allowed,
)
from repair_preflight import probe_role_capabilities  # noqa: E402
from state_io import atomic_write_json, sha256_file  # noqa: E402


class RunExecTests(unittest.TestCase):
    def test_fix_result_schema_omits_codex_unsupported_unique_items(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "fix-result.schema.json"
        schema = json.loads(schema_path.read_text())

        def assert_no_unique_items(value: object) -> None:
            if isinstance(value, dict):
                self.assertNotIn("uniqueItems", value)
                for child in value.values():
                    assert_no_unique_items(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_unique_items(child)

        assert_no_unique_items(schema)

    def test_skill_requires_non_project_validator_launcher(self) -> None:
        package = Path(__file__).resolve().parents[1]
        skill = (package / "SKILL.md").read_text(encoding="utf-8")
        protocol = (package / "references" / "protocol.md").read_text(encoding="utf-8")
        launcher = "uv run --no-project --with 'jsonschema==4.26.0' python"
        self.assertIn(launcher, skill)
        self.assertIn(launcher, protocol)
        self.assertIn("any new `uv.lock`", skill)
        self.assertIn("A new `uv.lock`", protocol)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("return the expected result", encoding="utf-8")
        self.schema = self.root / "schema.json"
        self.schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["phase", "role"],
                    "properties": {
                        "phase": {"type": "string", "const": "PLANNING"},
                        "role": {"type": "string", "const": "planner"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.fake = self.root / "codex"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "if '--version' in sys.argv:\n"
            " print('codex-cli 0.test')\n"
            " raise SystemExit(0)\n"
            "args=sys.argv[1:]\n"
            "out=pathlib.Path(args[args.index('-o')+1])\n"
            "out.write_text(json.dumps({'phase':'PLANNING','role':'planner'}))\n"
            "print(json.dumps({'type':'thread.started','thread_id':'thread-test'}), flush=True)\n"
            "print(json.dumps({'type':'turn.completed'}), flush=True)\n",
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _spec(self) -> Path:
        path = self.root / "spec.json"
        atomic_write_json(
            path,
            {
                "receipt_id": "fr:PLANNING:planner_run:planner:0:1",
                "phase": "PLANNING",
                "phase_detail": "planner_run",
                "role": "planner",
                "cwd": str(self.root),
                "prompt_path": str(self.prompt),
                "schema_path": str(self.schema),
                "artifact_dir": str(self.artifacts),
                "model": "test-model",
                "reasoning": "low",
                "sandbox": "read-only",
                "wall_timeout_seconds": 5,
                "expected": {"phase": "PLANNING", "role": "planner"},
            },
        )
        return path

    def _successful_attempt(self) -> tuple[Path, dict[str, object], tuple[str, str, str]]:
        spec_path = self._spec()
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            receipt = run(spec_path)
        identity = (str(self.fake.resolve()), str(receipt["codex_version"]), str(receipt["codex_executable_sha256"]))
        return spec_path, receipt, identity

    def _mark_validator_failed(self, receipt: dict[str, object]) -> Path:
        failed = dict(receipt)
        failed.update(
            status="failed",
            validation_errors=[VALIDATOR_UNAVAILABLE_ERROR],
            state_revision=int(failed["state_revision"]) + 1,
        )
        receipt_path = self.artifacts / "fr-PLANNING-planner_run-planner-0-1.receipt.json"
        atomic_write_json(receipt_path, failed)
        return receipt_path

    def test_gated_child_produces_terminal_receipt(self) -> None:
        spec_path, receipt, resolved = self._successful_attempt()
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["thread_id"], "thread-test")
        self.assertIn("spawned_at", receipt)
        self.assertIn("process_start_fingerprint", receipt)
        self.assertTrue(Path(receipt["exit_path"]).is_file())
        self.assertNotEqual(receipt["prompt_path"], str(self.prompt.resolve()))
        self.assertEqual(receipt["prompt_source_path"], str(self.prompt.resolve()))
        self.assertEqual(receipt["prompt_sha256"], sha256_file(Path(receipt["prompt_path"])))
        self.assertNotEqual(receipt["schema_path"], str(self.schema.resolve()))
        self.assertEqual(receipt["schema_source_path"], str(self.schema.resolve()))
        self.assertEqual(receipt["schema_sha256"], sha256_file(Path(receipt["schema_path"])))
        self.assertEqual(receipt["schema_transport_sha256"], receipt["schema_sha256"])
        self.assertNotEqual(receipt["schema_source_sha256"], "")
        self.assertEqual(receipt["terminal_cause"]["class"], "none")
        self.assertEqual(
            receipt["provider_usage"],
            {
                "status": "unknown",
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
            },
        )
        self.assertRegex(receipt["controller_package_digest"], r"^[0-9a-f]{64}$")
        self.assertIn(receipt["schema_path"], receipt["argv"])
        bound_schema = json.loads(Path(receipt["schema_path"]).read_text(encoding="utf-8"))
        self.assertEqual(bound_schema["properties"]["phase"]["const"], "PLANNING")
        self.assertEqual(bound_schema["properties"]["role"]["const"], "planner")
        with patch("run_exec._resolve_codex", return_value=resolved), patch(
            "run_exec.subprocess.Popen", side_effect=AssertionError("terminal receipt relaunched")
        ):
            reused = run(spec_path)
        self.assertEqual(reused, receipt)

    def test_provider_terminal_usage_is_exact_and_malformed_or_conflicting_fails(self) -> None:
        expected = {
            "status": "recorded",
            "input_tokens": 37,
            "cached_input_tokens": 11,
            "output_tokens": 5,
        }
        events = [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 37,
                    "cached_input_tokens": 11,
                    "output_tokens": 5,
                },
            }
        ]
        self.assertEqual(_provider_usage(events), expected)
        with self.assertRaisesRegex(StateError, "invalid: input_tokens"):
            _provider_usage(
                [
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": True,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                        },
                    }
                ]
            )
        with self.assertRaisesRegex(StateError, "conflicting"):
            _provider_usage(
                [
                    *events,
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 38,
                            "cached_input_tokens": 11,
                            "output_tokens": 5,
                        },
                    },
                ]
            )

    def test_terminal_receipt_revalidates_provider_usage_from_bound_stdout(self) -> None:
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "if '--version' in sys.argv:\n"
            " print('codex-cli 0.test'); raise SystemExit(0)\n"
            "args=sys.argv[1:]\n"
            "out=pathlib.Path(args[args.index('-o')+1])\n"
            "out.write_text(json.dumps({'phase':'PLANNING','role':'planner'}))\n"
            "print(json.dumps({'type':'thread.started','thread_id':'thread-usage'}),flush=True)\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':37,'cached_input_tokens':11,'output_tokens':5}}),flush=True)\n",
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        spec_path, receipt, identity = self._successful_attempt()
        self.assertEqual(
            receipt["provider_usage"],
            {
                "status": "recorded",
                "input_tokens": 37,
                "cached_input_tokens": 11,
                "output_tokens": 5,
            },
        )
        receipt_path = (
            self.artifacts / "fr-PLANNING-planner_run-planner-0-1.receipt.json"
        )
        changed = dict(receipt)
        changed["provider_usage"] = {
            "status": "recorded",
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        atomic_write_json(receipt_path, changed)
        with patch("run_exec._resolve_codex", return_value=identity), patch(
            "run_exec.subprocess.Popen",
            side_effect=AssertionError("terminal receipt relaunched"),
        ):
            with self.assertRaisesRegex(StateError, "provider usage mismatch"):
                run(spec_path)

    def test_sigint_terminalizes_owned_group_and_reuses_interrupted_receipt(self) -> None:
        descendant_path = self.root / "descendant.pid"
        launch_count = self.root / "launch-count.txt"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, subprocess, sys, time\n"
            "if '--version' in sys.argv:\n"
            " print('codex-cli 0.test'); raise SystemExit(0)\n"
            f"count=pathlib.Path({str(launch_count)!r})\n"
            "count.write_text(str(int(count.read_text())+1) if count.exists() else '1')\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            f"pathlib.Path({str(descendant_path)!r}).write_text(str(child.pid))\n"
            "print(json.dumps({'type':'thread.started','thread_id':'thread-sigint'}),flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        spec_path = self._spec()
        environment = dict(os.environ)
        environment["PATH"] = str(self.root) + os.pathsep + environment.get("PATH", "")
        environment["PYTHONPATH"] = str(SCRIPTS)
        controller = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from run_exec import run; run(Path(__import__('sys').argv[1]))",
                str(spec_path),
            ],
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not descendant_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(descendant_path.is_file(), "blocking descendant did not launch")
        descendant_pid = int(descendant_path.read_text())
        controller.send_signal(signal.SIGINT)
        controller.communicate(timeout=15)
        receipt_path = (
            self.artifacts / "fr-PLANNING-planner_run-planner-0-1.receipt.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["terminal_cause"]["class"], "controller_interrupted")
        self.assertEqual(receipt["interruption"]["termination_status"], "verified")
        self.assertTrue(receipt["interruption"]["supervisor_reaped"])
        import jsonschema

        receipt_schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas/process-receipt.schema.json"
            ).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(receipt_schema).validate(receipt)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant_pid, 0)
        identity = (
            str(self.fake.resolve()),
            str(receipt["codex_version"]),
            str(receipt["codex_executable_sha256"]),
        )
        with patch("run_exec._resolve_codex", return_value=identity), patch(
            "run_exec.subprocess.Popen",
            side_effect=AssertionError("interrupted receipt relaunched child"),
        ):
            reused = run(spec_path)
        self.assertEqual(reused, receipt)
        self.assertEqual(launch_count.read_text(), "1")

    def test_validator_preflight_fails_before_receipt_or_launch(self) -> None:
        with patch("run_exec._build_validator", side_effect=StateError(VALIDATOR_UNAVAILABLE_ERROR)), patch(
            "run_exec.subprocess.Popen", side_effect=AssertionError("child launched")
        ):
            with self.assertRaisesRegex(StateError, "jsonschema is required"):
                run(self._spec())
        self.assertFalse(self.artifacts.exists())

    def test_expected_identity_is_bound_when_source_schema_has_no_const(self) -> None:
        source = json.loads(self.schema.read_text(encoding="utf-8"))
        source["properties"]["phase"].pop("const")
        source["properties"]["role"].pop("const")
        self.schema.write_text(json.dumps(source), encoding="utf-8")

        _, receipt, _ = self._successful_attempt()

        snapshot = json.loads(Path(receipt["schema_path"]).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["properties"]["phase"]["const"], "PLANNING")
        self.assertEqual(snapshot["properties"]["role"]["const"], "planner")
        self.assertNotIn("const", source["properties"]["phase"])

    def test_expected_identity_missing_from_schema_fails_before_attempt(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["expected"]["protocol"] = "implement-v13-codex/test/1"
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "absent from output schema: protocol"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_expected_identity_conflicting_with_schema_fails_before_attempt(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["expected"]["role"] = "reviewer"
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "conflicts with schema const: role"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_additional_writable_root_is_forwarded_on_launch_and_resume(self) -> None:
        extra = self.root / "base-worktree"
        extra.mkdir()
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(sandbox="workspace-write", writable_roots=[str(extra)])
        launch = _codex_argv(spec, "codex", self.root / "output.json", self.schema)
        self.assertEqual(launch[launch.index("--add-dir") + 1], str(extra.resolve()))

        spec["resume_thread_id"] = "thread-test"
        resume = _codex_argv(spec, "codex", self.root / "output.json", self.schema)
        self.assertIn(
            "sandbox_workspace_write.writable_roots=" + json.dumps([str(extra.resolve())]),
            resume,
        )

    def test_child_launch_and_resume_disable_internal_multi_agent(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        launch = _codex_argv(spec, "codex", self.root / "output.json", self.schema)
        self.assertIn(["--disable", "multi_agent"], [launch[i : i + 2] for i in range(len(launch) - 1)])

        spec["resume_thread_id"] = "thread-test"
        resume = _codex_argv(spec, "codex", self.root / "output.json", self.schema)
        self.assertIn(["--disable", "multi_agent"], [resume[i : i + 2] for i in range(len(resume) - 1)])

    def test_controller_owned_ephemeral_scratch_is_passed_hashed_and_removed(self) -> None:
        probe_role_capabilities(
            repository_root=self.root,
            artifact_dir=self.root,
            feature_run_id="fr",
            controller_package_digest="a" * 64,
        )
        capability_path = self.root / "capability-manifest.v2.json"
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(
            feature_run_id="fr",
            ephemeral_scratch=True,
            capability_manifest_path=str(capability_path),
            capability_manifest_sha256=sha256_file(capability_path),
        )
        atomic_write_json(spec_path, spec)
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            receipt = run(spec_path)
        scratch = Path(receipt["ephemeral_scratch"])
        self.assertFalse(scratch.exists())
        self.assertTrue(receipt["ephemeral_scratch_removed"])
        self.assertRegex(receipt["ephemeral_scratch_contents_sha256"], r"^[0-9a-f]{64}$")
        child = json.loads(
            (self.artifacts / "fr-PLANNING-planner_run-planner-0-1.child.json").read_text()
        )
        self.assertEqual(child["environment"]["TMPDIR"], str(scratch))
        self.assertEqual(child["environment"]["CODEX_EPHEMERAL_SCRATCH"], str(scratch))
        self.assertNotIn(str(scratch), receipt["writable_roots"])

    def test_ephemeral_scratch_rejects_caller_selected_path_before_attempt(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["ephemeral_scratch"] = str(self.root / "caller-scratch")
        atomic_write_json(spec_path, spec)
        with self.assertRaisesRegex(StateError, "must be a boolean"):
            run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_ephemeral_scratch_hash_accepts_internal_pytest_symlink(self) -> None:
        scratch = self.root / "scratch"
        target = scratch / "pytest-of-user/pytest-0"
        target.mkdir(parents=True)
        current = target.parent / "pytest-current"
        current.symlink_to(target)
        self.assertRegex(_scratch_contents_sha256(scratch), r"^[0-9a-f]{64}$")

    def test_ephemeral_scratch_hash_rejects_escaping_symlink(self) -> None:
        scratch = self.root / "scratch"
        scratch.mkdir()
        outside = self.root / "outside"
        outside.write_text("unsafe", encoding="utf-8")
        (scratch / "escape").symlink_to(outside)
        with self.assertRaisesRegex(StateError, "escaping or invalid symlink"):
            _scratch_contents_sha256(scratch)

    def test_invalid_writable_root_fails_before_receipt_or_launch(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(sandbox="workspace-write", writable_roots=[str(self.root / "missing")])
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "writable root is not a directory"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_controller_child_marker_is_restricted_before_launch(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["controller_child"] = True
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "restricted to the feature coordinator"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_codex_schema_subset_fails_before_receipt_or_launch(self) -> None:
        self.schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": ["protocol"],
                    "properties": {"protocol": {"const": "implement-v13-codex/1"}},
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(
                StateError, r"explicit type at \$\.properties\.protocol"
            ):
                run(self._spec())
        self.assertFalse(self.artifacts.exists())

    def test_array_without_items_fails_before_receipt_or_launch(self) -> None:
        self.schema.write_text(
            json.dumps({
                "type": "object",
                "additionalProperties": False,
                "required": ["values"],
                "properties": {"values": {"type": "array"}},
            }),
            encoding="utf-8",
        )
        with patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, r"array items at \$\.properties\.values"):
                run(self._spec())
        self.assertFalse(self.artifacts.exists())

    def test_nested_incomplete_required_fails_before_attempt_identity(self) -> None:
        self.schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["nested"],
                    "properties": {
                        "nested": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kept"],
                            "properties": {
                                "kept": {"type": "string"},
                                "missing": {"type": "string"},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, r"required == properties.*nested"):
                run(self._spec())
        self.assertFalse(self.artifacts.exists())

    def test_structured_provider_schema_400_is_nonretryable_and_stderr_is_diagnostic(self) -> None:
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if '--version' in sys.argv:\n"
            " print('codex-cli 0.test'); raise SystemExit(0)\n"
            "print('warning: missing field supports_reasoning_summaries', file=sys.stderr)\n"
            "print(json.dumps({'type':'thread.started','thread_id':'thread-schema'}),flush=True)\n"
            "print(json.dumps({'type':'error','error':{'message':'Invalid response schema: required missing effect_contract','code':'invalid_json_schema','status_code':400}}),flush=True)\n"
            "print(json.dumps({'type':'turn.failed','error':{'message':'Invalid response schema','code':'invalid_json_schema','status_code':400}}),flush=True)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            with self.assertRaises(StateError):
                run(self._spec())
        receipt = json.loads(
            (self.artifacts / "fr-PLANNING-planner_run-planner-0-1.receipt.json").read_text()
        )
        cause = receipt["terminal_cause"]
        self.assertEqual(cause["class"], "response_schema_transport_rejected")
        self.assertEqual(cause["provider_code"], "invalid_json_schema")
        self.assertEqual(cause["http_status"], 400)
        self.assertFalse(terminal_retry_allowed(cause))
        self.assertIn("response schema", cause["message"].lower())
        self.assertIn("supports_reasoning_summaries", receipt["diagnostics"][0]["message"])
        self.assertEqual(
            receipt["artifact_sha256"]["stdout"],
            sha256_file(Path(receipt["stdout_path"])),
        )

    def test_plan_reviewer_rejects_noncanonical_schema_before_attempt(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(
            phase="PLAN_REVIEW",
            phase_detail="review_dispatch",
            role="frame_reviewer",
        )
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "canonical plan-review.schema.json"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_high_reasoning_fails_before_launch(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["reasoning"] = "high"
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "reasoning must be low or medium"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_terra_medium_implementation_worker_launches(self) -> None:
        import jsonschema

        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(
            phase="IMPLEMENTING",
            role="implementation_worker",
            model="gpt-5.6-terra",
            reasoning="medium",
        )
        atomic_write_json(spec_path, spec)
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            receipt = run(spec_path)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["phase"], "IMPLEMENTING")
        self.assertEqual(receipt["role"], "implementation_worker")
        self.assertEqual(receipt["model"], "gpt-5.6-terra")
        self.assertEqual(receipt["reasoning"], "medium")
        self.assertIn('model_reasoning_effort="medium"', receipt["argv"])
        receipt_schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/process-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator(receipt_schema).validate(receipt)
        invalid_receipt = dict(receipt, model="gpt-5.6-sol")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(receipt_schema).validate(invalid_receipt)

    def test_terra_medium_code_fixer_launches_and_receipt_validates(self) -> None:
        import jsonschema

        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(
            phase="REVIEWING",
            phase_detail="fix",
            role="code_fixer",
            model="gpt-5.6-terra",
            reasoning="medium",
        )
        atomic_write_json(spec_path, spec)
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            receipt = run(spec_path)
        self.assertEqual(
            (receipt["phase"], receipt["role"], receipt["model"], receipt["reasoning"]),
            ("REVIEWING", "code_fixer", "gpt-5.6-terra", "medium"),
        )
        receipt_schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/process-receipt.schema.json").read_text()
        )
        jsonschema.Draft202012Validator(receipt_schema).validate(receipt)

    def test_legacy_luna_high_implementation_worker_fails_before_launch(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec.update(
            phase="IMPLEMENTING",
            role="implementation_worker",
            model="gpt-5.6-luna",
            reasoning="high",
        )
        atomic_write_json(spec_path, spec)
        with patch(
            "run_exec._resolve_codex", side_effect=AssertionError("Codex resolved")
        ), patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "Terra-medium implementation identity"):
                run(spec_path)
        self.assertFalse(self.artifacts.exists())

    def test_unresolved_schema_reference_fails_before_receipt_or_launch(self) -> None:
        self.schema.write_text(json.dumps({"$ref": "file:///missing/q11.json"}), encoding="utf-8")
        with patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "local JSON pointer"):
                run(self._spec())
        self.assertFalse(self.artifacts.exists())

    def test_nested_schema_resource_fails_before_receipt_or_launch(self) -> None:
        self.schema.write_text(json.dumps({"$defs": {"child": {"$id": "child", "type": "string"}}}), encoding="utf-8")
        with patch("run_exec.subprocess.Popen", side_effect=AssertionError("child launched")):
            with self.assertRaisesRegex(StateError, "nested resources are unsupported"):
                run(self._spec())
        self.assertFalse(self.artifacts.exists())

    def test_validator_only_failure_revalidates_same_attempt_without_launch(self) -> None:
        spec_path, receipt, resolved = self._successful_attempt()
        receipt_path = self._mark_validator_failed(receipt)
        failed_revision = json.loads(receipt_path.read_text(encoding="utf-8"))["state_revision"]
        with patch("run_exec._resolve_codex", return_value=resolved), patch(
            "run_exec.subprocess.Popen", side_effect=AssertionError("revalidation relaunched child")
        ):
            recovered = run(spec_path)
            recovered_again = run(spec_path)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["attempt"], receipt["attempt"])
        self.assertEqual(recovered["thread_id"], receipt["thread_id"])
        self.assertEqual(recovered["state_revision"], failed_revision + 1)
        self.assertEqual(recovered_again, recovered)

    def test_revalidation_rejects_mixed_or_unproven_failure(self) -> None:
        for field, value, message in (
            ("validation_errors", [VALIDATOR_UNAVAILABLE_ERROR, "missing turn.completed"], "terminal-failed"),
            ("artifact_sha256", None, "complete artifact hash manifest"),
            ("expected_sha256", None, "original semantic contract"),
            ("exit_code", 1, "conflicting failure evidence"),
            ("timed_out", True, "conflicting failure evidence"),
        ):
            with self.subTest(field=field):
                spec_path, receipt, resolved = self._successful_attempt()
                receipt_path = self._mark_validator_failed(receipt)
                failed = json.loads(receipt_path.read_text(encoding="utf-8"))
                if value is None:
                    failed.pop(field)
                else:
                    failed[field] = value
                atomic_write_json(receipt_path, failed)
                with patch("run_exec._resolve_codex", return_value=resolved), patch(
                    "run_exec.subprocess.Popen", side_effect=AssertionError("failed receipt relaunched")
                ):
                    with self.assertRaisesRegex(StateError, message):
                        run(spec_path)
                for artifact in self.artifacts.iterdir():
                    artifact.unlink()

    def test_revalidation_rejects_changed_artifact(self) -> None:
        spec_path, receipt, resolved = self._successful_attempt()
        self._mark_validator_failed(receipt)
        Path(str(receipt["stdout_path"])).write_text("{}\n", encoding="utf-8")
        with patch("run_exec._resolve_codex", return_value=resolved), patch(
            "run_exec.subprocess.Popen", side_effect=AssertionError("failed receipt relaunched")
        ):
            with self.assertRaisesRegex(StateError, "artifact hash mismatch"):
                run(spec_path)

    def test_revalidation_rejects_missing_terminal_event(self) -> None:
        spec_path, receipt, resolved = self._successful_attempt()
        receipt_path = self._mark_validator_failed(receipt)
        failed = json.loads(receipt_path.read_text(encoding="utf-8"))
        stdout = Path(str(receipt["stdout_path"]))
        stdout.write_text(json.dumps({"type": "thread.started", "thread_id": "thread-test"}) + "\n", encoding="utf-8")
        failed["artifact_sha256"]["stdout"] = sha256_file(stdout)
        failed["event_types"] = ["thread.started"]
        atomic_write_json(receipt_path, failed)
        with patch("run_exec._resolve_codex", return_value=resolved), patch(
            "run_exec.subprocess.Popen", side_effect=AssertionError("failed receipt relaunched")
        ):
            with self.assertRaisesRegex(StateError, "missing turn.completed"):
                run(spec_path)

    def test_revalidation_rejects_schema_failure_even_with_updated_output_hash(self) -> None:
        spec_path, receipt, resolved = self._successful_attempt()
        receipt_path = self._mark_validator_failed(receipt)
        failed = json.loads(receipt_path.read_text(encoding="utf-8"))
        output = Path(str(receipt["output_path"]))
        output.write_text(json.dumps({"phase": "PLANNING", "role": "reviewer"}), encoding="utf-8")
        output_hash = sha256_file(output)
        failed["output_sha256"] = output_hash
        failed["artifact_sha256"]["output"] = output_hash
        atomic_write_json(receipt_path, failed)
        with patch("run_exec._resolve_codex", return_value=resolved), patch(
            "run_exec.subprocess.Popen", side_effect=AssertionError("failed receipt relaunched")
        ):
            with self.assertRaisesRegex(StateError, "schema validation failed"):
                run(spec_path)

    def test_semantic_mismatch_fails(self) -> None:
        spec_path = self._spec()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["expected"]["role"] = "reviewer"
        atomic_write_json(spec_path, spec)
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            with self.assertRaises(StateError):
                run(spec_path)

    def test_existing_receipt_rejects_invocation_mismatch(self) -> None:
        spec_path, _, _ = self._successful_attempt()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["model"] = "different-model"
        atomic_write_json(spec_path, spec)
        with patch("run_exec.shutil.which", return_value=str(self.fake)):
            with self.assertRaisesRegex(StateError, "invocation mismatch"):
                run(spec_path)


if __name__ == "__main__":
    unittest.main()
