from __future__ import annotations

import tempfile
import unittest
import json
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from review_closure import (
    activate_post_resolution_attempt_budget,
    activate_post_resolution_design_budget,
    attempt_history_sha256,
    backfill_assertion_map,
    create_ledger,
    finish_attempt,
    next_action,
    record_design,
    record_design_review,
    record_escalation,
    record_review,
    reconcile_interrupted_attempts,
    record_test,
    resolve_legacy_assertion_conflict,
    resolve_design_contradiction,
    select_repair_batch,
    start_attempt,
    validate_invocation_spec,
    validate_pre_model_closure,
    _save,
)
from repair_preflight import (
    execute_resolution_dataflow_probe,
    probe_role_capabilities,
    repository_identity,
)
from state_io import StateError, atomic_write_json, read_json, sha256_file


PACKAGE = Path(__file__).resolve().parents[1]


def option_two_effect_contract() -> dict[str, object]:
    return {
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


class ReviewClosureTests(unittest.TestCase):
    def test_canonical_repair_schemas_omit_unsupported_unique_items(self) -> None:
        for name in (
            "closure-test-result.schema.json",
            "repair-design-result.schema.json",
            "repair-design-review-result.schema.json",
        ):
            schema = json.loads((PACKAGE / "schemas" / name).read_text(encoding="utf-8"))
            self.assertNotIn("uniqueItems", json.dumps(schema, sort_keys=True))

    def test_v2_ledger_declares_nullable_baselines_and_activation_history(self) -> None:
        import jsonschema

        schema = json.loads(
            (PACKAGE / "schemas/review-closure-ledger.schema.json").read_text()
        )
        jsonschema.Draft202012Validator(schema).validate(read_json(self.ledger))
        closure = read_json(self.ledger)["closures"][0]
        self.assertIsNone(closure["design_rejection_baseline"])
        self.assertIsNone(closure["attempt_rejection_baseline"])
        self.assertEqual(closure["budget_activation_history"], [])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.test_source = self.root / "tests/test_closure.py"
        self.test_source.parent.mkdir(parents=True)
        self.test_source.write_text("def test_closure():\\n    assert True\\n", encoding="utf-8")
        self.capability = probe_role_capabilities(
            repository_root=self.root,
            artifact_dir=self.root,
            feature_run_id="fr_test",
            controller_package_digest="a" * 64,
        )
        self.capability_path = self.root / "capability-manifest.v2.json"
        self.capability_sha256 = sha256_file(self.capability_path)
        self.scratch_directory = tempfile.TemporaryDirectory()
        self.scratch = Path(self.scratch_directory.name).resolve()
        self.ledger = self.root / "review-closure-ledger.v1.json"
        create_ledger(
            self.ledger,
            feature_run_id="fr_test",
            groups=[
                {
                    "closure_id": "executor",
                    "fingerprints": ["missing-executor"],
                    "origin_reviewer": "code_reviewer_correctness",
                    "complexity": "implementation",
                    "acceptance": ["dispatch is consumed and executes a cell"],
                },
                {
                    "closure_id": "cell-authority",
                    "fingerprints": ["terminal-without-cell-proof"],
                    "origin_reviewer": "code_reviewer_durable_orchestration",
                    "complexity": "architectural",
                    "acceptance": ["parent cannot complete before durable cells"],
                },
            ],
        )

    def tearDown(self) -> None:
        self.scratch_directory.cleanup()
        self.directory.cleanup()

    def _test_result(self, role: str, receipt: str) -> dict[str, object]:
        source_sha256 = sha256_file(self.test_source)
        node_id = "tests/test_closure.py::test_closure"
        return {
            "author_role": role,
            "author_receipt_id": receipt,
            "test_paths": ["tests/test_closure.py"],
            "commands": ["pytest tests/test_closure.py -q"],
            "observed_failure": True,
            "evidence": ["fails because the behavior is absent"],
            "effect_contract": option_two_effect_contract(),
            "repository_root": str(self.root),
            "repository_identity": repository_identity(self.root),
            "test_node_id": node_id,
            "test_source_path": "tests/test_closure.py",
            "test_source_sha256": source_sha256,
            "assertions": [{
                "assertion_id": "immutable-failure-checkpoint",
                "test_node_id": node_id,
                "observation_source": "assert failure checkpoint is persisted",
                "source_sha256": source_sha256,
                "governed_artifact": "checkpoint",
                "effect": "failure_checkpoint",
                "expected_disposition": "must_persist",
            }],
            "capability_manifest_path": str(self.capability_path),
            "capability_manifest_sha256": self.capability_sha256,
        }

    def _start(self, closure_id: str, family: str, invocation: str, fixer: str) -> None:
        closure = next(
            item for item in read_json(self.ledger)["closures"]
            if item["closure_id"] == closure_id
        )
        start_attempt(self.ledger, closure_id, {
            "attempt_history_sha256": attempt_history_sha256(closure),
            "strategy_family": family,
            "strategy_summary": f"use {family}",
            "invocation_id": invocation,
            "fixer_identity": fixer,
        })

    def _capability_spec(self) -> dict[str, object]:
        return {
            "cwd": str(self.root),
            "capability_manifest_path": str(self.capability_path),
            "capability_manifest_sha256": self.capability_sha256,
        }

    def test_legacy_pytest_command_normalizes_to_certified_runtime(self) -> None:
        result = self._test_result(
            "code_reviewer_correctness",
            "receipt-normalized",
        )
        result["commands"] = [
            "TMPDIR=/private/tmp PYTHONDONTWRITEBYTECODE=1 "
            "python3 -m pytest tests/test_closure.py -q"
        ]
        recorded = record_test(self.ledger, "executor", result)
        closure = recorded["closures"][0]
        command = closure["closure_test"]["commands"][0]
        self.assertEqual(
            command["argv"],
            [
                self.capability["certification_runtime"]["interpreter_path"],
                "-m",
                "pytest",
                "tests/test_closure.py",
                "-q",
            ],
        )
        self.assertEqual(
            command["certification_runtime_sha256"],
            read_json(Path(closure["assertion_map_path"]))["test"]["command"][
                "certification_runtime_sha256"
            ],
        )

    def test_unsafe_legacy_commands_fail_without_ledger_mutation(self) -> None:
        unsafe = (
            "pytest tests/test_closure.py | tee result",
            "pytest tests/test_closure.py > result",
            "pytest $(cat node)",
            "custom-runner tests/test_closure.py",
        )
        for index, command in enumerate(unsafe):
            with self.subTest(command=command):
                ledger_path = self.root / f"unsafe-{index}.json"
                create_ledger(
                    ledger_path,
                    feature_run_id="fr_test",
                    groups=[
                        {
                            "closure_id": "executor",
                            "fingerprints": [f"unsafe-{index}"],
                            "origin_reviewer": "code_reviewer_correctness",
                            "complexity": "implementation",
                            "acceptance": ["safe command"],
                        }
                    ],
                )
                before = read_json(ledger_path)
                result = self._test_result(
                    "code_reviewer_correctness",
                    f"unsafe-receipt-{index}",
                )
                result["commands"] = [command]
                with self.assertRaisesRegex(StateError, "legacy_unpinned"):
                    record_test(ledger_path, "executor", result)
                self.assertEqual(read_json(ledger_path), before)

    def _resolution(
        self,
        closure_id: str,
        *,
        kind: str = "controller_owned_anonymous_capability",
        suffix: str = "resolution",
    ) -> dict[str, object]:
        closure = next(
            item
            for item in read_json(self.ledger)["closures"]
            if item["closure_id"] == closure_id
        )
        assertion_map = read_json(Path(closure["assertion_map_path"]))
        profile = {
            "protocol": "implement-v13-codex/operator-resolution-profile/1",
            "authority": "operator",
            "resolution_kind": kind,
            "active_subject": {
                "repository_identity": assertion_map["repository_identity"],
                "feature_run_id": "fr_test",
                "closure_id": closure_id,
                "test_node_id": assertion_map["test"]["node_id"],
                "test_source_path": assertion_map["test"]["source_path"],
                "test_source_sha256": assertion_map["test"]["source_sha256"],
                "assertion_map_sha256": closure["assertion_map_sha256"],
            },
            "effect_contract": option_two_effect_contract(),
            "operator_authorization_sha256": hashlib.sha256(
                f"operator:{suffix}".encode()
            ).hexdigest(),
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
            "evidence": ["operator authorized exact active subject"],
        }
        profile["dataflow_proof"] = execute_resolution_dataflow_probe(profile)
        profile_dir = self.root / "operator-resolutions"
        profile_dir.mkdir(exist_ok=True)
        profile_path = profile_dir / f"{suffix}.json"
        atomic_write_json(profile_path, profile)
        return {
            "authority": "operator",
            "decision": kind,
            "effect_contract": option_two_effect_contract(),
            "operator_resolution_profile_path": str(profile_path),
            "operator_resolution_profile_sha256": sha256_file(profile_path),
        }

    def _finish(self, closure_id: str, suffix: str) -> None:
        finish_attempt(self.ledger, closure_id, {
            "result_path": f"/tmp/{suffix}.json",
            "result_sha256": "a" * 64,
        })

    def test_adversarial_separation_complexity_routing_history_and_regression(self) -> None:
        with self.assertRaisesRegex(StateError, "originating reviewer"):
            record_test(self.ledger, "executor", self._test_result("code_fixer", "test-bad"))
        record_test(
            self.ledger, "executor",
            self._test_result("code_reviewer_correctness", "test-executor"),
        )
        self.assertEqual(next_action(self.ledger)["status"], "ready_for_fix")

        self._start("executor", "owned-consumer", "fix-executor-1", "luna-a")
        prior_hash = read_json(self.ledger)["closures"][0]["attempts"][0]["prior_attempt_history_sha256"]
        with self.assertRaisesRegex(StateError, "Terra-medium implementation identity"):
            validate_invocation_spec({
                **self._capability_spec(),
                "closure_action": "fix", "closure_ledger_path": str(self.ledger),
                "closure_id": "executor", "role": "code_reviewer_correctness",
                "model": "gpt-5.6-sol", "reasoning": "medium", "receipt_id": "fix-executor-1",
                "closure_strategy_family": "owned-consumer",
                "closure_attempt_history_sha256": prior_hash,
            }, self.root)
        validate_invocation_spec({
            **self._capability_spec(),
            "closure_action": "fix", "closure_ledger_path": str(self.ledger),
            "closure_id": "executor", "role": "code_fixer", "phase": "REVIEWING",
            "model": "gpt-5.6-terra", "reasoning": "medium", "receipt_id": "fix-executor-1",
            "closure_strategy_family": "owned-consumer",
            "closure_attempt_history_sha256": prior_hash,
        }, self.root)
        self._finish("executor", "executor")
        record_review(self.ledger, "executor", {
            "reviewer_role": "code_reviewer_correctness",
            "reviewer_receipt_id": "review-executor-1",
            "finding_statuses": {"missing-executor": "fixed"},
            "regression_checks": {},
            "evidence": ["dispatch now executes a cell"],
        })
        self.assertEqual(next_action(self.ledger)["closure_id"], "cell-authority")

        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        self.assertEqual(next_action(self.ledger)["status"], "design_required")
        record_design(self.ledger, "cell-authority", {
            "designer_receipt_id": "design-cell-1", "strategy_family": "cell-backed-rollup",
            "result_path": "/tmp/design.json", "result_sha256": "b" * 64,
            "effect_contract": option_two_effect_contract(),
        })
        with self.assertRaisesRegex(StateError, "cannot approve its own"):
            record_design_review(self.ledger, "cell-authority", {
                "reviewer_role": "code_reviewer_durable_orchestration",
                "reviewer_receipt_id": "design-cell-1", "approved": True,
                "evidence": ["self approval"],
                "effect_contract": option_two_effect_contract(),
            })
        record_design_review(self.ledger, "cell-authority", {
            "reviewer_role": "code_reviewer_durable_orchestration",
            "reviewer_receipt_id": "review-design-cell-1", "approved": True,
            "evidence": ["authority and transaction boundary are explicit"],
            "effect_contract": option_two_effect_contract(),
        })

        for index, family in enumerate(("member-rollup", "receipt-only", "synthetic-ack"), 1):
            self._start("cell-authority", family, f"fix-cell-{index}", f"luna-{index}")
            self._finish("cell-authority", f"cell-{index}")
            record_review(self.ledger, "cell-authority", {
                "reviewer_role": "code_reviewer_durable_orchestration",
                "reviewer_receipt_id": f"review-cell-{index}",
                "finding_statuses": {"terminal-without-cell-proof": "not_fixed"},
                "regression_checks": {"executor": True},
                "evidence": [f"strategy {family} still lacks durable proof"],
            })
            if index == 1:
                with self.assertRaisesRegex(StateError, "repeats a previously rejected"):
                    self._start("cell-authority", family, "repeat-shallow", "luna-repeat")
        self.assertEqual(next_action(self.ledger)["status"], "escalation_required")
        with self.assertRaisesRegex(StateError, "only when ready_for_fix"):
            self._start("cell-authority", "fourth", "fix-cell-4", "luna-4")
        record_escalation(self.ledger, "cell-authority", {
            "action": "reassign", "new_fixer_identity": "luna-new",
            "reason": "three distinct strategies failed", "evidence": ["attempt history attached"],
        })
        self._start("cell-authority", "transactional-cell-authority", "fix-cell-4", "luna-new")
        self._finish("cell-authority", "cell-4")
        record_review(self.ledger, "cell-authority", {
            "reviewer_role": "code_reviewer_durable_orchestration",
            "reviewer_receipt_id": "review-cell-4",
            "finding_statuses": {"terminal-without-cell-proof": "fixed"},
            "regression_checks": {"executor": False},
            "evidence": ["cell proof fixed but executor closure test regressed"],
        })
        state = read_json(self.ledger)
        statuses = {item["closure_id"]: item["status"] for item in state["closures"]}
        self.assertEqual(statuses, {"executor": "closed", "cell-authority": "ready_for_fix"})
        self.assertEqual(state["active_closure_id"], "cell-authority")
        collision = next_action(self.ledger)["collision"]
        self.assertEqual(collision["closure_ids"], ["cell-authority", "executor"])
        self.assertTrue(Path(collision["packet_path"]).is_file())

        self._start(
            "cell-authority", "combined-custody", "fix-collision-1", "luna-collision"
        )
        self._finish("cell-authority", "collision-1")
        record_review(self.ledger, "cell-authority", {
            "reviewer_role": "code_reviewer_durable_orchestration",
            "reviewer_receipt_id": "review-collision-1",
            "finding_statuses": {"terminal-without-cell-proof": "fixed"},
            "regression_checks": {"executor": True},
            "evidence": ["both immutable closure tests pass together"],
        })
        state = read_json(self.ledger)
        self.assertEqual(
            {item["closure_id"]: item["status"] for item in state["closures"]},
            {"executor": "closed", "cell-authority": "closed"},
        )
        owner = next(item for item in state["closures"] if item["closure_id"] == "cell-authority")
        self.assertNotIn("active_collision", owner)
        self.assertEqual(owner["collision_history"][-1]["status"], "resolved")

        with self.assertRaisesRegex(StateError, "active closure group"):
            validate_invocation_spec({
                **self._capability_spec(),
                "closure_action": "targeted_review", "closure_ledger_path": str(self.ledger),
                "closure_id": "cell-authority", "role": "code_reviewer_durable_orchestration",
                "model": "gpt-5.6-sol", "reasoning": "medium",
            }, self.root)

    def test_repair_designer_is_canonical_schema_bound_and_read_only(self) -> None:
        record_test(
            self.ledger, "executor",
            self._test_result("code_reviewer_correctness", "test-executor"),
        )
        self._start("executor", "owned-consumer", "fix-executor", "luna-a")
        self._finish("executor", "executor")
        record_review(self.ledger, "executor", {
            "reviewer_role": "code_reviewer_correctness",
            "reviewer_receipt_id": "review-executor",
            "finding_statuses": {"missing-executor": "fixed"},
            "regression_checks": {},
            "evidence": ["executor closure passed"],
        })
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        policy = validate_pre_model_closure(
            self.ledger, "cell-authority"
        )["model_policy"]
        self.assertEqual(
            policy["designer"],
            {"model": "gpt-5.6-terra", "reasoning": "medium"},
        )
        self.assertEqual(
            policy["design_reviewer"],
            {"model": "gpt-5.6-sol", "reasoning": "medium"},
        )

        self.assertEqual(policy["quality_advantage"], "not_established")
        self.assertFalse(policy["benchmark_is_release_gate"])
        expected = {
            "protocol": "implement-v13-codex/repair-design/1",
            "queue_run_id": "sr_test",
            "feature_run_id": "fr_test",
            "phase": "REVIEWING",
            "phase_detail": "repair_design",
            "role": "repair_designer",
        }
        spec = {
            "closure_action": "design",
            "closure_ledger_path": str(self.ledger),
            "closure_id": "cell-authority",
            "queue_run_id": "sr_test",
            "feature_run_id": "fr_test",
            "phase": "REVIEWING",
            "phase_detail": "repair_design",
            "role": "repair_designer",
            "model": "gpt-5.6-terra",
            "reasoning": "medium",
            "schema_path": str(PACKAGE / "schemas" / "repair-design-result.schema.json"),
            "sandbox": "read-only",
            "expected": expected,
        }
        validate_invocation_spec({**self._capability_spec(), **spec}, self.root)

        mutable = dict(spec, sandbox="workspace-write", writable_roots=[str(self.root)])
        with self.assertRaisesRegex(StateError, "must be read-only"):
            validate_invocation_spec({**self._capability_spec(), **mutable}, self.root)

        generic = dict(spec, schema_path=str(PACKAGE / "schemas" / "role-result.schema.json"))
        with self.assertRaisesRegex(StateError, "canonical repair-design-result"):
            validate_invocation_spec({**self._capability_spec(), **generic}, self.root)

    def test_collision_repair_allows_two_fresh_attempts_then_requires_operator(self) -> None:
        self.ledger = self.root / "bounded-collision-ledger.json"
        create_ledger(
            self.ledger,
            feature_run_id="fr_test",
            groups=[
                {
                    "closure_id": "a",
                    "fingerprints": ["finding-a"],
                    "origin_reviewer": "code_reviewer_correctness",
                    "complexity": "implementation",
                    "acceptance": ["a remains correct"],
                },
                {
                    "closure_id": "b",
                    "fingerprints": ["finding-b"],
                    "origin_reviewer": "code_reviewer_durable_orchestration",
                    "complexity": "implementation",
                    "acceptance": ["b becomes correct"],
                },
            ],
        )
        record_test(
            self.ledger,
            "a",
            self._test_result("code_reviewer_correctness", "test-a"),
        )
        self._start("a", "fix-a", "fix-a", "fixer-a")
        self._finish("a", "a")
        record_review(self.ledger, "a", {
            "reviewer_role": "code_reviewer_correctness",
            "reviewer_receipt_id": "review-a",
            "finding_statuses": {"finding-a": "fixed"},
            "regression_checks": {},
            "evidence": ["a passes"],
        })
        record_test(
            self.ledger,
            "b",
            self._test_result("code_reviewer_durable_orchestration", "test-b"),
        )
        self._start("b", "fix-b", "fix-b", "fixer-b")
        self._finish("b", "b")
        record_review(self.ledger, "b", {
            "reviewer_role": "code_reviewer_durable_orchestration",
            "reviewer_receipt_id": "review-b",
            "finding_statuses": {"finding-b": "fixed"},
            "regression_checks": {"a": False},
            "evidence": ["b passes but a regressed"],
        })

        for index in (1, 2):
            self._start(
                "b",
                f"collision-strategy-{index}",
                f"fix-collision-{index}",
                f"fresh-fixer-{index}",
            )
            self._finish("b", f"collision-{index}")
            record_review(self.ledger, "b", {
                "reviewer_role": "code_reviewer_durable_orchestration",
                "reviewer_receipt_id": f"review-collision-{index}",
                "finding_statuses": {"finding-b": "not_fixed"},
                "regression_checks": {"a": True},
                "evidence": [f"combined strategy {index} failed"],
            })
            state = read_json(self.ledger)
            self.assertEqual(state["closures"][0]["status"], "closed")

        self.assertEqual(next_action(self.ledger)["status"], "escalation_required")
        with self.assertRaisesRegex(StateError, "requires operator"):
            record_escalation(self.ledger, "b", {
                "action": "reassign",
                "new_fixer_identity": "third-fixer",
                "reason": "try again",
                "evidence": ["two collision attempts failed"],
            })
        record_escalation(self.ledger, "b", {
            "action": "operator",
            "reason": "two bounded collision attempts failed",
            "evidence": ["complete collision history attached"],
        })
        self.assertEqual(read_json(self.ledger)["closures"][1]["status"], "blocked")

    def test_targeted_reviewer_uses_boolean_controller_owned_scratch_request(self) -> None:
        record_test(
            self.ledger,
            "executor",
            self._test_result("code_reviewer_correctness", "test-executor"),
        )
        self._start("executor", "owned-consumer", "fix-executor", "luna-a")
        self._finish("executor", "executor")
        spec = {
            **self._capability_spec(),
            "closure_action": "targeted_review",
            "closure_ledger_path": str(self.ledger),
            "closure_id": "executor",
            "role": "code_reviewer_correctness",
            "model": "gpt-5.6-sol",
            "reasoning": "medium",
            "receipt_id": "review-executor",
            "ephemeral_scratch": True,
        }

        validate_invocation_spec(spec, self.root)

        with self.assertRaisesRegex(StateError, "ephemeral_scratch=true"):
            validate_invocation_spec(
                {**spec, "ephemeral_scratch": str(self.scratch)}, self.root
            )
        with self.assertRaisesRegex(StateError, "requires receipt_id"):
            validate_invocation_spec({**spec, "receipt_id": ""}, self.root)

    def test_contradictory_design_review_is_rejected_before_fixer_attempt(self) -> None:
        record_test(
            self.ledger, "executor",
            self._test_result("code_reviewer_correctness", "test-executor"),
        )
        self._start("executor", "owned-consumer", "fix-executor", "luna-a")
        self._finish("executor", "executor")
        record_review(self.ledger, "executor", {
            "reviewer_role": "code_reviewer_correctness",
            "reviewer_receipt_id": "review-executor",
            "finding_statuses": {"missing-executor": "fixed"},
            "regression_checks": {},
            "evidence": ["executor closure passed"],
        })
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        record_design(self.ledger, "cell-authority", {
            "designer_receipt_id": "design-cell", "strategy_family": "failure-authority",
            "result_path": "/tmp/design.json", "result_sha256": "c" * 64,
            "effect_contract": option_two_effect_contract(),
        })
        contradictory = option_two_effect_contract()
        contradictory["must_persist"] = ["failure_event"]
        contradictory["must_remain_unchanged"] = [
            "failure_checkpoint", "blocked_queue", "failure_summary", "base_git_state",
        ]
        state = record_design_review(self.ledger, "cell-authority", {
            "reviewer_role": "code_reviewer_durable_orchestration",
            "reviewer_receipt_id": "review-design-cell", "approved": True,
            "evidence": ["incorrectly requires governed bytes to remain unchanged"],
            "effect_contract": contradictory,
        })
        closure = state["closures"][1]
        self.assertEqual(closure["status"], "design_required")
        self.assertFalse(closure["design"]["review"]["approved"])
        self.assertTrue(closure["design"]["review"]["requested_approval"])
        self.assertEqual(closure["attempts"], [])
        self.assertIn(
            "failure_checkpoint: authoritative=must_persist, candidate=must_remain_unchanged",
            closure["design"]["review"]["compatibility_conflicts"],
        )

    def test_three_rejected_designs_escalate_without_fixer_attempt(self) -> None:
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        for index in range(1, 4):
            record_design(self.ledger, "cell-authority", {
                "designer_receipt_id": f"design-cell-{index}",
                "strategy_family": f"design-family-{index}",
                "result_path": f"/tmp/design-{index}.json",
                "result_sha256": f"{index}" * 64,
                "effect_contract": option_two_effect_contract(),
            })
            state = record_design_review(self.ledger, "cell-authority", {
                "reviewer_role": "code_reviewer_durable_orchestration",
                "reviewer_receipt_id": f"review-design-cell-{index}",
                "approved": False,
                "evidence": [f"design {index} remains contradictory"],
                "effect_contract": option_two_effect_contract(),
            })
        closure = next(item for item in state["closures"] if item["closure_id"] == "cell-authority")
        self.assertEqual(closure["status"], "escalation_required")
        self.assertEqual(len(closure["design_rejections"]), 3)
        self.assertEqual(closure["attempts"], [])

    def test_operator_resolution_reopens_blocked_design_without_rewriting_test(self) -> None:
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        state = read_json(self.ledger)
        closure = state["closures"][1]
        closure["status"] = "blocked"
        closure["escalation_history"] = [{
            "action": "operator", "reason": "contract contradiction",
            "evidence": ["three independently rejected strategies"],
        }]
        closure["design_rejections"] = [
            {"rejection": index, "designer_receipt_id": f"old-design-{index}"}
            for index in range(1, 5)
        ]
        closure["attempts"] = [
            {"attempt": index, "status": "rejected", "strategy_family": f"old-{index}"}
            for index in range(1, 4)
        ]
        atomic_write_json(self.ledger, state)
        original_test = read_json(self.ledger)["closures"][1]["closure_test"]
        resolution = self._resolution("cell-authority")
        resolved = resolve_design_contradiction(
            self.ledger, "cell-authority", resolution
        )
        closure = resolved["closures"][1]
        self.assertEqual(closure["status"], "design_required")
        self.assertEqual(closure["closure_test"], original_test)
        self.assertEqual(closure["design_rejection_baseline"], 4)
        self.assertEqual(closure["attempt_rejection_baseline"], 3)
        self.assertEqual(len(closure["design_rejections"]), 4)
        self.assertEqual(len(closure["attempts"]), 3)
        self.assertEqual(len(closure["budget_activation_history"]), 1)
        with self.assertRaisesRegex(StateError, "already activated"):
            state = read_json(self.ledger)
            state["closures"][1]["status"] = "blocked"
            atomic_write_json(self.ledger, state)
            resolve_design_contradiction(
                self.ledger, "cell-authority", resolution
            )
        second = self._resolution("cell-authority", suffix="resolution-2")
        resolved_again = resolve_design_contradiction(
            self.ledger, "cell-authority", second
        )
        closure = resolved_again["closures"][1]
        self.assertEqual(len(closure["budget_activation_history"]), 2)
        self.assertEqual(len(closure["design_rejections"]), 4)
        self.assertEqual(len(closure["attempts"]), 3)

    def test_operator_can_reopen_only_test_cycle_after_blocked_legacy_map_review(self) -> None:
        record_test(
            self.ledger,
            "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "legacy-test"),
        )
        state = read_json(self.ledger)
        closure = state["closures"][1]
        original = json.loads(json.dumps(closure["closure_test"]))
        for field in (
            "assertion_map_path",
            "assertion_map_sha256",
            "capability_manifest_path",
            "capability_manifest_sha256",
            "effect_contract",
        ):
            closure["closure_test"].pop(field, None)
            closure.pop(field, None)
        closure["status"] = "ready_for_fix"
        closure["attempts"] = [{
            "attempt": 1,
            "status": "accepted",
            "strategy_family": "legacy-family",
        }]
        atomic_write_json(self.ledger, state)
        verification = {
            "protocol": "implement-v13-codex/assertion-map-verification/1",
            "queue_run_id": "qr_test",
            "feature_run_id": "fr_test",
            "role": "assertion_map_verifier",
            "status": "blocked",
            "assertion_map_path": str(self.root / "proposed.json"),
            "assertion_map_sha256": "b" * 64,
            "capability_manifest_path": str(self.capability_path),
            "capability_manifest_sha256": self.capability_sha256,
            "effect_contract": option_two_effect_contract(),
            "evidence": ["legacy test omits controller-owned failure persistence"],
        }
        verification_path = self.root / "blocked-verification.json"
        atomic_write_json(verification_path, verification)
        authorization = hashlib.sha256(b"operator supplemental test").hexdigest()
        resolved = resolve_legacy_assertion_conflict(
            self.ledger,
            "cell-authority",
            {
                "authority": "operator",
                "decision": "supplemental_immutable_test",
                "operator_authorization_sha256": authorization,
                "verification_result_path": str(verification_path),
                "verification_result_sha256": sha256_file(verification_path),
                "evidence": ["authorize an additive test for all nine effects"],
            },
        )
        closure = resolved["closures"][1]
        self.assertEqual(closure["status"], "test_required")
        self.assertIsNone(closure["closure_test"])
        self.assertEqual(closure["attempts"][0]["status"], "accepted")
        self.assertEqual(
            closure["superseded_closure_tests"][0]["closure_test"],
            {key: value for key, value in original.items() if key not in {
                "assertion_map_path", "assertion_map_sha256",
                "capability_manifest_path", "capability_manifest_sha256",
                "effect_contract",
            }},
        )
        self.assertEqual(
            closure["supplemental_test_resolution"]["effect_contract"],
            option_two_effect_contract(),
        )
        supplemental = self._test_result(
            "code_reviewer_durable_orchestration", "supplemental-test"
        )
        supplemental["observed_failure"] = False
        recorded = record_test(self.ledger, "cell-authority", supplemental)
        closure = recorded["closures"][1]
        self.assertTrue(closure["closure_test"]["supplemental"])
        self.assertEqual(closure["status"], "design_required")

        with self.assertRaisesRegex(StateError, "already resolved"):
            state = read_json(self.ledger)
            state["closures"][1]["closure_test"] = {
                key: value for key, value in original.items() if key not in {
                    "assertion_map_path", "assertion_map_sha256",
                    "capability_manifest_path", "capability_manifest_sha256",
                    "effect_contract",
                }
            }
            atomic_write_json(self.ledger, state)
            resolve_legacy_assertion_conflict(
                self.ledger,
                "cell-authority",
                {
                    "authority": "operator",
                    "decision": "supplemental_immutable_test",
                    "operator_authorization_sha256": authorization,
                    "verification_result_path": str(verification_path),
                    "verification_result_sha256": sha256_file(verification_path),
                    "evidence": ["duplicate attempt"],
                },
            )
    def test_generic_resolution_rejects_wrong_subject_and_caller_selectability(self) -> None:
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        state = read_json(self.ledger)
        state["closures"][1]["status"] = "blocked"
        state["closures"][1]["escalation_history"] = [{
            "action": "operator", "reason": "needs exact capability",
            "evidence": ["blocked evidence"],
        }]
        atomic_write_json(self.ledger, state)
        resolution = self._resolution("cell-authority", suffix="unsafe")
        profile_path = Path(str(resolution["operator_resolution_profile_path"]))
        profile = read_json(profile_path)
        profile["active_subject"]["closure_id"] = "stale-closure"
        atomic_write_json(profile_path, profile)
        resolution["operator_resolution_profile_sha256"] = sha256_file(profile_path)
        with self.assertRaisesRegex(StateError, "active subject mismatch"):
            resolve_design_contradiction(self.ledger, "cell-authority", resolution)

        safe = self._resolution("cell-authority", suffix="caller-selectable")
        profile_path = Path(str(safe["operator_resolution_profile_path"]))
        profile = read_json(profile_path)
        profile["capability"]["caller_claim_selectable"] = True
        atomic_write_json(profile_path, profile)
        safe["operator_resolution_profile_sha256"] = sha256_file(profile_path)
        with self.assertRaisesRegex(StateError, "validation failed|broadened"):
            resolve_design_contradiction(self.ledger, "cell-authority", safe)

    def test_post_resolution_attempt_budget_preserves_old_rejections(self) -> None:
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        state = read_json(self.ledger)
        closure = state["closures"][1]
        closure["status"] = "design_required"
        closure["attempts"] = [
            {"attempt": index, "status": "rejected", "strategy_family": f"old-{index}"}
            for index in range(1, 4)
        ]
        closure["contract_resolution_history"] = [{
            "authority": "operator",
            "decision": "controller_attested_digest_bound_test_compatibility_profile",
            "effect_contract": option_two_effect_contract(),
            "evidence": ["new governing contract"],
        }]
        from state_io import atomic_write_json
        atomic_write_json(self.ledger, state)

        activated = activate_post_resolution_attempt_budget(self.ledger, "cell-authority")
        closure = activated["closures"][1]
        self.assertEqual(closure["attempt_rejection_baseline"], 3)
        self.assertEqual(len(closure["attempts"]), 3)
        self.assertEqual(
            closure["contract_resolution_history"][-1]["attempt_rejection_baseline"],
            3,
        )

    def test_legacy_assertion_map_backfill_is_hash_bound_and_independently_verified(self) -> None:
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        state = read_json(self.ledger)
        closure = state["closures"][1]
        assertion_path = closure["assertion_map_path"]
        assertion_sha256 = closure["assertion_map_sha256"]
        closure["closure_test"].pop("assertion_map_path")
        closure["closure_test"].pop("assertion_map_sha256")
        closure["closure_test"].pop("capability_manifest_path")
        closure["closure_test"].pop("capability_manifest_sha256")
        closure["closure_test"].pop("effect_contract")
        closure["assertion_map_path"] = None
        closure["assertion_map_sha256"] = None
        closure["capability_manifest_path"] = None
        closure["capability_manifest_sha256"] = None
        state["protocol"] = "implement-v13-codex/review-closure-ledger/1"
        atomic_write_json(self.ledger, state)
        migrated = backfill_assertion_map(self.ledger, "cell-authority", {
            "assertion_map_path": assertion_path,
            "assertion_map_sha256": assertion_sha256,
            "effect_contract": option_two_effect_contract(),
            "capability_manifest_path": str(self.capability_path),
            "capability_manifest_sha256": self.capability_sha256,
            "independent_verifier_role": "migration_contract_reviewer",
            "verification_receipt_id": "verify-backfill-1",
        })
        closure = migrated["closures"][1]
        self.assertEqual(closure["assertion_map_sha256"], assertion_sha256)
        self.assertEqual(len(closure["assertion_backfill_history"]), 1)
        with self.assertRaisesRegex(StateError, "already bound"):
            backfill_assertion_map(self.ledger, "cell-authority", {
                "assertion_map_path": assertion_path,
                "assertion_map_sha256": assertion_sha256,
                "effect_contract": option_two_effect_contract(),
                "capability_manifest_path": str(self.capability_path),
                "capability_manifest_sha256": self.capability_sha256,
                "independent_verifier_role": "migration_contract_reviewer",
                "verification_receipt_id": "verify-backfill-2",
            })

    def test_post_resolution_budget_preserves_old_rejections_and_allows_fresh_design(self) -> None:
        record_test(
            self.ledger, "cell-authority",
            self._test_result("code_reviewer_durable_orchestration", "test-cell"),
        )
        state = read_json(self.ledger)
        closure = state["closures"][1]
        closure["status"] = "blocked"
        closure["design_rejections"] = [
            {"rejection": index, "designer_receipt_id": f"old-{index}"}
            for index in range(1, 5)
        ]
        closure["contract_resolution_history"] = [{
            "authority": "operator",
            "decision": "digest_bound_test_compatibility_profile",
            "effect_contract": option_two_effect_contract(),
            "evidence": ["operator supplied a new governing constraint"],
        }]
        closure["escalation_history"] = [{
            "action": "operator",
            "reason": "legacy controller immediately re-escalated",
            "evidence": ["fresh design could not be recorded"],
        }]
        from state_io import atomic_write_json
        atomic_write_json(self.ledger, state)

        activated = activate_post_resolution_design_budget(self.ledger, "cell-authority")
        closure = activated["closures"][1]
        self.assertEqual(closure["status"], "design_required")
        self.assertEqual(closure["design_rejection_baseline"], 4)
        self.assertEqual(len(closure["design_rejections"]), 4)
        designed = record_design(self.ledger, "cell-authority", {
            "designer_receipt_id": "fresh-design",
            "strategy_family": "digest-bound-profile",
            "result_path": "/tmp/fresh-design.json",
            "result_sha256": "b" * 64,
            "effect_contract": option_two_effect_contract(),
        })
        closure = designed["closures"][1]
        self.assertEqual(closure["status"], "design_review_required")
        self.assertEqual(len(closure["design_rejections"]), 4)

    def test_closure_dependencies_control_serial_order_and_reject_cycles(self) -> None:
        dependent = self.root / "dependent-ledger.json"
        create_ledger(
            dependent,
            feature_run_id="fr_dependent",
            groups=[
                {
                    "closure_id": "consumer",
                    "fingerprints": ["consumer-finding"],
                    "origin_reviewer": "code_reviewer_correctness",
                    "complexity": "implementation",
                    "acceptance": ["consumer is correct"],
                    "depends_on": ["authority"],
                    "related_closures": ["authority"],
                    "excluded_fingerprints": ["authority-finding"],
                },
                {
                    "closure_id": "authority",
                    "fingerprints": ["authority-finding"],
                    "origin_reviewer": "code_reviewer_durable_orchestration",
                    "complexity": "architectural",
                    "acceptance": ["authority is durable"],
                },
            ],
        )
        action = next_action(dependent)
        self.assertEqual(action["closure_id"], "authority")
        self.assertEqual(action["related_closures"], [])

        cyclic = self.root / "cyclic-ledger.json"
        with self.assertRaisesRegex(StateError, "contains a cycle"):
            create_ledger(
                cyclic,
                feature_run_id="fr_cycle",
                groups=[
                    {
                        "closure_id": "a", "fingerprints": ["fa"],
                        "origin_reviewer": "reviewer_a", "complexity": "implementation",
                        "acceptance": ["a"], "depends_on": ["b"],
                    },
                    {
                        "closure_id": "b", "fingerprints": ["fb"],
                        "origin_reviewer": "reviewer_b", "complexity": "implementation",
                        "acceptance": ["b"], "depends_on": ["a"],
                    },
                ],
            )

    def test_source_bound_scheduler_advances_unrelated_closure_around_failure(self) -> None:
        source = self.root / "controller.py"
        source.write_text("dispatch = True\n", encoding="utf-8")
        paths = []
        for name in ("failing", "descendant", "unrelated"):
            candidate = self.root / f"tests/test_{name}.py"
            candidate.write_text("def test_node():\n    assert True\n", encoding="utf-8")
            paths.append(candidate)

        def group(
            closure_id: str,
            fingerprint: str,
            reviewer: str,
            surface: str,
            test_path: Path,
            dependencies: list[str],
        ) -> dict[str, object]:
            return {
                "closure_id": closure_id,
                "fingerprints": [fingerprint],
                "origin_reviewer": reviewer,
                "complexity": "implementation",
                "acceptance": [closure_id],
                "depends_on": dependencies,
                "write_surfaces": [surface],
                "read_surfaces": [],
                "source_bindings": [{
                    "surface": surface,
                    "path": "controller.py",
                    "sha256": sha256_file(source),
                }],
                "immutable_test_nodes": [{
                    "node_id": f"tests/{test_path.name}::test_node",
                    "source_path": f"tests/{test_path.name}",
                    "source_sha256": sha256_file(test_path),
                    "command": ["python3", "-m", "unittest"],
                    "covers_surfaces": [surface],
                }],
                "dependency_edge_reasons": [
                    {
                        "dependency_id": dependency,
                        "reason": "dependent repair consumes the failing authority",
                        "code_surfaces": ["shared.dispatch"],
                        "test_nodes": [
                            f"tests/{test_path.name}::test_node",
                            "tests/test_failing.py::test_node",
                        ],
                    }
                    for dependency in dependencies
                ],
            }

        ledger_path = self.root / "source-bound-ledger.json"
        create_ledger(
            ledger_path,
            feature_run_id="fr_scheduler",
            repository_root=self.root,
            scheduler_policy={"max_ready_age": 2, "retry_penalty": 1},
            groups=[
                group("failing", "ff", "reviewer-a", "shared.dispatch", paths[0], []),
                group("descendant", "fd", "reviewer-b", "shared.dispatch", paths[1], ["failing"]),
                group("unrelated", "fu", "reviewer-c", "other.module", paths[2], []),
            ],
        )
        ledger = read_json(ledger_path)
        failing = ledger["closures"][0]
        failing["status"] = "fix_running"
        ledger["closures"][2]["status"] = "ready_for_fix"
        ledger["closures"][2]["ready_age"] = 2
        _save(ledger_path, ledger)
        pinned = read_json(ledger_path)
        self.assertEqual(pinned["active_closure_id"], "failing")
        failing = pinned["closures"][0]
        failing["status"] = "ready_for_fix"
        failing["attempts"].append({"status": "rejected"})
        _save(ledger_path, pinned)
        scheduled = read_json(ledger_path)
        self.assertEqual(scheduled["active_closure_id"], "unrelated")
        self.assertNotEqual(scheduled["active_closure_id"], "descendant")
        self.assertLessEqual(
            max(item["ready_age"] for item in scheduled["closures"]),
            scheduled["scheduler_policy"]["max_ready_age"],
        )
        self.assertEqual(
            scheduled["scheduling_history"][-1]["event"],
            "repair_starvation_reordered",
        )

    def test_multi_closure_batch_requires_connected_graph_union_and_reviewers(self) -> None:
        source = self.root / "batch.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        test_a = self.root / "tests/test_batch_a.py"
        test_b = self.root / "tests/test_batch_b.py"
        test_c = self.root / "tests/test_batch_c.py"
        for test in (test_a, test_b, test_c):
            test.write_text("def test_node():\n    assert True\n", encoding="utf-8")

        def item(
            closure_id: str,
            reviewer: str,
            surface: str,
            test: Path,
            dependencies: list[str],
        ) -> dict[str, object]:
            return {
                "closure_id": closure_id,
                "fingerprints": [f"f-{closure_id}"],
                "origin_reviewer": reviewer,
                "complexity": "implementation",
                "acceptance": [closure_id],
                "depends_on": dependencies,
                "write_surfaces": [surface],
                "source_bindings": [{
                    "surface": surface,
                    "path": "batch.py",
                    "sha256": sha256_file(source),
                }],
                "immutable_test_nodes": [{
                    "node_id": f"tests/{test.name}::test_node",
                    "source_path": f"tests/{test.name}",
                    "source_sha256": sha256_file(test),
                    "command": ["python3", "-c", "raise SystemExit(0)"],
                    "covers_surfaces": [surface],
                }],
                "dependency_edge_reasons": [
                    {
                        "dependency_id": dependency,
                        "reason": "shared controller repair",
                        "code_surfaces": ["shared"],
                        "test_nodes": [
                            f"tests/{test.name}::test_node",
                            "tests/test_batch_a.py::test_node",
                        ],
                    }
                    for dependency in dependencies
                ],
            }

        ledger_path = self.root / "batch-ledger.json"
        create_ledger(
            ledger_path,
            feature_run_id="fr_batch",
            repository_root=self.root,
            scheduler_policy={"max_ready_age": 3, "retry_penalty": 1},
            groups=[
                item("a", "reviewer-a", "shared", test_a, []),
                item("b", "reviewer-b", "shared", test_b, ["a"]),
                item("c", "reviewer-c", "disconnected", test_c, []),
            ],
        )
        probe_role_capabilities(
            repository_root=self.root,
            artifact_dir=self.root,
            feature_run_id="fr_batch",
            controller_package_digest="a" * 64,
        )
        batch_capability = self.root / "capability-manifest.v2.json"
        ledger = read_json(ledger_path)
        for closure in ledger["closures"]:
            closure["status"] = "ready_for_fix"
            closure["capability_manifest_path"] = str(batch_capability)
            closure["capability_manifest_sha256"] = sha256_file(batch_capability)
        atomic_write_json(ledger_path, ledger)
        batch = select_repair_batch(ledger_path, ["a", "b"], ["shared"])
        self.assertEqual(batch["closure_ids"], ["a", "b"])
        self.assertEqual(batch["independent_reviewers"], ["reviewer-a", "reviewer-b"])
        self.assertEqual(
            batch["selected_test_nodes"],
            [
                "tests/test_batch_a.py::test_node",
                "tests/test_batch_b.py::test_node",
            ],
        )
        self.assertEqual(
            [command["argv"] for command in batch["selected_commands"]],
            [
                ["python3", "-c", "raise SystemExit(0)"],
                ["python3", "-c", "raise SystemExit(0)"],
            ],
        )
        self.assertTrue(
            all(
                command["protocol"] == "implement-v13-codex/test-command/1"
                for command in batch["selected_commands"]
            )
        )
        graph = read_json(Path(batch["dependency_graph_path"]))
        self.assertEqual(
            graph["protocol"],
            "implement-v13-codex/repair-dependency-graph/2",
        )
        self.assertEqual(
            [
                test["command"]
                for closure in graph["closures"][:2]
                for test in closure["immutable_test_nodes"]
            ],
            batch["selected_commands"],
        )
        with self.assertRaisesRegex(StateError, "disconnected"):
            select_repair_batch(
                ledger_path,
                ["a", "c"],
                ["disconnected", "shared"],
            )
        with self.assertRaisesRegex(StateError, "union write set"):
            select_repair_batch(ledger_path, ["a", "b"], ["wrong"])

    def test_interruption_reconciles_complete_shared_invocation_atomically(self) -> None:
        ledger_path = self.root / "interrupted-ledger.json"
        create_ledger(
            ledger_path,
            feature_run_id="fr_test",
            groups=[
                {
                    "closure_id": "a",
                    "fingerprints": ["fa"],
                    "origin_reviewer": "reviewer-a",
                    "complexity": "implementation",
                    "acceptance": ["a fixed"],
                },
                {
                    "closure_id": "b",
                    "fingerprints": ["fb"],
                    "origin_reviewer": "reviewer-b",
                    "complexity": "implementation",
                    "acceptance": ["b fixed"],
                },
            ],
        )
        for closure_id, role, receipt_id in (
            ("a", "reviewer-a", "test-a"),
            ("b", "reviewer-b", "test-b"),
        ):
            result = self._test_result(role, receipt_id)
            record_test(ledger_path, closure_id, result)
            closure = next(
                item
                for item in read_json(ledger_path)["closures"]
                if item["closure_id"] == closure_id
            )
            start_attempt(
                ledger_path,
                closure_id,
                {
                    "attempt_history_sha256": attempt_history_sha256(closure),
                    "strategy_family": f"family-{closure_id}",
                    "strategy_summary": f"repair {closure_id}",
                    "invocation_id": "shared-fixer-receipt",
                    "fixer_identity": "fixer",
                },
            )
        before = read_json(ledger_path)
        revision = before["state_revision"]
        prior_hashes = [
            closure["attempts"][-1]["prior_attempt_history_sha256"]
            for closure in before["closures"]
        ]
        receipt_path = self.root / "shared-fixer.receipt.json"
        atomic_write_json(
            receipt_path,
            {
                "receipt_id": "shared-fixer-receipt",
                "status": "failed",
                "interruption": {
                    "marker": True,
                    "termination_status": "verified",
                    "supervisor_reaped": True,
                    "available_artifact_sha256": {},
                },
            },
        )
        reconciled = reconcile_interrupted_attempts(
            ledger_path,
            {
                "receipt_path": str(receipt_path),
                "receipt_sha256": sha256_file(receipt_path),
                "closure_ids": ["a", "b"],
            },
        )
        self.assertEqual(reconciled["state_revision"], revision + 1)
        self.assertEqual(
            [closure["status"] for closure in reconciled["closures"]],
            ["ready_for_fix", "ready_for_fix"],
        )
        self.assertEqual(
            [closure["attempts"][-1]["status"] for closure in reconciled["closures"]],
            ["interrupted", "interrupted"],
        )
        self.assertEqual(
            [
                closure["attempts"][-1]["prior_attempt_history_sha256"]
                for closure in reconciled["closures"]
            ],
            prior_hashes,
        )
        self.assertFalse(
            any(
                attempt["status"] == "rejected"
                for closure in reconciled["closures"]
                for attempt in closure["attempts"]
            )
        )
        stable = json.loads(json.dumps(reconciled))
        with self.assertRaisesRegex(StateError, "complete ordered repair batch"):
            reconcile_interrupted_attempts(
                ledger_path,
                {
                    "receipt_path": str(receipt_path),
                    "receipt_sha256": sha256_file(receipt_path),
                    "closure_ids": ["a"],
                },
            )
        self.assertEqual(read_json(ledger_path), stable)


if __name__ == "__main__":
    unittest.main()
