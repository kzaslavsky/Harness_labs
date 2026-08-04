from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))

from repair_preflight import (  # noqa: E402
    effect_contract_sha256,
    execute_resolution_dataflow_probe,
    probe_role_capabilities,
    repository_identity,
    solve_effect_constraints,
    validate_assertion_effects,
    validate_capability_manifest,
    validate_resolution_dataflow,
)
from state_io import StateError, sha256_file  # noqa: E402


EFFECT_CONTRACT = {
    "protocol": "implement-v13-codex/repair-effect-contract/1",
    "must_persist": [
        "failure_checkpoint", "blocked_queue", "failure_summary", "failure_event",
    ],
    "must_remain_absent": [
        "success_result", "success_receipt", "integration_artifact",
        "dispatcher_acknowledgement",
    ],
    "must_remain_unchanged": ["base_git_state"],
}


class RepairPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "tests/test_subject.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("def test_subject():\\n    assert True\\n", encoding="utf-8")
        source_sha256 = sha256_file(self.source)
        self.assertion_map = {
            "protocol": "implement-v13-codex/repair-assertion-map/1",
            "feature_run_id": "fr_subject",
            "closure_id": "closure-subject",
            "repository_root": str(self.root),
            "repository_identity": repository_identity(self.root),
            "test": {
                "source_path": "tests/test_subject.py",
                "source_sha256": source_sha256,
                "node_id": "tests/test_subject.py::test_subject",
                "command": "pytest tests/test_subject.py::test_subject",
            },
            "effect_contract_sha256": effect_contract_sha256(EFFECT_CONTRACT),
            "assertions": [{
                "assertion_id": "checkpoint-persists",
                "test_node_id": "tests/test_subject.py::test_subject",
                "observation_source": "checkpoint assertion",
                "source_sha256": source_sha256,
                "governed_artifact": "checkpoint",
                "effect": "failure_checkpoint",
                "expected_disposition": "must_persist",
            }],
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _validate_map(self, assertion_map: dict[str, object]) -> dict[str, object]:
        return validate_assertion_effects(
            assertion_map,
            feature_run_id="fr_subject",
            closure_id="closure-subject",
            effect_contract=EFFECT_CONTRACT,
            test_paths=["tests/test_subject.py"],
            commands=["pytest tests/test_subject.py::test_subject"],
        )

    def test_assertion_map_solves_before_model_and_rejects_contradiction(self) -> None:
        validated = self._validate_map(self.assertion_map)
        self.assertEqual(validated["status"], "validated")
        solved = solve_effect_constraints(
            self.assertion_map, effect_contract=EFFECT_CONTRACT
        )
        self.assertTrue(solved["model_calls_permitted"])

        contradictory = copy.deepcopy(self.assertion_map)
        contradictory["assertions"].append({
            **contradictory["assertions"][0],
            "assertion_id": "checkpoint-unchanged",
            "expected_disposition": "must_remain_unchanged",
        })
        self._validate_map(contradictory)
        solved = solve_effect_constraints(
            contradictory, effect_contract=EFFECT_CONTRACT
        )
        self.assertEqual(solved["status"], "contradictory")
        self.assertFalse(solved["model_calls_permitted"])
        self.assertEqual(solved["conflicts"][0]["effect"], "failure_checkpoint")

    def test_assertion_map_rejects_unknown_effect_and_source_drift(self) -> None:
        unknown = copy.deepcopy(self.assertion_map)
        unknown["assertions"][0]["effect"] = "invented_effect"
        with self.assertRaisesRegex(StateError, "validation failed|unknown effect"):
            self._validate_map(unknown)
        self.source.write_text("changed\\n", encoding="utf-8")
        with self.assertRaisesRegex(StateError, "source hash mismatch"):
            self._validate_map(self.assertion_map)

    def test_assertion_map_accepts_exact_legacy_pytest_node_path(self) -> None:
        validated = validate_assertion_effects(
            self.assertion_map,
            feature_run_id="fr_subject",
            closure_id="closure-subject",
            effect_contract=EFFECT_CONTRACT,
            test_paths=["tests/test_subject.py::test_subject"],
            commands=["pytest tests/test_subject.py::test_subject"],
        )
        self.assertEqual(validated["status"], "validated")

    def test_assertion_map_rejects_different_node_in_same_legacy_file(self) -> None:
        with self.assertRaisesRegex(StateError, "active test path mismatch"):
            validate_assertion_effects(
                self.assertion_map,
                feature_run_id="fr_subject",
                closure_id="closure-subject",
                effect_contract=EFFECT_CONTRACT,
                test_paths=["tests/test_subject.py::different_test"],
                commands=["pytest tests/test_subject.py::test_subject"],
            )

    def test_real_capability_probe_certifies_and_injected_runner_cannot(self) -> None:
        manifest = probe_role_capabilities(
            repository_root=self.root,
            artifact_dir=self.root,
            feature_run_id="fr_subject",
            controller_package_digest="a" * 64,
        )
        manifest_path = self.root / "capability-manifest.v2.json"
        self.assertEqual(manifest["status"], "ready")
        validate_capability_manifest(
            manifest_path,
            sha256_file(manifest_path),
            repository_root=self.root,
            feature_run_id="fr_subject",
        )

        def simulated(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({
                    "repository_read": True,
                    "repository_write_denied": True,
                    "ephemeral_scratch_write": True,
                    "test_runner_scratch": True,
                }),
                stderr="",
            )

        simulated_manifest = probe_role_capabilities(
            repository_root=self.root,
            artifact_dir=self.root / "simulated",
            feature_run_id="fr_subject",
            controller_package_digest="a" * 64,
            runner=simulated,
        )
        simulated_path = self.root / "simulated/capability-manifest.v2.json"
        self.assertTrue(simulated_manifest["simulation_only"])
        self.assertEqual(
            simulated_manifest["status"], "external_capability_unavailable"
        )
        with self.assertRaisesRegex(StateError, "simulation-only"):
            validate_capability_manifest(
                simulated_path, sha256_file(simulated_path)
            )

    def test_generic_resolution_proves_exact_subject_and_fails_closed(self) -> None:
        subject = {
            "repository_identity": self.assertion_map["repository_identity"],
            "feature_run_id": "fr_subject",
            "closure_id": "closure-subject",
            "test_node_id": self.assertion_map["test"]["node_id"],
            "test_source_path": self.assertion_map["test"]["source_path"],
            "test_source_sha256": self.assertion_map["test"]["source_sha256"],
            "assertion_map_sha256": self._validate_map(self.assertion_map)[
                "assertion_map_sha256"
            ],
        }
        profile = {
            "protocol": "implement-v13-codex/operator-resolution-profile/1",
            "authority": "operator",
            "resolution_kind": "controller_owned_anonymous_capability",
            "active_subject": subject,
            "effect_contract": EFFECT_CONTRACT,
            "operator_authorization_sha256": "b" * 64,
            "capability": {
                "transport": "anonymous_pipe",
                "minting_authority": "controller_only",
                "controller_minted": True,
                "single_use": True,
                "role_visible": False,
                "caller_supplied": False,
                "caller_claim_selectable": False,
                "production_selectable": False,
                "fail_closed_on_absence": True,
                "fail_closed_on_reuse": True,
                "fail_closed_on_mismatch": True,
            },
            "dataflow_proof": {},
            "evidence": ["exact operator authorization"],
        }
        profile["dataflow_proof"] = execute_resolution_dataflow_probe(profile)
        validated = validate_resolution_dataflow(
            profile,
            repository_identity_sha256=subject["repository_identity"],
            feature_run_id=subject["feature_run_id"],
            closure_id=subject["closure_id"],
            test_node_id=subject["test_node_id"],
            test_source_path=subject["test_source_path"],
            test_source_sha256=subject["test_source_sha256"],
            assertion_map_sha256=subject["assertion_map_sha256"],
        )
        self.assertEqual(validated["status"], "validated")

        for field, value, message in (
            ("closure_id", "stale", "active subject mismatch"),
            ("test_source_sha256", "c" * 64, "active subject mismatch"),
        ):
            with self.subTest(field=field):
                kwargs = dict(subject)
                kwargs[field] = value
                with self.assertRaisesRegex(StateError, message):
                    validate_resolution_dataflow(
                        profile,
                        repository_identity_sha256=kwargs["repository_identity"],
                        feature_run_id=kwargs["feature_run_id"],
                        closure_id=kwargs["closure_id"],
                        test_node_id=kwargs["test_node_id"],
                        test_source_path=kwargs["test_source_path"],
                        test_source_sha256=kwargs["test_source_sha256"],
                        assertion_map_sha256=kwargs["assertion_map_sha256"],
                    )

    def test_generic_controller_has_no_repository_specific_recovery_literals(self) -> None:
        forbidden = ("testing_harness", "test_git_metadata_mutation_blocks_integration")
        for directory in (PACKAGE / "scripts", PACKAGE / "schemas"):
            for path in directory.iterdir():
                if path.is_file():
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    for literal in forbidden:
                        self.assertNotIn(literal, content, path)


if __name__ == "__main__":
    unittest.main()
