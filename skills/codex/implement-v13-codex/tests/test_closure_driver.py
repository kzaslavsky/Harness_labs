from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))

from closure_driver import (  # noqa: E402
    LEGAL_UNBOUND_ROUTES,
    PROTOCOL,
    continue_without_bound_program,
    route_routine_transition,
    run_closure_program,
)
from repair_preflight import probe_role_capabilities, repository_identity  # noqa: E402
from review_closure import create_ledger, record_test, select_repair_batch  # noqa: E402
from state_io import atomic_write_json, read_json, sha256_file  # noqa: E402


EFFECT_CONTRACT = {
    "protocol": "implement-v13-codex/repair-effect-contract/1",
    "must_persist": ["failure_checkpoint", "blocked_queue", "failure_summary", "failure_event"],
    "must_remain_absent": ["success_result", "success_receipt", "integration_artifact", "dispatcher_acknowledgement"],
    "must_remain_unchanged": ["base_git_state"],
}


class ClosureDriverTests(unittest.TestCase):
    def test_legal_unbound_routes_match_normative_text_and_do_not_block(self) -> None:
        expected = {"next_ready", "retry_fix", "redesign"}
        self.assertEqual(set(LEGAL_UNBOUND_ROUTES), expected)
        for reference in ("protocol.md", "phase-contracts.md"):
            text = (PACKAGE / "references" / reference).read_text(encoding="utf-8")
            normalized = " ".join(text.split())
            self.assertIn(
                "legal routes `next_ready`, `retry_fix`, and `redesign`",
                normalized,
            )
        for status in sorted(expected):
            with self.subTest(status=status):
                result = {
                    "protocol": PROTOCOL,
                    "status": "ready_for_fix",
                    "closure_id": "closure-a",
                    "receipts": ["targeted-review-a"],
                }
                route = {
                    "status": status,
                    "next_action": {
                        "status": "ready_for_fix",
                        "closure_id": "closure-a",
                    },
                    "coordinator_turns": 0,
                    "judgment_reason": None,
                }
                continued = continue_without_bound_program(result, route)
                self.assertEqual(continued["status"], "ready_for_fix")
                self.assertNotIn("blocker", continued)
                if status == "next_ready":
                    self.assertNotIn("coordinator_followup", continued)
                else:
                    self.assertEqual(
                        continued["coordinator_followup"],
                        {
                            "required": True,
                            "route": status,
                            "reason": (
                                "no pre-bound routine program for the selected "
                                "strategy route"
                            ),
                        },
                    )

    def test_unknown_unbound_routine_route_remains_blocking(self) -> None:
        result = {"protocol": PROTOCOL, "status": "ready_for_fix"}
        continued = continue_without_bound_program(
            result,
            {"status": "escalated_operator"},
        )
        self.assertEqual(continued["status"], "deterministic_blocked")
        self.assertEqual(
            continued["blocker"]["blocker_class"], "routine_program_missing"
        )

    def test_connected_two_closure_batch_uses_one_fix_gate_suite_and_independent_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "controller.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            test_paths = []
            for name in ("a", "b"):
                test_path = root / f"test_{name}.py"
                test_path.write_text("def test_node():\n    assert True\n", encoding="utf-8")
                test_paths.append(test_path)
            probe_role_capabilities(
                repository_root=root,
                artifact_dir=root,
                feature_run_id="fr_batch_driver",
                controller_package_digest="a" * 64,
            )
            capability = root / "capability-manifest.v2.json"

            def group(
                closure_id: str,
                reviewer: str,
                test_path: Path,
                dependencies: list[str],
            ) -> dict[str, object]:
                return {
                    "closure_id": closure_id,
                    "fingerprints": [f"f-{closure_id}"],
                    "origin_reviewer": reviewer,
                    "complexity": "implementation",
                    "acceptance": [closure_id],
                    "depends_on": dependencies,
                    "write_surfaces": ["controller.dispatch"],
                    "source_bindings": [{
                        "surface": "controller.dispatch",
                        "path": "controller.py",
                        "sha256": sha256_file(source),
                    }],
                    "immutable_test_nodes": [{
                        "node_id": f"{test_path.name}::test_node",
                        "source_path": test_path.name,
                        "source_sha256": sha256_file(test_path),
                        "command": ["python3", "-c", "raise SystemExit(0)"],
                        "covers_surfaces": ["controller.dispatch"],
                    }],
                    "dependency_edge_reasons": [
                        {
                            "dependency_id": dependency,
                            "reason": "shared controller authority",
                            "code_surfaces": ["controller.dispatch"],
                            "test_nodes": [
                                f"{test_path.name}::test_node",
                                "test_a.py::test_node",
                            ],
                        }
                        for dependency in dependencies
                    ],
                }

            ledger = root / "review-closure-ledger.v1.json"
            create_ledger(
                ledger,
                feature_run_id="fr_batch_driver",
                repository_root=root,
                scheduler_policy={"max_ready_age": 2, "retry_penalty": 1},
                groups=[
                    group("a", "reviewer-a", test_paths[0], []),
                    group("b", "reviewer-b", test_paths[1], ["a"]),
                ],
            )
            for closure_id, reviewer, test_path in (
                ("a", "reviewer-a", test_paths[0]),
                ("b", "reviewer-b", test_paths[1]),
            ):
                source_hash = sha256_file(test_path)
                record_test(
                    ledger,
                    closure_id,
                    {
                        "author_role": reviewer,
                        "author_receipt_id": f"test-{closure_id}",
                        "test_paths": [test_path.name],
                        "commands": [f"pytest -q {test_path.name}"],
                        "observed_failure": True,
                        "evidence": ["repair is absent"],
                        "effect_contract": EFFECT_CONTRACT,
                        "repository_root": str(root),
                        "repository_identity": repository_identity(root),
                        "test_node_id": f"{test_path.name}::test_node",
                        "test_source_path": test_path.name,
                        "test_source_sha256": source_hash,
                        "assertions": [{
                            "assertion_id": f"failure-{closure_id}",
                            "test_node_id": f"{test_path.name}::test_node",
                            "observation_source": "checkpoint",
                            "source_sha256": source_hash,
                            "governed_artifact": "checkpoint",
                            "effect": "failure_checkpoint",
                            "expected_disposition": "must_persist",
                        }],
                        "capability_manifest_path": str(capability),
                        "capability_manifest_sha256": sha256_file(capability),
                    },
                )
            batch = select_repair_batch(
                ledger, ["a", "b"], ["controller.dispatch"]
            )
            fix_spec = root / "fix.spec.json"
            review_a = root / "review-a.spec.json"
            review_b = root / "review-b.spec.json"
            atomic_write_json(fix_spec, {"receipt_id": "fix-batch", "role": "code_fixer"})
            atomic_write_json(review_a, {"receipt_id": "review-a", "role": "reviewer-a"})
            atomic_write_json(review_b, {"receipt_id": "review-b", "role": "reviewer-b"})
            gate_input = root / "gate-input.json"
            gate_receipt = root / "gate-receipt.json"
            atomic_write_json(gate_input, {"fixture": True})
            program = root / "closure-program.json"
            atomic_write_json(
                program,
                {
                    "protocol": PROTOCOL,
                    "closure_ledger_path": str(ledger),
                    "closure_id": "a",
                    "strategy_family": "shared-controller",
                    "strategy_summary": "repair shared controller once",
                    "fixer_identity": "luna-batch",
                    "specs": {
                        "fix": str(fix_spec),
                        "targeted_review": str(review_a),
                    },
                    "repair_batch_path": batch["batch_path"],
                    "batch_targeted_review_specs": {"b": str(review_b)},
                    "gate_evidence_path": str(gate_input),
                    "gate_receipt_path": str(gate_receipt),
                },
            )
            calls: list[str] = []

            def invoke(action, _spec_path):
                calls.append(action)
                output_path = root / f"{action.replace(':', '-')}.output.json"
                if action == "fix":
                    output = {"status": "passed"}
                    receipt_id = "fix-batch"
                elif action == "targeted_review":
                    output = {
                        "findings": [{"fingerprint": "f-a", "status": "fixed"}],
                        "regression_checks": {},
                        "evidence": ["a fixed"],
                    }
                    receipt_id = "review-a"
                else:
                    output = {
                        "findings": [{"fingerprint": "f-b", "status": "fixed"}],
                        "regression_checks": {},
                        "evidence": ["b fixed"],
                    }
                    receipt_id = "review-b"
                atomic_write_json(output_path, output)
                return {
                    "status": "succeeded",
                    "receipt_id": receipt_id,
                    "output_path": str(output_path),
                    "error": "",
                }

            def gates(_batch, _evidence, receipt_path):
                result = {
                    "protocol": "implement-v13-codex/repair-gates/1",
                    "status": "passed",
                    "failure_class": "",
                    "feature_run_id": "fr_batch_driver",
                    "batch_closure_ids": ["a", "b"],
                    "affected_closure_ids": ["a", "b"],
                    "dependency_graph_sha256": batch["dependency_graph_sha256"],
                    "selected_test_nodes": batch["selected_test_nodes"],
                    "gates": [
                        {"gate_class": gate_class, "status": "passed"}
                        for gate_class in (
                            "forbidden_access",
                            "pre_communication_output_bound",
                            "process_evidence",
                            "capability_manifest",
                            "production_certification",
                        )
                    ],
                }
                atomic_write_json(receipt_path, result)
                return result

            with patch("closure_driver.run_repair_gates", side_effect=gates):
                result = run_closure_program(program, root, invoke)
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["batch_closure_ids"], ["a", "b"])
            self.assertEqual(
                calls, ["fix", "targeted_review", "targeted_review:b"]
            )
            self.assertEqual(
                [item["status"] for item in read_json(ledger)["closures"]],
                ["closed", "closed"],
            )

    def test_retry_escalation_and_next_ready_are_deterministic_zero_turn_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger = root / "review-closure-ledger.v1.json"
            create_ledger(
                ledger,
                feature_run_id="fr_routes",
                groups=[
                    {
                        "closure_id": "a",
                        "fingerprints": ["fa"],
                        "origin_reviewer": "reviewer-a",
                        "complexity": "implementation",
                        "acceptance": ["a"],
                    },
                    {
                        "closure_id": "b",
                        "fingerprints": ["fb"],
                        "origin_reviewer": "reviewer-b",
                        "complexity": "implementation",
                        "acceptance": ["b"],
                    },
                ],
            )
            state = read_json(ledger)
            state["closures"][0]["status"] = "ready_for_fix"
            state["closures"][0]["attempts"] = [
                {
                    "status": "rejected",
                    "fixer_identity": "luna-a",
                    "strategy_family": "first",
                }
            ]
            atomic_write_json(ledger, state)
            retry = route_routine_transition(ledger, "a", "fix_rejected")
            self.assertEqual(retry["status"], "retry_fix")
            self.assertEqual(retry["coordinator_turns"], 0)

            state = read_json(ledger)
            state["closures"][0]["status"] = "escalation_required"
            atomic_write_json(ledger, state)
            escalated = route_routine_transition(
                ledger,
                "a",
                "escalation_required",
                escalation={
                    "action": "reassign",
                    "reason": "configured bounded reassign route",
                    "evidence": ["three distinct strategies rejected"],
                    "new_fixer_identity": "luna-b",
                },
            )
            self.assertEqual(escalated["status"], "escalated_reassign")
            self.assertEqual(escalated["coordinator_turns"], 0)
            self.assertIsNone(escalated["judgment_reason"])

    def test_closed_closure_returns_next_ready_without_requiring_another_program(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            test_source = root / "test_a.py"
            test_source.write_text("def test_a():\n    assert True\n", encoding="utf-8")
            probe_role_capabilities(
                repository_root=root,
                artifact_dir=root,
                feature_run_id="fr_next_ready",
                controller_package_digest="a" * 64,
            )
            capability_path = root / "capability-manifest.v2.json"
            ledger = root / "review-closure-ledger.v1.json"
            create_ledger(
                ledger,
                feature_run_id="fr_next_ready",
                groups=[
                    {
                        "closure_id": "a",
                        "fingerprints": ["fa"],
                        "origin_reviewer": "reviewer-a",
                        "complexity": "implementation",
                        "acceptance": ["a"],
                    },
                    {
                        "closure_id": "b",
                        "fingerprints": ["fb"],
                        "origin_reviewer": "reviewer-b",
                        "complexity": "implementation",
                        "acceptance": ["b"],
                    },
                ],
            )
            record_test(
                ledger,
                "a",
                {
                    "author_role": "reviewer-a",
                    "author_receipt_id": "test-a",
                    "test_paths": ["test_a.py"],
                    "commands": ["pytest -q test_a.py"],
                    "observed_failure": True,
                    "evidence": ["a is absent"],
                    "effect_contract": EFFECT_CONTRACT,
                    "repository_root": str(root),
                    "repository_identity": repository_identity(root),
                    "test_node_id": "test_a.py::test_a",
                    "test_source_path": "test_a.py",
                    "test_source_sha256": sha256_file(test_source),
                    "assertions": [{
                        "assertion_id": "a-persists",
                        "test_node_id": "test_a.py::test_a",
                        "observation_source": "checkpoint assertion",
                        "source_sha256": sha256_file(test_source),
                        "governed_artifact": "checkpoint",
                        "effect": "failure_checkpoint",
                        "expected_disposition": "must_persist",
                    }],
                    "capability_manifest_path": str(capability_path),
                    "capability_manifest_sha256": sha256_file(capability_path),
                },
            )
            fix_spec = root / "fix.spec.json"
            review_spec = root / "review.spec.json"
            atomic_write_json(fix_spec, {"receipt_id": "fix-a", "role": "code_fixer"})
            atomic_write_json(review_spec, {"receipt_id": "review-a", "role": "reviewer-a"})
            program = root / "closure-program.json"
            atomic_write_json(
                program,
                {
                    "protocol": PROTOCOL,
                    "closure_ledger_path": str(ledger),
                    "closure_id": "a",
                    "strategy_family": "close-a",
                    "strategy_summary": "close a and return to scheduling",
                    "fixer_identity": "terra-a",
                    "specs": {
                        "fix": str(fix_spec),
                        "targeted_review": str(review_spec),
                    },
                },
            )

            def invoke(action, _spec_path):
                output_path = root / f"{action}.output.json"
                output = (
                    {"status": "passed"}
                    if action == "fix"
                    else {
                        "findings": [{"fingerprint": "fa", "status": "fixed"}],
                        "regression_checks": {},
                        "evidence": ["a fixed"],
                    }
                )
                atomic_write_json(output_path, output)
                return {
                    "status": "succeeded",
                    "receipt_id": "fix-a" if action == "fix" else "review-a",
                    "output_path": str(output_path),
                    "error": "",
                }

            result = run_closure_program(program, root, invoke)
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["routine_route"]["status"], "next_ready")
            self.assertEqual(
                result["routine_route"]["next_action"]["closure_id"], "b"
            )
            self.assertEqual(read_json(ledger)["active_closure_id"], "b")

    def test_normal_architectural_path_runs_without_coordinator_between_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            test_source = root / "tests/test_authority.py"
            test_source.parent.mkdir(parents=True)
            test_source.write_text("def test_authority():\\n    assert True\\n")
            probe_role_capabilities(
                repository_root=root,
                artifact_dir=root,
                feature_run_id="fr_test",
                controller_package_digest="a" * 64,
            )
            capability_path = root / "capability-manifest.v2.json"
            ledger = root / "review-closure-ledger.v1.json"
            create_ledger(ledger, feature_run_id="fr_test", groups=[{
                "closure_id": "authority", "fingerprints": ["missing-authority"],
                "origin_reviewer": "code_reviewer_durable_orchestration",
                "complexity": "architectural", "acceptance": ["authority is durable"],
            }])
            roles = {
                "author_test": "code_reviewer_durable_orchestration",
                "design": "repair_designer",
                "design_review": "code_reviewer_durable_orchestration",
                "fix": "code_fixer",
                "targeted_review": "code_reviewer_durable_orchestration",
            }
            specs = {}
            for action, role in roles.items():
                path = root / f"{action}.spec.json"
                atomic_write_json(path, {
                    "receipt_id": f"receipt-{action}", "role": role,
                    "closure_action": action,
                    "capability_manifest_path": str(capability_path),
                    "capability_manifest_sha256": sha256_file(capability_path),
                })
                specs[action] = str(path)
            program = root / "closure-program.json"
            atomic_write_json(program, {
                "protocol": PROTOCOL, "closure_ledger_path": str(ledger), "closure_id": "authority",
                "strategy_family": "durable-authority", "strategy_summary": "add durable authority",
                "fixer_identity": "luna-fixer-a", "specs": specs,
            })
            calls = []

            def invoke(action, _spec_path):
                calls.append(action)
                outputs = {
                    "author_test": {
                        "test_paths": ["tests/test_authority.py"],
                        "commands": ["pytest tests/test_authority.py"],
                        "observed_failure": True,
                        "evidence": ["authority absent"],
                        "repository_root": str(root),
                        "repository_identity": repository_identity(root),
                        "test_node_id": "tests/test_authority.py::test_authority",
                        "test_source_path": "tests/test_authority.py",
                        "test_source_sha256": sha256_file(test_source),
                        "assertions": [{
                            "assertion_id": "authority-persists",
                            "test_node_id": "tests/test_authority.py::test_authority",
                            "observation_source": "checkpoint assertion",
                            "source_sha256": sha256_file(test_source),
                            "governed_artifact": "checkpoint",
                            "effect": "failure_checkpoint",
                            "expected_disposition": "must_persist",
                        }],
                        "effect_contract": EFFECT_CONTRACT,
                    },
                    "design": {"strategy_family": "durable-authority", "effect_contract": EFFECT_CONTRACT},
                    "design_review": {"approved": True, "evidence": ["design preserves ownership"], "effect_contract": EFFECT_CONTRACT},
                    "fix": {"status": "passed"},
                    "targeted_review": {"findings": [{"fingerprint": "missing-authority", "status": "fixed"}], "regression_checks": {}, "evidence": ["closure test passes"]},
                }
                output_path = root / f"{action}.output.json"
                atomic_write_json(output_path, outputs[action])
                return {"status": "succeeded", "receipt_id": f"receipt-{action}", "output_path": str(output_path), "error": ""}

            result = run_closure_program(program, root, invoke)
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["metrics"], {"deterministic_transitions": 5, "coordinator_turns_avoided": 4})
            self.assertEqual(calls, ["author_test", "design", "design_review", "fix", "targeted_review"])
            self.assertEqual(read_json(ledger)["closures"][0]["status"], "closed")
            self.assertEqual(read_json(Path(specs["fix"]))["closure_strategy_family"], "durable-authority")


if __name__ == "__main__":
    unittest.main()
