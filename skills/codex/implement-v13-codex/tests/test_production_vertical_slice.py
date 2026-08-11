from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE = Path(__file__).parents[1]
SCRIPTS = PACKAGE / "scripts"
SERIAL_SCRIPT = PACKAGE / "scripts" / "feature_queue_state.py"
START_PLANNING = SCRIPTS / "start_planning.py"
sys.path.insert(0, str(SCRIPTS))

from repair_preflight import probe_role_capabilities, repository_identity  # noqa: E402
from review_closure import create_ledger, select_repair_batch  # noqa: E402
from state_io import atomic_write_json, read_json, sha256_file  # noqa: E402


class ProductionVerticalSliceTests(unittest.TestCase):
    def test_group3_source_graph_to_early_gate_cli_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src").mkdir()
            (root / "tests").mkdir()
            source = root / "src/controller.py"
            test_source = root / "tests/test_controller.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            test_source.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            ledger_path = root / "review-closure-ledger.v1.json"
            create_ledger(
                ledger_path,
                feature_run_id="fr_vertical_group3",
                repository_root=root,
                scheduler_policy={"max_ready_age": 2, "retry_penalty": 1},
                groups=[
                    {
                        "closure_id": "controller",
                        "fingerprints": ["controller-finding"],
                        "origin_reviewer": "reviewer-controller",
                        "complexity": "implementation",
                        "acceptance": ["controller remains durable"],
                        "write_surfaces": ["controller.dispatch"],
                        "source_bindings": [{
                            "surface": "controller.dispatch",
                            "path": "src/controller.py",
                            "sha256": sha256_file(source),
                        }],
                        "immutable_test_nodes": [{
                            "node_id": "tests/test_controller.py::test_ok",
                            "source_path": "tests/test_controller.py",
                            "source_sha256": sha256_file(test_source),
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
                            "covers_surfaces": ["controller.dispatch"],
                        }],
                        "dependency_edge_reasons": [],
                    }
                ],
            )
            probe_role_capabilities(
                repository_root=root,
                artifact_dir=root,
                feature_run_id="fr_vertical_group3",
                controller_package_digest="d" * 64,
            )
            capability = root / "capability-manifest.v2.json"
            ledger = read_json(ledger_path)
            ledger["closures"][0]["status"] = "ready_for_fix"
            ledger["closures"][0]["capability_manifest_path"] = str(capability)
            ledger["closures"][0]["capability_manifest_sha256"] = sha256_file(
                capability
            )
            atomic_write_json(ledger_path, ledger)
            batch = select_repair_batch(
                ledger_path, ["controller"], ["controller.dispatch"]
            )
            receipt = root / "fix.receipt.json"
            atomic_write_json(
                receipt,
                {
                    "status": "succeeded",
                    "receipt_id": "fix-vertical",
                    "exit_code": 0,
                    "timed_out": False,
                    "pid": 10,
                    "process_group_id": 10,
                    "process_start_fingerprint": "10:started",
                    "event_types": ["thread.started", "turn.completed"],
                    "terminal_cause": {"class": "none"},
                    "artifact_sha256": {
                        name: "a" * 64
                        for name in (
                            "prompt", "schema", "codex_executable", "stdout",
                            "stderr", "output", "child_spec", "exit",
                        )
                    },
                },
            )
            gate_input = root / "gate-input.json"
            gate_receipt = root / "gate-receipt.json"
            atomic_write_json(
                gate_input,
                {
                    "protocol": "implement-v13-codex/repair-gate-input/1",
                    "feature_run_id": "fr_vertical_group3",
                    "batch_sha256": batch["batch_sha256"],
                    "forbidden_access": {
                        "observed_reads": ["src/controller.py"],
                        "observed_selectors": ["controller_minted"],
                        "forbidden_reads": ["/private/global"],
                        "forbidden_selectors": ["caller_payload"],
                        "selector_contract": {
                            "caller_selectable": False,
                            "production_selectable": False,
                            "caller_claim_selectable": False,
                        },
                    },
                    "output_bound": {
                        "limit_bytes": 2048,
                        "observed_bytes_before_communicate": 128,
                        "bound_checked_before_communicate": True,
                        "communicate_started_at_check": False,
                    },
                    "process_evidence": {
                        "receipt_path": str(receipt),
                        "receipt_sha256": sha256_file(receipt),
                    },
                    "production_sandbox": {
                        "capability_manifest_path": str(capability),
                        "capability_manifest_sha256": sha256_file(capability),
                        "controller_package_digest": "d" * 64,
                    },
                    "dependency_regression": {"command_timeout_seconds": 10},
                },
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "repair_gates.py"),
                    batch["batch_path"],
                    str(gate_input),
                    str(gate_receipt),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["targeted_review_permitted"])
            self.assertEqual(len(result["gates"]), 5)

    def test_planner_terminal_failure_blocks_queue_and_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main"], cwd=base, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=base, check=True)
            (base / "docs/development").mkdir(parents=True)
            (base / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            (base / "docs/development/NEXT_STEPS.md").write_text("Q12\n", encoding="utf-8")
            (base / "docs/development/INDEX.md").write_text("index\n", encoding="utf-8")
            queue_path = base / "docs/development/serial_implementation_queue.json"
            queue_path.write_text(json.dumps({
                "base_branch": "main",
                "features": [{"index": 11, "description": "Implement Q12", "status": "pending", "engine": "v13-codex"}],
                "current_index": 11, "started": "2026-07-21T00:00:00Z", "results": [], "state_revision": 0,
            }), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=base, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=base, check=True, capture_output=True)
            dispatch_path = base / "handoff/dispatch.v1.json"
            dispatched = subprocess.run([
                sys.executable, str(SERIAL_SCRIPT), "dispatch", str(queue_path),
                "--base-worktree-path", str(base), "--coordinator-id", "coordinator-failure-e2e",
                "--output", str(dispatch_path),
            ], check=False, capture_output=True, text=True, timeout=10)
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            dispatch = json.loads(dispatch_path.read_text())
            fake = base / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if '--version' in sys.argv:\n print('codex-cli 0.test'); raise SystemExit(0)\n"
                "print(json.dumps({'type':'thread.started','thread_id':'thread-planner-failure'}),flush=True)\n"
                "print(json.dumps({'type':'turn.failed','error':'injected planner failure'}),flush=True)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment["PATH"] = f"{base}{os.pathsep}{environment.get('PATH', '')}"
            lifecycle = subprocess.run(
                [sys.executable, str(START_PLANNING), str(dispatch_path)],
                check=False, capture_output=True, text=True, timeout=15, env=environment,
            )
            self.assertNotEqual(lifecycle.returncode, 0)
            queue = json.loads(queue_path.read_text())
            feature = queue["features"][0]
            self.assertEqual(feature["status"], "blocked")
            self.assertEqual(feature["dispatch_lease"]["state"], "blocked")
            checkpoint = json.loads((base / dispatch["checkpoint_path"]).read_text())
            self.assertEqual(checkpoint["phase_state"], "blocked")
            self.assertEqual(checkpoint["active_blocker"]["blocker_class"], "planner_process_failure")
            receipt = json.loads(next((base / dispatch["artifact_dir"]).glob("*planner-0-1.receipt.json")).read_text())
            self.assertEqual(receipt["status"], "failed")

    def test_dispatch_planner_coordinator_and_terminal_queue_without_operator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main"], cwd=base, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=base, check=True)
            (base / "docs/development").mkdir(parents=True)
            (base / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            (base / "docs/development/NEXT_STEPS.md").write_text("Q12\n", encoding="utf-8")
            (base / "docs/development/INDEX.md").write_text("index\n", encoding="utf-8")
            queue_path = base / "docs/development/serial_implementation_queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "base_branch": "main",
                        "features": [
                            {
                                "index": 11,
                                "description": "Implement Q12",
                                "status": "pending",
                                "engine": "v13-codex",
                            }
                        ],
                        "current_index": 11,
                        "started": "2026-07-21T00:00:00Z",
                        "results": [],
                        "state_revision": 0,
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=base, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=base, check=True, capture_output=True)
            dispatch_path = base / "handoff/dispatch.v1.json"
            dispatch_process = subprocess.run(
                [
                    sys.executable,
                    str(SERIAL_SCRIPT),
                    "dispatch",
                    str(queue_path),
                    "--base-worktree-path",
                    str(base),
                    "--coordinator-id",
                    "coordinator-e2e",
                    "--output",
                    str(dispatch_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(dispatch_process.returncode, 0, dispatch_process.stderr)
            dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            self.assertEqual(dispatch["queue_path"], str(queue_path))

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
                "steps": [{"id": "q12", "title": "Q12", "effort": 1, "dependencies": [], "write_paths": ["q12.py"], "targeted_tests": ["test"]}],
                "task_dag": {"nodes": ["q12"], "edges": []},
                "critical_path_effort": 1,
                "total_effort": 1,
                "critical_path_share": 1,
                "parallelization": {"recommended": False, "worker_groups": [], "shared_file_owner": None},
                "testing_strategy": ["test"],
                "risks": [],
                "review_lenses": [
                    {"id": "l1_l2_contract_boundary", "charge": "Challenge layer ownership, dependency direction, and contract boundaries.", "reason": "contract", "must_read": ["AGENTS.md"]},
                    {"id": "security_privacy_destructive_behavior", "charge": "Challenge trust boundaries, sensitive-data handling, and destructive operations.", "reason": "safety", "must_read": ["AGENTS.md"]},
                    {"id": "correctness", "charge": "Challenge state invariants, edge cases, and failure handling.", "reason": "correctness", "must_read": ["docs/development/NEXT_STEPS.md"]},
                ],
            }
            fake = base / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                " print('codex-cli 0.test'); raise SystemExit(0)\n"
                f"plan={plan!r}\n"
                "args=sys.argv[1:]\n"
                "out=pathlib.Path(args[args.index('-o')+1])\n"
                "schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())\n"
                "protocol=schema.get('properties',{}).get('protocol',{}).get('const')\n"
                "if protocol == 'implement-v13-codex/coordinator-turn/2':\n"
                " checkpoint=pathlib.Path('docs/development/current_implementation_checkpoint.json')\n"
                " state=json.loads(checkpoint.read_text())\n"
                " state.update(phase='PLAN_REVIEW',phase_detail='revised_plan_validate',phase_state='blocked',state_revision=state['state_revision']+1)\n"
                " checkpoint.write_text(json.dumps(state))\n"
                " output={'protocol':protocol,'status':'blocked','summary':'stub terminal','blocker':{'blocker_class':'hard_contract_conflict','reason':'stub terminal','resume_condition':'supply decision'},'resume_token':'resume-vertical','invocation_spec_path':'','judgment_reason':None,'rollover_ack':None,'metrics':None}\n"
                "else:\n"
                " output=plan\n"
                "out.write_text(json.dumps(output))\n"
                "print(json.dumps({'type':'thread.started','thread_id':'thread-'+('coordinator' if protocol else 'planner')}),flush=True)\n"
                "print(json.dumps({'type':'turn.completed'}),flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment["PATH"] = f"{base}{os.pathsep}{environment.get('PATH', '')}"
            lifecycle_process = subprocess.run(
                [sys.executable, str(START_PLANNING), str(dispatch_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                env=environment,
            )
            self.assertEqual(lifecycle_process.returncode, 0, lifecycle_process.stderr)
            lifecycle = json.loads(lifecycle_process.stdout)
            phase_events = [
                json.loads(line) for line in lifecycle_process.stderr.splitlines()
                if line.startswith("{") and json.loads(line).get("type") == "controller.phase"
            ]
            self.assertGreaterEqual(len(phase_events), 2)
            self.assertTrue(all(event["phase_authority"] == "durable_checkpoint" for event in phase_events))
            self.assertTrue(all(event["process_liveness_only"] for event in phase_events))
            self.assertIn(
                ("PLANNING", "plan_validate", "ready"),
                [(event["phase"], event["phase_detail"], event["phase_state"]) for event in phase_events],
            )
            self.assertIn(
                ("PLAN_REVIEW", "revised_plan_validate", "blocked"),
                [(event["phase"], event["phase_detail"], event["phase_state"]) for event in phase_events],
            )
            self.assertEqual(lifecycle["planner"]["next_phase_detail"], "plan_validate")
            settled = lifecycle["feature"]

            self.assertEqual(settled["status"], "blocked")
            final_queue = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(final_queue["features"][0]["status"], "blocked")
            self.assertEqual(final_queue["features"][0]["dispatch_lease"]["state"], "blocked")
            worktree = base / dispatch["worktree_path"]
            checkpoint = json.loads(
                (worktree / "docs/development/current_implementation_checkpoint.json").read_text()
            )
            self.assertEqual(checkpoint["phase_state"], "blocked")
            artifact_dir = base / dispatch["artifact_dir"]
            self.assertEqual(len(list(artifact_dir.glob("*COORDINATOR*.receipt.json"))), 1)


if __name__ == "__main__":
    unittest.main()
