from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch


PACKAGE = Path(__file__).parents[1]
SCRIPTS = PACKAGE / "scripts"
SERIAL_SCRIPT = PACKAGE / "scripts" / "feature_queue_state.py"
SERIAL_TEST = PACKAGE / "tests" / "test_feature_queue_state.py"
sys.path.insert(0, str(SCRIPTS))

from run_feature import (  # noqa: E402
    CONTROLLER_CHILD_ENV,
    EXECUTION_ENVIRONMENT_CONTEXT,
    _broker_invocation,
    _coordinator_limits,
    _emit_controller_phase,
    _requested_specs,
    _recover_coordinator_position,
    _receipt_provider_usage,
    _runtime_rollover_paths,
    _run_requested_spec,
    _settle_blocked,
    _validate_rollover_ack,
    _validate_requested_spec,
    _write_rollover_summary,
    drive,
    main,
)
from review_closure import create_ledger, record_test, start_attempt, attempt_history_sha256  # noqa: E402
from repair_preflight import probe_role_capabilities, repository_identity  # noqa: E402
from state_io import StateError, atomic_write_json, read_json, sha256_file  # noqa: E402

SPEC = importlib.util.spec_from_file_location("serial_state_fixture", SERIAL_SCRIPT)
assert SPEC and SPEC.loader
serial_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serial_state)
TEST_SPEC = importlib.util.spec_from_file_location("serial_state_test_fixture", SERIAL_TEST)
assert TEST_SPEC and TEST_SPEC.loader
serial_fixture = importlib.util.module_from_spec(TEST_SPEC)
TEST_SPEC.loader.exec_module(serial_fixture)


class RunFeatureTests(unittest.TestCase):
    def test_coordinator_schema_rejects_model_token_fields(self) -> None:
        import jsonschema

        schema = json.loads(
            (PACKAGE / "schemas" / "feature-coordinator-result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        output = {
            "protocol": "implement-v13-codex/coordinator-turn/2",
            "status": "blocked",
            "summary": "blocked",
            "blocker": {
                "blocker_class": "test",
                "reason": "test",
                "resume_condition": "test",
            },
            "resume_token": "token",
            "invocation_spec_path": "",
            "judgment_reason": None,
            "rollover_ack": None,
            "metrics": None,
        }
        jsonschema.Draft202012Validator(schema).validate(output)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(
                {**output, "input_tokens": 0}
            )

    def test_rollover_unknown_usage_stays_null_and_receipt_usage_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint = root / "checkpoint.json"
            atomic_write_json(
                checkpoint,
                {
                    "phase": "REVIEWING",
                    "phase_detail": "fix",
                    "phase_state": "ready",
                    "state_revision": 1,
                },
            )
            dispatch = {
                "feature_run_id": "fr_unknown",
                "controller_package_digest": "a" * 64,
                "coordinator_limits": {
                    "authority": "certification fixture",
                    "max_turns_per_context": 2,
                    "input_token_slope_window": 2,
                    "max_input_token_slope": 500,
                },
            }
            summary = _write_rollover_summary(
                dispatch=dispatch,
                checkpoint_path=checkpoint,
                artifact_dir=root,
                prior_thread_id="thread-old",
                prior_last_turn=1,
                generation=1,
                cause="closure_boundary",
                turns_in_context=1,
                input_tokens=[None],
                coordinator_turns_avoided=0,
            )
            self.assertEqual(
                summary["telemetry"],
                {
                    "status": "unknown",
                    "turns_in_context": 1,
                    "input_tokens_in_context": None,
                    "input_token_slope": None,
                    "slope_window_turns": 2,
                    "coordinator_turns_avoided": 0,
                },
            )
            receipt_path = root / "coordinator.receipt.json"
            atomic_write_json(
                receipt_path,
                {
                    "protocol": "implement-v13-codex/process-receipt/3",
                    "provider_usage": {
                        "status": "recorded",
                        "input_tokens": 101,
                        "cached_input_tokens": 19,
                        "output_tokens": 7,
                    },
                },
            )
            self.assertEqual(
                _receipt_provider_usage(receipt_path)["input_tokens"],
                101,
            )

    def test_migration_rollover_is_not_a_runtime_context_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy = root / "coordinator-rollover-v3.v1.json"
            atomic_write_json(
                legacy,
                {
                    "protocol": "implement-v13-codex/coordinator-rollover/1",
                    "controller_migration_id": "a" * 64,
                    "feature_run_id": "fr_roll",
                    "old_context_state": "historical",
                    "next_context": "fresh",
                },
            )
            current = root / "coordinator-rollover-v4.v1.json"
            atomic_write_json(
                current,
                {
                    "protocol": "implement-v13-codex/controller-migration-rollover/1",
                    "controller_migration_id": "b" * 64,
                    "feature_run_id": "fr_roll",
                },
            )
            self.assertEqual(_runtime_rollover_paths(root), [])
            self.assertEqual(_recover_coordinator_position(root, "fr_roll"), (1, None))

    def test_hash_bound_rollover_requires_fresh_thread_and_exact_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint = root / "checkpoint.json"
            atomic_write_json(
                checkpoint,
                {
                    "phase": "REVIEWING",
                    "phase_detail": "fix",
                    "phase_state": "ready",
                    "state_revision": 7,
                },
            )
            atomic_write_json(root / "review-closure-ledger.v1.json", {"active_closure_id": "a"})
            atomic_write_json(root / "repair-dependency-graph.v2.json", {"closures": []})
            atomic_write_json(
                root / "fr_roll-COORDINATOR-drive-feature_coordinator-1-1.receipt.json",
                {
                    "receipt_id": "fr_roll:COORDINATOR:drive:feature_coordinator:1:1",
                    "status": "succeeded",
                    "thread_id": "thread-old",
                },
            )
            dispatch = {
                "feature_run_id": "fr_roll",
                "controller_package_digest": "a" * 64,
                "coordinator_limits": {
                    "authority": "certification fixture",
                    "max_turns_per_context": 2,
                    "input_token_slope_window": 2,
                    "max_input_token_slope": 500,
                },
            }
            summary = _write_rollover_summary(
                dispatch=dispatch,
                checkpoint_path=checkpoint,
                artifact_dir=root,
                prior_thread_id="thread-old",
                prior_last_turn=1,
                generation=1,
                cause="closure_boundary",
                turns_in_context=1,
                input_tokens=[100],
                coordinator_turns_avoided=4,
            )
            self.assertEqual(
                _recover_coordinator_position(root, "fr_roll"),
                (2, None),
            )
            ack = {
                field: summary[field]
                for field in (
                    "summary_sha256",
                    "controller_package_digest",
                    "checkpoint_sha256",
                    "closure_ledger_sha256",
                    "dependency_graph_sha256",
                )
            }
            _validate_rollover_ack({"rollover_ack": ack}, summary)
            with self.assertRaisesRegex(StateError, "exact rollover hashes"):
                _validate_rollover_ack(
                    {"rollover_ack": {**ack, "checkpoint_sha256": "b" * 64}},
                    summary,
                )
            atomic_write_json(
                root / "fr_roll-COORDINATOR-drive-feature_coordinator-2-1.receipt.json",
                {
                    "receipt_id": "fr_roll:COORDINATOR:drive:feature_coordinator:2:1",
                    "status": "succeeded",
                    "thread_id": "thread-new",
                },
            )
            self.assertEqual(
                _recover_coordinator_position(root, "fr_roll"),
                (3, "thread-new"),
            )
            old_thread = read_json(
                root / "fr_roll-COORDINATOR-drive-feature_coordinator-2-1.receipt.json"
            )
            old_thread["thread_id"] = "thread-old"
            atomic_write_json(
                root / "fr_roll-COORDINATOR-drive-feature_coordinator-2-1.receipt.json",
                old_thread,
            )
            with self.assertRaisesRegex(StateError, "pre-rollover thread"):
                _recover_coordinator_position(root, "fr_roll")

    def test_coordinator_limits_are_explicit_authoritative_configuration(self) -> None:
        configured = _coordinator_limits(
            {
                "controller_package_digest": "a" * 64,
                "coordinator_limits": {
                    "authority": "operator certification",
                    "max_turns_per_context": 3,
                    "input_token_slope_window": 2,
                    "max_input_token_slope": 900,
                },
            }
        )
        self.assertEqual(configured["max_turns_per_context"], 3)
        with self.assertRaisesRegex(StateError, "explicit operator or safety authority"):
            _coordinator_limits(
                {
                    "controller_package_digest": "a" * 64,
                    "coordinator_limits": {
                        "authority": "",
                        "max_turns_per_context": 3,
                        "input_token_slope_window": 2,
                        "max_input_token_slope": 900,
                    },
                }
            )

    def test_resume_recovers_contiguous_turn_and_exact_coordinator_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for turn in (1, 2, 3):
                atomic_write_json(
                    root / f"fr_resume-COORDINATOR-drive-feature_coordinator-{turn}-1.receipt.json",
                    {
                        "receipt_id": f"fr_resume:COORDINATOR:drive:feature_coordinator:{turn}:1",
                        "status": "succeeded",
                        "thread_id": "thread-resume",
                    },
                )
            self.assertEqual(
                _recover_coordinator_position(root, "fr_resume"),
                (4, "thread-resume"),
            )

    def test_broker_routes_closure_program_to_deterministic_driver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory).resolve()
            program = artifact_dir / "closure-program.json"
            atomic_write_json(program, {"protocol": "implement-v13-codex/closure-program/1"})
            expected = {
                "protocol": "implement-v13-codex/closure-program/1",
                "status": "closed",
                "closure_id": "authority",
                "receipts": ["a", "b", "c", "d", "e"],
                "metrics": {"deterministic_transitions": 5, "coordinator_turns_avoided": 4},
            }
            with patch("run_feature.run_closure_program", return_value=expected) as driver:
                result = _broker_invocation(
                    {"feature_run_id": "fr_test"}, artifact_dir,
                    {"status": "invoke", "invocation_spec_path": str(program)}, 1,
                )
            driver.assert_called_once()
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["closure_program_result"]["metrics"]["coordinator_turns_avoided"], 4)
            self.assertTrue((artifact_dir / "controller-child-result-000001.json").is_file())

    def test_workspace_write_reviewer_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess = __import__("subprocess")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            spec_path = root / "reviewer.spec.json"
            atomic_write_json(spec_path, {
                "cwd": str(root), "phase": "CODE_REVIEW", "role": "code_reviewer_correctness",
                "sandbox": "workspace-write",
            })

            def mutate(_path):
                tracked.write_text("after\n", encoding="utf-8")
                return {"status": "succeeded"}

            with patch("run_feature.run_exec", side_effect=mutate):
                result = _run_requested_spec(spec_path)
            self.assertEqual(result["status"], "failed")
            self.assertIn("mutated the feature tree", result["error"])
            self.assertEqual(result["failure_class"], "reviewer_tree_mutation")

    def test_author_test_may_write_only_declared_supplemental_test_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess = __import__("subprocess")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            spec_path = root / "author.spec.json"
            atomic_write_json(spec_path, {
                "cwd": str(root), "phase": "REVIEWING", "role": "code_reviewer_correctness",
                "closure_action": "author_test", "sandbox": "workspace-write",
                "allowed_write_paths": ["tests/test_supplemental.py"],
            })

            def write_declared(_path):
                test_path = root / "tests/test_supplemental.py"
                test_path.parent.mkdir(parents=True)
                test_path.write_text("def test_supplemental():\n    assert True\n", encoding="utf-8")
                return {"status": "succeeded", "receipt_id": "author", "output_path": str(root / "out.json")}

            with patch("run_feature.run_exec", side_effect=write_declared):
                result = _run_requested_spec(spec_path)
            self.assertEqual(result["status"], "succeeded")

    def test_author_test_write_outside_declared_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess = __import__("subprocess")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            spec_path = root / "author.spec.json"
            atomic_write_json(spec_path, {
                "cwd": str(root), "phase": "REVIEWING", "role": "code_reviewer_correctness",
                "closure_action": "author_test", "sandbox": "workspace-write",
                "allowed_write_paths": ["tests/test_supplemental.py"],
            })

            def mutate_target(_path):
                tracked.write_text("after\n", encoding="utf-8")
                return {"status": "succeeded"}

            with patch("run_feature.run_exec", side_effect=mutate_target):
                result = _run_requested_spec(spec_path)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_class"], "author_test_scope_violation")
            self.assertIn("tracked.txt", result["error"])

    def test_targeted_review_remains_mutation_protected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess = __import__("subprocess")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            spec_path = root / "targeted.spec.json"
            atomic_write_json(spec_path, {
                "cwd": str(root), "phase": "REVIEWING", "role": "code_reviewer_correctness",
                "closure_action": "targeted_review", "sandbox": "workspace-write",
            })

            def mutate(_path):
                tracked.write_text("after\n", encoding="utf-8")
                return {"status": "succeeded"}

            with patch("run_feature.run_exec", side_effect=mutate):
                result = _run_requested_spec(spec_path)
            self.assertEqual(result["failure_class"], "reviewer_tree_mutation")

    def test_broker_enforces_closure_ledger_and_serial_repair_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory).resolve()
            test_source = artifact_dir / "tests/test_executor.py"
            test_source.parent.mkdir(parents=True)
            test_source.write_text("def test_executor():\\n    assert True\\n")
            probe_role_capabilities(
                repository_root=artifact_dir,
                artifact_dir=artifact_dir,
                feature_run_id="fr_closure",
                controller_package_digest="a" * 64,
            )
            capability_path = artifact_dir / "capability-manifest.v2.json"
            ledger_path = artifact_dir / "review-closure-ledger.v1.json"
            create_ledger(ledger_path, feature_run_id="fr_closure", groups=[{
                "closure_id": "executor", "fingerprints": ["missing-executor"],
                "origin_reviewer": "code_reviewer_correctness",
                "complexity": "implementation", "acceptance": ["dispatch executes a cell"],
            }])
            record_test(ledger_path, "executor", {
                "author_role": "code_reviewer_correctness", "author_receipt_id": "test-author-1",
                "test_paths": ["tests/test_executor.py"], "commands": ["pytest tests/test_executor.py"],
                "observed_failure": True, "evidence": ["no executor consumed the dispatch"],
                "effect_contract": {
                    "protocol": "implement-v13-codex/repair-effect-contract/1",
                    "must_persist": ["failure_checkpoint", "blocked_queue", "failure_summary", "failure_event"],
                    "must_remain_absent": ["success_result", "success_receipt", "integration_artifact", "dispatcher_acknowledgement"],
                    "must_remain_unchanged": ["base_git_state"],
                },
                "repository_root": str(artifact_dir),
                "repository_identity": repository_identity(artifact_dir),
                "test_node_id": "tests/test_executor.py::test_executor",
                "test_source_path": "tests/test_executor.py",
                "test_source_sha256": sha256_file(test_source),
                "assertions": [{
                    "assertion_id": "executor-failure-persists",
                    "test_node_id": "tests/test_executor.py::test_executor",
                    "observation_source": "checkpoint assertion",
                    "source_sha256": sha256_file(test_source),
                    "governed_artifact": "checkpoint",
                    "effect": "failure_checkpoint",
                    "expected_disposition": "must_persist",
                }],
                "capability_manifest_path": str(capability_path),
                "capability_manifest_sha256": sha256_file(capability_path),
            })
            closure = read_json(ledger_path)["closures"][0]
            history_hash = attempt_history_sha256(closure)
            start_attempt(ledger_path, "executor", {
                "attempt_history_sha256": history_hash, "strategy_family": "owned-consumer",
                "strategy_summary": "add owned consumer", "invocation_id": "fix-1",
                "fixer_identity": "luna-a",
            })
            dispatch = {"feature_run_id": "fr_closure"}
            common = {
                "feature_run_id": "fr_closure", "phase": "REVIEWING", "role": "code_fixer",
                "model": "gpt-5.6-terra", "reasoning": "medium",
                "closure_action": "fix", "closure_ledger_path": str(ledger_path),
                "closure_id": "executor", "receipt_id": "fix-1",
                "closure_strategy_family": "owned-consumer",
                "closure_attempt_history_sha256": history_hash,
                "cwd": str(artifact_dir),
                "capability_manifest_path": str(capability_path),
                "capability_manifest_sha256": sha256_file(capability_path),
            }
            fixer_prompt = artifact_dir / "fix.prompt.md"
            fixer_prompt.write_text("Fix the executor consumer.", encoding="utf-8")
            common["prompt_path"] = str(fixer_prompt)
            spec_one = artifact_dir / "fix-1.spec.json"
            atomic_write_json(spec_one, common)
            self.assertEqual(
                _validate_requested_spec(dispatch, artifact_dir, str(spec_one)), spec_one
            )
            self.assertTrue(fixer_prompt.read_text().startswith(EXECUTION_ENVIRONMENT_CONTEXT))
            missing = artifact_dir / "missing-ledger.spec.json"
            atomic_write_json(missing, {
                "feature_run_id": "fr_closure", "phase": "REVIEWING",
                "role": "code_fixer", "model": "gpt-5.6-terra", "reasoning": "medium",
            })
            with self.assertRaisesRegex(StateError, "missing closure_action"):
                _validate_requested_spec(dispatch, artifact_dir, str(missing))
            spec_two = artifact_dir / "fix-1-copy.spec.json"
            atomic_write_json(spec_two, common)
            batch = artifact_dir / "repair.batch.json"
            atomic_write_json(batch, {
                "protocol": "implement-v13-codex/invocation-batch/1",
                "invocations": [str(spec_one), str(spec_two)],
            })
            with self.assertRaisesRegex(StateError, "one closure group at a time"):
                _requested_specs(dispatch, artifact_dir, {
                    "status": "invoke", "invocation_spec_path": str(batch),
                })

    def test_phase_event_names_checkpoint_as_sole_phase_authority(self) -> None:
        output = io.StringIO()
        with redirect_stderr(output):
            signature = _emit_controller_phase({
                "phase": "IMPLEMENTING", "phase_detail": "workers_dispatch",
                "phase_state": "running", "state_revision": 12,
            })
        event = json.loads(output.getvalue().strip())
        self.assertEqual(signature, ("IMPLEMENTING", "workers_dispatch", "running", 12))
        self.assertEqual(event["phase_authority"], "durable_checkpoint")
        self.assertTrue(event["process_liveness_only"])

    def test_outer_controller_runs_batch_concurrently_with_one_resumed_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [{"index": 11, "description": "Q12", "status": "pending", "engine": "v13-codex"}],
                "current_index": 11,
                "started": "2026-07-21T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_broker", "fr_broker", "lease_broker"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue, base_worktree_path=base, coordinator_id="coordinator-broker",
                new_id=lambda _prefix: next(ids),
            )
            queue_path = base / "docs/development/serial_implementation_queue.json"
            atomic_write_json(queue_path, queue)
            dispatch["queue_path"] = str(queue_path)
            worktree = base / dispatch["worktree_path"]
            checkpoint = base / dispatch["checkpoint_path"]
            artifact_dir = base / dispatch["artifact_dir"]
            worktree.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            atomic_write_json(checkpoint, {
                "phase": "PLANNING", "phase_detail": "plan_validate",
                "phase_state": "ready", "state_revision": 1,
            })
            atomic_write_json(base / dispatch["transaction_path"], {"state": "prepared"})
            dispatch_path = artifact_dir / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)

            child_prompt = artifact_dir / "plan-correction.prompt.md"
            child_prompt.write_text("Correct the invalid plan.", encoding="utf-8")
            child_schema = artifact_dir / "plan-correction.schema.json"
            atomic_write_json(child_schema, {
                "type": "object", "additionalProperties": False,
                "required": ["protocol", "status"],
                "properties": {
                    "protocol": {"type": "string", "const": "test/plan-correction/1"},
                    "status": {"type": "string", "const": "corrected"},
                },
            })
            child_specs = []
            for role in ("source_binding", "necessity", "frame"):
                child_spec = artifact_dir / f"plan-review-{role}.spec.json"
                atomic_write_json(child_spec, {
                    "receipt_id": f"fr_broker:PLAN_REVIEW:review_dispatch:{role}:0:1",
                    "queue_run_id": "qr_broker", "feature_run_id": "fr_broker",
                    "phase": "PLAN_REVIEW", "phase_detail": "review_dispatch", "role": role,
                    "attempt": 1, "cwd": str(worktree), "prompt_path": str(child_prompt),
                    "schema_path": str(child_schema), "artifact_dir": str(artifact_dir),
                    "model": "gpt-5.6-sol", "reasoning": "medium", "sandbox": "read-only",
                    "wall_timeout_seconds": 60,
                    "expected": {"protocol": "test/plan-correction/1", "status": "corrected"},
                })
                child_specs.append(child_spec)
            batch = artifact_dir / "plan-review.batch.json"
            atomic_write_json(batch, {
                "protocol": "implement-v13-codex/invocation-batch/1",
                "invocations": [str(path) for path in child_specs],
            })
            overlap_log = artifact_dir / "child-overlap.jsonl"
            fake = base / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys, time\n"
                "if '--version' in sys.argv:\n print('codex-cli 0.test'); raise SystemExit(0)\n"
                "args=sys.argv[1:]; out=pathlib.Path(args[args.index('-o')+1]); prompt=sys.stdin.read()\n"
                "if os.environ.get('IMPLEMENT_V13_RUN_FEATURE_CHILD') == '1':\n"
                " previous=pathlib.Path(next(line.split('=',1)[1] for line in prompt.splitlines() if line.startswith('PREVIOUS_CHILD_RESULT_PATH=')))\n"
                " if not previous.exists():\n"
                f"  result={{'protocol':'implement-v13-codex/coordinator-turn/2','status':'invoke','summary':'request reviews','blocker':{{'blocker_class':'','reason':'','resume_condition':''}},'resume_token':'','invocation_spec_path':{str(batch)!r},'judgment_reason':None,'rollover_ack':None,'metrics':None}}\n"
                " else:\n"
                "  checkpoint=pathlib.Path('docs/development/current_implementation_checkpoint.json')\n"
                "  state=json.loads(checkpoint.read_text()); state.update(phase_state='blocked',state_revision=2); checkpoint.write_text(json.dumps(state))\n"
                "  result={'protocol':'implement-v13-codex/coordinator-turn/2','status':'blocked','summary':'test terminal','blocker':{'blocker_class':'test_terminal','reason':'test terminal','resume_condition':'none'},'resume_token':'resume-broker','invocation_spec_path':'','judgment_reason':None,'rollover_ack':None,'metrics':None}\n"
                "else:\n"
                f" with open({str(overlap_log)!r},'a') as log: log.write(json.dumps({{'event':'start','pid':os.getpid(),'at':time.time()}})+'\\n')\n"
                " time.sleep(0.25)\n"
                f" with open({str(overlap_log)!r},'a') as log: log.write(json.dumps({{'event':'end','pid':os.getpid(),'at':time.time()}})+'\\n')\n"
                " result={'protocol':'test/plan-correction/1','status':'corrected'}\n"
                "out.write_text(json.dumps(result))\n"
                "thread=('thread-coordinator' if os.environ.get('IMPLEMENT_V13_RUN_FEATURE_CHILD') == '1' else 'thread-'+out.stem)\n"
                "print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True)\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':len(prompt),'cached_input_tokens':0,'output_tokens':1}}),flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

            with patch("run_exec.shutil.which", return_value=str(fake)):
                settled = drive(dispatch_path)
            self.assertEqual(settled["status"], "blocked")
            for role in ("source_binding", "necessity", "frame"):
                receipt = json.loads((
                    artifact_dir / f"fr_broker-PLAN_REVIEW-review_dispatch-{role}-0-1.receipt.json"
                ).read_text())
                self.assertEqual(receipt["status"], "succeeded")
            child_result = json.loads((artifact_dir / "controller-child-result-000001.json").read_text())
            self.assertEqual(child_result["status"], "succeeded")
            self.assertEqual(len(child_result["invocations"]), 3)
            events = [json.loads(line) for line in overlap_log.read_text().splitlines()]
            starts = [item["at"] for item in events if item["event"] == "start"]
            ends = [item["at"] for item in events if item["event"] == "end"]
            self.assertEqual((len(starts), len(ends)), (3, 3))
            self.assertLess(max(starts), min(ends))
            coordinator_receipts = sorted(artifact_dir.glob("fr_broker-COORDINATOR-*.receipt.json"))
            self.assertEqual(len(coordinator_receipts), 2)
            coordinators = [json.loads(path.read_text()) for path in coordinator_receipts]
            self.assertEqual(coordinators[0]["thread_id"], coordinators[1]["thread_id"])
            self.assertIsNone(coordinators[0].get("resume_thread_id"))
            self.assertEqual(coordinators[1].get("resume_thread_id"), coordinators[0]["thread_id"])
            prompts = [artifact_dir / f"coordinator-turn-{turn}.prompt.md" for turn in (1, 2)]
            self.assertLess(prompts[1].stat().st_size, prompts[0].stat().st_size)
            for prompt_path in prompts[:1]:
                prompt_text = prompt_path.read_text()
                self.assertIn("PLAN_PATH=", prompt_text)
                self.assertIn("CHECKPOINT_PATH=", prompt_text)
                self.assertIn("EXECUTION_ENVIRONMENT=macOS BSD userland; shell=zsh", prompt_text)
                self.assertIn("optional rg discovery separately", prompt_text)
                self.assertNotIn("DISPATCH={", prompt_text)
                self.assertNotIn("CHECKPOINT={", prompt_text)
            self.assertIn("Do not reread the installed skill", prompts[1].read_text())
            coordinator_inputs = []
            for receipt in coordinators:
                events = [json.loads(line) for line in Path(receipt["stdout_path"]).read_text().splitlines()]
                coordinator_inputs.append(next(item["usage"]["input_tokens"] for item in events if item["type"] == "turn.completed"))
            self.assertLessEqual(coordinator_inputs[1], coordinator_inputs[0])

    def test_real_controller_path_reaches_terminal_queue_with_stub_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [{"index": 11, "description": "Q12", "status": "pending", "engine": "v13-codex"}],
                "current_index": 11,
                "started": "2026-07-21T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_e2e", "fr_e2e", "lease_e2e"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue,
                base_worktree_path=base,
                coordinator_id="coordinator-e2e",
                new_id=lambda _prefix: next(ids),
            )
            queue_path = base / "docs/development/serial_implementation_queue.json"
            atomic_write_json(queue_path, queue)
            dispatch["queue_path"] = str(queue_path)
            worktree = base / dispatch["worktree_path"]
            checkpoint = base / dispatch["checkpoint_path"]
            artifact_dir = base / dispatch["artifact_dir"]
            worktree.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            atomic_write_json(
                checkpoint,
                {
                    "phase": "PLAN_REVIEW",
                    "phase_detail": "revised_plan_validate",
                    "phase_state": "running",
                    "state_revision": 8,
                },
            )
            atomic_write_json(base / dispatch["transaction_path"], {"state": "prepared"})
            dispatch_path = artifact_dir / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)
            fake = base / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                " print('codex-cli 0.test')\n"
                " raise SystemExit(0)\n"
                "if os.environ.get('IMPLEMENT_V13_RUN_FEATURE_CHILD') != '1':\n"
                " raise SystemExit(91)\n"
                "args=sys.argv[1:]\n"
                "out=pathlib.Path(args[args.index('-o')+1])\n"
                "checkpoint=pathlib.Path('docs/development/current_implementation_checkpoint.json')\n"
                "state=json.loads(checkpoint.read_text())\n"
                "state.update(phase_state='blocked', state_revision=state['state_revision']+1)\n"
                "checkpoint.write_text(json.dumps(state))\n"
                "out.write_text(json.dumps({\n"
                " 'protocol':'implement-v13-codex/coordinator-turn/2',\n"
                " 'status':'blocked',\n"
                " 'summary':'stub unresolved conflict',\n"
                " 'blocker':{'blocker_class':'hard_contract_conflict','reason':'stub unresolved conflict','resume_condition':'supply decision'},\n"
                " 'resume_token':'resume-e2e','invocation_spec_path':'',\n"
                " 'judgment_reason':None,'rollover_ack':None,'metrics':None}))\n"
                "print(json.dumps({'type':'thread.started','thread_id':'thread-e2e'}), flush=True)\n"
                "print(json.dumps({'type':'turn.completed'}), flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

            with patch("run_exec.shutil.which", return_value=str(fake)):
                settled = drive(dispatch_path)

            self.assertEqual(settled["status"], "blocked")
            stored = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["features"][0]["status"], "blocked")
            receipts = list(artifact_dir.glob("fr_e2e-COORDINATOR-*.receipt.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["cwd"], str(worktree.resolve()))
            self.assertEqual(receipt["writable_roots"], [str(base)])
            self.assertTrue(receipt["controller_child"])
            add_dir = receipt["argv"].index("--add-dir")
            self.assertEqual(receipt["argv"][add_dir + 1], str(base))
            child = json.loads(
                (artifact_dir / "fr_e2e-COORDINATOR-drive-feature_coordinator-1-1.child.json").read_text()
            )
            self.assertEqual(child["environment"], {CONTROLLER_CHILD_ENV: "1"})

    def test_recursive_controller_entry_is_rejected_before_state_access(self) -> None:
        with patch.dict(os.environ, {CONTROLLER_CHILD_ENV: "1"}, clear=False), patch(
            "run_feature.read_json", side_effect=AssertionError("run state was read")
        ):
            with self.assertRaisesRegex(StateError, "recursive run_feature.py invocation"):
                main(["/does/not/exist.json"])

    def test_coordinator_block_output_atomically_blocks_checkpoint_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [{"index": 0, "description": "Q1", "status": "pending", "engine": "v13-codex"}],
                "current_index": 0,
                "started": "2026-07-22T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_block", "fr_block", "lease_block"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue,
                base_worktree_path=base,
                coordinator_id="coordinator-block",
                new_id=lambda _prefix: next(ids),
            )
            queue_path = base / "docs/development/serial_implementation_queue.json"
            atomic_write_json(queue_path, queue)
            dispatch["queue_path"] = str(queue_path)
            (base / dispatch["worktree_path"]).mkdir(parents=True)
            artifact_dir = base / dispatch["artifact_dir"]
            artifact_dir.mkdir(parents=True)
            checkpoint_path = base / dispatch["checkpoint_path"]
            atomic_write_json(checkpoint_path, {
                "phase": "REVIEWING",
                "phase_detail": "fix",
                "phase_state": "ready",
                "state_revision": 12,
                "blocked_history": [],
            })
            atomic_write_json(base / dispatch["transaction_path"], {"state": "prepared"})
            dispatch_path = artifact_dir / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)
            calls = 0

            def invoke(_dispatch, _checkpoint, _artifact, _turn, _thread):
                nonlocal calls
                calls += 1
                return ({
                    "protocol": "implement-v13-codex/coordinator-turn/2",
                    "status": "blocked",
                    "summary": "operator decision required",
                    "blocker": {
                        "blocker_class": "architectural_design_contradiction",
                        "reason": "immutable contracts conflict",
                        "resume_condition": "operator must reconcile the contracts",
                    },
                    "resume_token": "resume-design",
                    "invocation_spec_path": "",
                }, "thread-block")

            settled = drive(dispatch_path, invoke=invoke)
            self.assertEqual(settled["status"], "blocked")
            self.assertEqual(calls, 1)
            checkpoint = read_json(checkpoint_path)
            self.assertEqual(checkpoint["phase_state"], "blocked")
            self.assertEqual(checkpoint["state_revision"], 13)
            self.assertEqual(
                checkpoint["active_blocker"]["blocker_class"],
                "architectural_design_contradiction",
            )
            stored = read_json(queue_path)
            self.assertEqual(stored["features"][0]["status"], "blocked")

    def test_outer_block_settlement_is_idempotent_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [{"index": 0, "description": "debug", "status": "pending", "engine": "v13-codex"}],
                "current_index": 0,
                "started": "2026-07-21T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_idempotent", "fr_idempotent", "lease_idempotent"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue,
                base_worktree_path=base,
                coordinator_id="coordinator-idempotent",
                new_id=lambda _prefix: next(ids),
            )
            blocker = {
                "blocker_class": "debug_terminal",
                "reason": "already settled",
                "resume_condition": "none",
            }
            queue = serial_state.block_feature(
                queue,
                index=0,
                coordinator_id="coordinator-idempotent",
                lease_id="lease_idempotent",
                blocker=blocker,
                resume_token="resume-idempotent",
            )
            queue_path = base / "queue.json"
            atomic_write_json(queue_path, queue)
            settled = _settle_blocked(
                serial_state,
                queue_path,
                dispatch,
                blocker,
                "resume-idempotent",
            )
            self.assertEqual(settled["status"], "blocked")
            self.assertTrue(settled["already_settled"])

    def test_existing_feature_result_is_acknowledged_without_coordinator_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue_path, transaction_path, result_path, queue, _, _ = serial_fixture.terminal_fixture(base)
            feature = queue["features"][0]
            worktree = base / feature["worktree_path"]
            worktree.mkdir(parents=True, exist_ok=True)
            dispatch = {
                "queue_run_id": queue["queue_run_id"],
                "feature_run_id": feature["feature_run_id"],
                "feature_index": feature["index"],
                "base_worktree_path": str(base),
                "queue_path": str(queue_path),
                "worktree_path": feature["worktree_path"],
                "checkpoint_path": feature["checkpoint_path"],
                "artifact_dir": feature["artifact_dir"],
                "transaction_path": feature["transaction_path"],
                "feature_result_path": feature["feature_result_path"],
                "coordinator_id": feature["dispatch_lease"]["coordinator_id"],
                "lease_id": feature["dispatch_lease"]["lease_id"],
            }
            dispatch_path = base / feature["artifact_dir"] / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)

            def unexpected_invoke(*_args):
                raise AssertionError("coordinator turn launched after terminal feature result")

            settled = drive(dispatch_path, invoke=unexpected_invoke)
            self.assertEqual(settled["status"], "done")
            stored = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["features"][0]["status"], "done")
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
            self.assertEqual(transaction["state"], "dispatcher_ack")
            self.assertTrue(result_path.is_file())

    def test_blocked_checkpoint_is_atomically_settled_without_parent_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [
                    {
                        "index": 11,
                        "description": "Q12",
                        "status": "pending",
                        "engine": "v13-codex",
                    }
                ],
                "current_index": 11,
                "started": "2026-07-21T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_test", "fr_test", "lease_test"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue,
                base_worktree_path=base,
                coordinator_id="coordinator-test",
                new_id=lambda _prefix: next(ids),
            )
            queue_path = base / "docs/development/serial_implementation_queue.json"
            atomic_write_json(queue_path, queue)
            dispatch["queue_path"] = str(queue_path)
            worktree = base / dispatch["worktree_path"]
            checkpoint = base / dispatch["checkpoint_path"]
            artifact_dir = base / dispatch["artifact_dir"]
            transaction = base / dispatch["transaction_path"]
            result = base / dispatch["feature_result_path"]
            worktree.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            atomic_write_json(
                checkpoint,
                {
                    "queue_run_id": "qr_test",
                    "feature_run_id": "fr_test",
                    "feature_index": 11,
                    "phase": "PLAN_REVIEW",
                    "phase_detail": "revised_plan_validate",
                    "phase_state": "running",
                    "state_revision": 4,
                },
            )
            atomic_write_json(transaction, {"state": "prepared"})
            dispatch_path = artifact_dir / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)

            calls: list[int] = []

            def fake_invoke(*_args):
                calls.append(1)
                current = json.loads(checkpoint.read_text(encoding="utf-8"))
                current.update(phase_state="blocked", state_revision=5)
                atomic_write_json(checkpoint, current)
                return (
                    {
                        "protocol": "implement-v13-codex/coordinator-turn/2",
                        "status": "blocked",
                        "summary": "unresolved contract decision",
                        "blocker": {
                            "blocker_class": "hard_contract_conflict",
                            "reason": "revised plan retains an unresolved conflict",
                            "resume_condition": "record the missing decision",
                        },
                        "resume_token": "resume-fr-test",
                        "invocation_spec_path": "",
                    },
                    "thread-test",
                )

            settled = drive(dispatch_path, invoke=fake_invoke)
            self.assertEqual(settled["status"], "blocked")
            self.assertEqual(len(calls), 1)
            stored = json.loads(queue_path.read_text(encoding="utf-8"))
            feature = stored["features"][0]
            self.assertEqual(feature["status"], "blocked")
            self.assertEqual(feature["dispatch_lease"]["state"], "blocked")
            self.assertFalse(result.exists())

    def test_nonterminal_turn_uses_fresh_coordinator_without_operator_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [{"index": 11, "description": "Q12", "status": "pending", "engine": "v13-codex"}],
                "current_index": 11,
                "started": "2026-07-21T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_loop", "fr_loop", "lease_loop"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue,
                base_worktree_path=base,
                coordinator_id="coordinator-loop",
                new_id=lambda _prefix: next(ids),
            )
            queue_path = base / "docs/development/serial_implementation_queue.json"
            atomic_write_json(queue_path, queue)
            dispatch["queue_path"] = str(queue_path)
            worktree = base / dispatch["worktree_path"]
            checkpoint = base / dispatch["checkpoint_path"]
            artifact_dir = base / dispatch["artifact_dir"]
            worktree.mkdir(parents=True)
            artifact_dir.mkdir(parents=True)
            atomic_write_json(
                checkpoint,
                {
                    "phase": "PLAN_REVIEW",
                    "phase_detail": "review_collect",
                    "phase_state": "ready",
                    "state_revision": 1,
                },
            )
            atomic_write_json(base / dispatch["transaction_path"], {"state": "prepared"})
            dispatch_path = artifact_dir / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)
            resumes: list[str | None] = []

            def fake_invoke(_dispatch, _checkpoint, _artifacts, turn, resume_thread_id):
                resumes.append(resume_thread_id)
                if turn == 1:
                    current = json.loads(checkpoint.read_text(encoding="utf-8"))
                    current.update(phase_state="running", state_revision=2)
                    atomic_write_json(checkpoint, current)
                    return (
                        {
                            "protocol": "implement-v13-codex/coordinator-turn/2",
                            "status": "continue",
                            "summary": "review collected",
                            "blocker": {"blocker_class": "", "reason": "", "resume_condition": ""},
                            "resume_token": "",
                            "invocation_spec_path": "",
                        },
                        "thread-loop",
                    )
                current = json.loads(checkpoint.read_text(encoding="utf-8"))
                current.update(
                    phase_detail="revised_plan_validate",
                    phase_state="blocked",
                    state_revision=2,
                )
                atomic_write_json(checkpoint, current)
                return (
                    {
                        "protocol": "implement-v13-codex/coordinator-turn/2",
                        "status": "blocked",
                        "summary": "unresolved after revision",
                        "blocker": {
                            "blocker_class": "hard_contract_conflict",
                            "reason": "unresolved after revision",
                            "resume_condition": "supply decision",
                        },
                        "resume_token": "resume-loop",
                        "invocation_spec_path": "",
                    },
                    "thread-loop",
                )

            settled = drive(dispatch_path, invoke=fake_invoke)
            self.assertEqual(settled["status"], "blocked")
            self.assertEqual(resumes, [None, "thread-loop"])

    def test_coordinator_failure_settles_queue_instead_of_orphaning_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            queue = {
                "base_branch": "feature/base",
                "features": [{"index": 11, "description": "Q12", "status": "pending", "engine": "v13-codex"}],
                "current_index": 11,
                "started": "2026-07-21T00:00:00Z",
                "results": [],
                "state_revision": 0,
            }
            ids = iter(("qr_fail", "fr_fail", "lease_fail"))
            queue, dispatch = serial_state.prepare_dispatch(
                queue,
                base_worktree_path=base,
                coordinator_id="coordinator-fail",
                new_id=lambda _prefix: next(ids),
            )
            queue_path = base / "docs/development/serial_implementation_queue.json"
            atomic_write_json(queue_path, queue)
            dispatch["queue_path"] = str(queue_path)
            worktree = base / dispatch["worktree_path"]
            worktree.mkdir(parents=True)
            artifact_dir = base / dispatch["artifact_dir"]
            artifact_dir.mkdir(parents=True)
            atomic_write_json(
                base / dispatch["checkpoint_path"],
                {"phase": "IMPLEMENTING", "phase_detail": "workers_dispatch", "phase_state": "running", "state_revision": 3},
            )
            atomic_write_json(base / dispatch["transaction_path"], {"state": "prepared"})
            dispatch_path = artifact_dir / "dispatch.v1.json"
            atomic_write_json(dispatch_path, dispatch)

            def failed_invoke(*_args):
                raise StateError("model service rejected coordinator")

            settled = drive(dispatch_path, invoke=failed_invoke)
            self.assertEqual(settled["status"], "blocked")
            self.assertTrue(settled["resume_token"])
            stored = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["features"][0]["blocker_class"], "coordinator_process_failure")


if __name__ == "__main__":
    unittest.main()
