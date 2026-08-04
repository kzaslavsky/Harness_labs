from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
sys.path.insert(0, str(SCRIPTS))

from validate_artifact import validate  # noqa: E402
from render_plan import render  # noqa: E402


class PlanSemanticTests(unittest.TestCase):
    def test_plan_schema_avoids_rejected_composition_keywords(self) -> None:
        schema_text = (SCHEMAS / "plan.schema.json").read_text(encoding="utf-8")
        self.assertNotIn('"allOf"', schema_text)
        self.assertNotIn('"prefixItems"', schema_text)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "inputs.json"
        self.manifest.write_text(
            json.dumps({"inputs": [{"id": "rules", "sha256": "a" * 64, "role": "governing", "required": True}]}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _lens(self, lens_id: str, charge: str, reason: str, must_read: str) -> dict[str, object]:
        return {
            "id": lens_id,
            "charge": charge,
            "reason": reason,
            "must_read": [must_read],
        }

    def _mandatory_review_lenses(self) -> list[dict[str, object]]:
        return [
            self._lens(
                "l1_l2_contract_boundary",
                "Challenge layer ownership, dependency direction, and contract boundaries.",
                "The plan changes behavior across an L1/L2 boundary.",
                "docs/architecture/harness-contract.md",
            ),
            self._lens(
                "security_privacy_destructive_behavior",
                "Challenge trust boundaries, sensitive-data handling, and destructive operations.",
                "The mandatory review must test safety properties independently.",
                "AGENTS.md",
            ),
            self._lens(
                "correctness",
                "Challenge state invariants, edge cases, and failure handling.",
                "The mandatory review must try to refute behavioral correctness.",
                "schemas/feature-checkpoint.schema.json",
            ),
        ]

    def _integration_lens(self) -> dict[str, object]:
        return self._lens(
            "integration_consumer_compatibility",
            "Challenge callers, schemas, persistence, and backwards compatibility.",
            "The blast radius includes public artifacts consumed by other components.",
            "schemas/plan.schema.json",
        )

    def _ui_lens(self) -> dict[str, object]:
        return self._lens(
            "ui_accessibility",
            "Challenge keyboard access, semantic structure, and visual state feedback.",
            "The blast radius includes an interactive user interface.",
            "docs/development/ui-contract.md",
        )

    def _plan(self) -> dict[str, object]:
        return {
            "protocol": "implement-v13-codex/1",
            "task": "synthetic",
            "scope": {"in": ["x"], "out": []},
            "governing_contracts": ["rules"],
            "source_evidence": ["source.py:1"],
            "input_acknowledgements": [{"input_id": "rules", "sha256": "a" * 64, "role": "governing"}],
            "complexity": "low",
            "ui_impact": False,
            "runtime_contracts": ["import works"],
            "steps": [
                {"id": "s1", "title": "one", "effort": 1, "dependencies": [], "write_paths": ["a.py"], "targeted_tests": ["test a"]},
                {"id": "s2", "title": "two", "effort": 1, "dependencies": ["s1"], "write_paths": ["b.py"], "targeted_tests": ["test b"]},
            ],
            "task_dag": {"nodes": ["s1", "s2"], "edges": [{"from": "s1", "to": "s2"}]},
            "critical_path_effort": 2,
            "total_effort": 2,
            "critical_path_share": 1.0,
            "parallelization": {"recommended": False, "worker_groups": [], "shared_file_owner": None},
            "testing_strategy": ["unit"],
            "risks": [],
            "review_lenses": self._mandatory_review_lenses(),
        }

    def _write(self, plan: dict[str, object]) -> Path:
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def test_valid_plan_passes_schema_and_semantics(self) -> None:
        validate(self._write(self._plan()), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_cycle_and_effort_mismatch_block(self) -> None:
        plan = self._plan()
        plan["task_dag"] = {"nodes": ["s1", "s2"], "edges": [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s1"}]}
        with self.assertRaisesRegex(ValueError, "acyclic"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)
        plan = self._plan()
        plan["total_effort"] = 3
        with self.assertRaisesRegex(ValueError, "total_effort"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_missing_input_acknowledgement_blocks(self) -> None:
        plan = self._plan()
        plan["input_acknowledgements"] = []
        with self.assertRaisesRegex(ValueError, "acknowledgements"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_plan_accepts_zero_one_or_two_distinct_optional_review_lenses(self) -> None:
        optional_sets = [
            [],
            [self._integration_lens()],
            [self._integration_lens(), self._ui_lens()],
        ]
        for optional_lenses in optional_sets:
            with self.subTest(optional_count=len(optional_lenses)):
                plan = self._plan()
                plan["review_lenses"] = self._mandatory_review_lenses() + optional_lenses
                validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_plan_preserves_optional_review_lens_priority_order(self) -> None:
        plan = self._plan()
        plan["review_lenses"] = self._mandatory_review_lenses() + [self._integration_lens(), self._ui_lens()]
        path = self._write(plan)

        validate(path, SCHEMAS / "plan.schema.json", {}, self.manifest)

        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [lens["id"] for lens in persisted["review_lenses"]],
            [
                "l1_l2_contract_boundary",
                "security_privacy_destructive_behavior",
                "correctness",
                "integration_consumer_compatibility",
                "ui_accessibility",
            ],
        )

    def test_missing_mandatory_review_lens_blocks(self) -> None:
        plan = self._plan()
        plan["review_lenses"] = self._mandatory_review_lenses()[:2] + [self._integration_lens()]

        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_reordered_mandatory_review_lenses_block(self) -> None:
        plan = self._plan()
        mandatory = self._mandatory_review_lenses()
        plan["review_lenses"] = [mandatory[1], mandatory[0], mandatory[2]]

        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_mandatory_review_lens_charge_is_fixed(self) -> None:
        plan = self._plan()
        plan["review_lenses"][0]["charge"] = "Review architecture generally."

        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_review_lens_must_read_paths_are_unique(self) -> None:
        plan = self._plan()
        plan["review_lenses"][0]["must_read"] = ["AGENTS.md", "AGENTS.md"]

        with self.assertRaises(ValueError):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_duplicate_mandatory_review_lens_blocks(self) -> None:
        plan = self._plan()
        mandatory = self._mandatory_review_lenses()
        plan["review_lenses"] = [mandatory[0], mandatory[1], mandatory[1]]

        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_more_than_two_optional_review_lenses_blocks(self) -> None:
        plan = self._plan()
        third_optional = self._lens(
            "data_migration_recovery",
            "Challenge replay, rollback, and partial migration recovery.",
            "The blast radius includes durable data transformation.",
            "docs/architecture/recovery.md",
        )
        plan["review_lenses"] = self._mandatory_review_lenses() + [
            self._integration_lens(),
            self._ui_lens(),
            third_optional,
        ]

        with self.assertRaises(jsonschema.ValidationError):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_optional_lens_cannot_duplicate_mandatory_identity(self) -> None:
        plan = self._plan()
        duplicate = self._integration_lens()
        duplicate["id"] = "correctness"
        plan["review_lenses"] = self._mandatory_review_lenses() + [duplicate]

        with self.assertRaisesRegex(ValueError, "review lens IDs must be unique"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_optional_lens_cannot_restate_mandatory_charge_under_a_new_identity(self) -> None:
        plan = self._plan()
        duplicate = self._integration_lens()
        duplicate["charge"] = self._mandatory_review_lenses()[2]["charge"]
        plan["review_lenses"] = self._mandatory_review_lenses() + [duplicate]

        with self.assertRaisesRegex(ValueError, "materially distinct"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_optional_lenses_cannot_duplicate_each_other(self) -> None:
        plan = self._plan()
        first = self._integration_lens()
        duplicate = dict(first)
        plan["review_lenses"] = self._mandatory_review_lenses() + [first, duplicate]

        with self.assertRaisesRegex(ValueError, "review lens IDs must be unique"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_optional_lenses_cannot_restate_each_other_under_distinct_identities(self) -> None:
        plan = self._plan()
        first = self._integration_lens()
        duplicate = dict(first)
        duplicate["id"] = "release_compatibility"
        plan["review_lenses"] = self._mandatory_review_lenses() + [first, duplicate]

        with self.assertRaisesRegex(ValueError, "materially distinct"):
            validate(self._write(plan), SCHEMAS / "plan.schema.json", {}, self.manifest)

    def test_rendered_plan_preserves_review_lens_priority(self) -> None:
        plan = self._plan()
        plan["review_lenses"] = self._mandatory_review_lenses() + [self._integration_lens()]

        rendered = render(plan)

        positions = [rendered.index(f"`{lens['id']}`") for lens in plan["review_lenses"]]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
