from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))

from repair_gates import run_repair_gates  # noqa: E402
from repair_preflight import probe_role_capabilities, repository_identity  # noqa: E402
from review_closure import create_ledger, select_repair_batch  # noqa: E402
from state_io import atomic_write_json, read_json, sha256_file  # noqa: E402


class RepairGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        self.source = self.root / "src/controller.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.tests = []
        for name in ("authority", "consumer", "unrelated"):
            path = self.root / f"tests/test_{name}.py"
            path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            self.tests.append(path)
        self.ledger_path = self.root / "review-closure-ledger.v1.json"
        groups = [
            self._group(
                "authority",
                "fa",
                "reviewer_authority",
                "controller.dispatch",
                self.tests[0],
            ),
            self._group(
                "consumer",
                "fb",
                "reviewer_consumer",
                "controller.dispatch",
                self.tests[1],
                depends_on=["authority"],
            ),
            self._group(
                "unrelated",
                "fc",
                "reviewer_unrelated",
                "unrelated.module",
                self.tests[2],
            ),
        ]
        create_ledger(
            self.ledger_path,
            feature_run_id="fr_graph",
            groups=groups,
            repository_root=self.root,
            scheduler_policy={"max_ready_age": 2, "retry_penalty": 1},
        )
        ledger = read_json(self.ledger_path)
        probe_role_capabilities(
            repository_root=self.root,
            artifact_dir=self.root,
            feature_run_id="fr_graph",
            controller_package_digest="d" * 64,
        )
        self.capability_path = self.root / "capability-manifest.v2.json"
        for closure in ledger["closures"][:2]:
            closure["status"] = "ready_for_fix"
            closure["capability_manifest_path"] = str(self.capability_path)
            closure["capability_manifest_sha256"] = sha256_file(
                self.capability_path
            )
        atomic_write_json(self.ledger_path, ledger)
        self.batch = select_repair_batch(
            self.ledger_path,
            ["authority", "consumer"],
            ["controller.dispatch"],
        )
        self.batch_path = Path(self.batch["batch_path"])
        self.process_receipt = self.root / "fix.receipt.json"
        atomic_write_json(
            self.process_receipt,
            {
                "status": "succeeded",
                "receipt_id": "fix-1",
                "exit_code": 0,
                "timed_out": False,
                "pid": 101,
                "process_group_id": 101,
                "process_start_fingerprint": "101:started",
                "event_types": ["thread.started", "turn.completed"],
                "terminal_cause": {"class": "none"},
                "artifact_sha256": {
                    name: "a" * 64
                    for name in (
                        "prompt",
                        "schema",
                        "codex_executable",
                        "stdout",
                        "stderr",
                        "output",
                        "child_spec",
                        "exit",
                    )
                },
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _group(
        self,
        closure_id: str,
        fingerprint: str,
        reviewer: str,
        surface: str,
        test_source: Path,
        *,
        depends_on: list[str] | None = None,
    ) -> dict[str, object]:
        dependencies = depends_on or []
        edge_reasons = [
            {
                "dependency_id": dependency,
                "reason": "consumer uses the durable dispatch authority",
                "code_surfaces": ["controller.dispatch"],
                "test_nodes": [
                    f"tests/{test_source.name}::test_ok",
                    "tests/test_authority.py::test_ok",
                ],
            }
            for dependency in dependencies
        ]
        return {
            "closure_id": closure_id,
            "fingerprints": [fingerprint],
            "origin_reviewer": reviewer,
            "complexity": "implementation",
            "acceptance": [f"{closure_id} is correct"],
            "depends_on": dependencies,
            "write_surfaces": [surface],
            "read_surfaces": [],
            "source_bindings": [
                {
                    "surface": surface,
                    "path": "src/controller.py",
                    "sha256": sha256_file(self.source),
                }
            ],
            "immutable_test_nodes": [
                {
                    "node_id": f"tests/{test_source.name}::test_ok",
                    "source_path": f"tests/{test_source.name}",
                    "source_sha256": sha256_file(test_source),
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "covers_surfaces": [surface],
                }
            ],
            "dependency_edge_reasons": edge_reasons,
        }

    def _evidence(self) -> dict[str, object]:
        return {
            "protocol": "implement-v13-codex/repair-gate-input/1",
            "feature_run_id": "fr_graph",
            "batch_sha256": sha256_file(self.batch_path),
            "forbidden_access": {
                "observed_reads": ["src/controller.py"],
                "observed_selectors": ["controller_minted_capability"],
                "forbidden_reads": ["/private/global"],
                "forbidden_selectors": ["caller_payload"],
                "selector_contract": {
                    "caller_selectable": False,
                    "production_selectable": False,
                    "caller_claim_selectable": False,
                },
            },
            "output_bound": {
                "limit_bytes": 4096,
                "observed_bytes_before_communicate": 512,
                "bound_checked_before_communicate": True,
                "communicate_started_at_check": False,
            },
            "process_evidence": {
                "receipt_path": str(self.process_receipt),
                "receipt_sha256": sha256_file(self.process_receipt),
            },
            "production_sandbox": {
                "capability_manifest_path": str(self.capability_path),
                "capability_manifest_sha256": sha256_file(self.capability_path),
                "controller_package_digest": "d" * 64,
            },
            "dependency_regression": {"command_timeout_seconds": 10},
        }

    def test_all_early_gates_run_in_order_and_select_only_affected_tests(self) -> None:
        evidence_path = self.root / "gate-input.json"
        receipt_path = self.root / "gate-receipt.json"
        atomic_write_json(evidence_path, self._evidence())
        receipt = run_repair_gates(self.batch_path, evidence_path, receipt_path)
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["targeted_review_permitted"])
        self.assertEqual(
            [gate["gate_class"] for gate in receipt["gates"]],
            [
                "forbidden_access",
                "pre_communication_output_bound",
                "process_evidence",
                "capability_manifest",
                "production_certification",
            ],
        )
        self.assertEqual(
            receipt["selected_test_nodes"],
            [
                "tests/test_authority.py::test_ok",
                "tests/test_consumer.py::test_ok",
            ],
        )
        self.assertNotIn("tests/test_unrelated.py::test_ok", receipt["selected_test_nodes"])

    def test_forbidden_selector_stops_before_process_sandbox_or_regression(self) -> None:
        evidence = self._evidence()
        evidence["forbidden_access"]["observed_selectors"] = ["caller_payload"]  # type: ignore[index]
        evidence_path = self.root / "gate-input-failed.json"
        receipt_path = self.root / "gate-receipt-failed.json"
        atomic_write_json(evidence_path, evidence)
        calls = 0

        def never_run(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("regression command ran after an early gate failure")

        receipt = run_repair_gates(
            self.batch_path,
            evidence_path,
            receipt_path,
            command_runner=never_run,
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure_class"], "forbidden_access_failed")
        self.assertEqual(len(receipt["gates"]), 1)
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
