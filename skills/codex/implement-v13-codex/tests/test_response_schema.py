from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))

from response_schema import (  # noqa: E402
    COMPILER_VERSION,
    canonical_schema_hashes,
    compile_schema_file,
    compile_transport_schema,
    production_response_schema_paths,
    validate_provider_schema,
)
from run_exec import preflight_response_schema  # noqa: E402
from state_io import StateError  # noqa: E402


class ResponseSchemaTests(unittest.TestCase):
    def test_every_registered_production_schema_compiles_and_uses_production_preflight(self) -> None:
        observed = {}
        for path in production_response_schema_paths(PACKAGE):
            source, transport, hashes = compile_schema_file(path)
            validate_provider_schema(transport)
            production = preflight_response_schema(path)
            self.assertEqual(production["schema_compiler_version"], COMPILER_VERSION)
            self.assertEqual(
                production["schema_transport_sha256"],
                hashes["schema_transport_sha256"],
            )
            self.assertTrue(source)
            observed[path.name] = production
        self.assertIn("closure-test-result.schema.json", observed)
        self.assertIn("feature-coordinator-result.schema.json", observed)

    def test_compilation_is_deterministic_and_does_not_mutate_normative_schema(self) -> None:
        path = PACKAGE / "schemas" / "plan.schema.json"
        source = json.loads(path.read_text(encoding="utf-8"))
        original = copy.deepcopy(source)
        first = compile_transport_schema(source)
        second = compile_transport_schema(source)
        self.assertEqual(first, second)
        self.assertEqual(source, original)
        self.assertEqual(
            canonical_schema_hashes(source, first),
            canonical_schema_hashes(source, second),
        )

    def test_nested_provider_mutations_fail_closed(self) -> None:
        valid = {
            "type": "object",
            "additionalProperties": False,
            "required": ["nested"],
            "properties": {
                "nested": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                }
            },
        }
        mutations = []
        incomplete = copy.deepcopy(valid)
        incomplete["properties"]["nested"]["required"] = []
        mutations.append((incomplete, "required == properties"))
        open_object = copy.deepcopy(valid)
        open_object["properties"]["nested"]["additionalProperties"] = True
        mutations.append((open_object, "additionalProperties=false"))
        missing_items = copy.deepcopy(valid)
        missing_items["properties"]["nested"] = {"type": "array"}
        mutations.append((missing_items, "array items"))
        unsupported = copy.deepcopy(valid)
        unsupported["properties"]["nested"]["oneOf"] = [{"type": "string"}]
        mutations.append((unsupported, "unsupported"))
        malformed_any_of = copy.deepcopy(valid)
        malformed_any_of["properties"]["nested"] = {"anyOf": []}
        mutations.append((malformed_any_of, "nonempty anyOf"))
        for schema, message in mutations:
            with self.subTest(message=message):
                with self.assertRaisesRegex(StateError, message):
                    validate_provider_schema(schema)


if __name__ == "__main__":
    unittest.main()
