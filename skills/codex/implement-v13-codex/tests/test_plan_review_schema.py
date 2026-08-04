from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema


PACKAGE = Path(__file__).parents[1]


class PlanReviewSchemaTests(unittest.TestCase):
    def test_canonical_schema_accepts_review_and_declares_explicit_types(self) -> None:
        schema = json.loads(
            (PACKAGE / "schemas" / "plan-review.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(
            {
                "protocol": "implement-v13-codex/1",
                "task": "Q12",
                "reviewer": "frame",
                "findings": [],
            }
        )

        def assert_explicit_types(value: object) -> None:
            if isinstance(value, dict):
                if "const" in value or "enum" in value:
                    self.assertIn("type", value)
                for child in value.values():
                    assert_explicit_types(child)
            elif isinstance(value, list):
                for child in value:
                    assert_explicit_types(child)

        assert_explicit_types(schema)


if __name__ == "__main__":
    unittest.main()
