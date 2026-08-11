"""Contract tests for descriptor, liveness, and catalog snapshot fixtures."""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
FIXTURES = ROOT / "tests" / "fixtures" / "run_catalog"


class SchemaValidationError(ValueError):
    """Raised when a fixture violates the closed schema subset used here."""


class ClosedSchemaValidator:
    """Small dependency-free validator for these repository-owned contracts."""

    def __init__(self, schema: dict[str, object]) -> None:
        self.schema = schema

    def validate(self, value: object) -> None:
        self._validate(value, self.schema, "$")

    def _resolve(self, reference: str) -> dict[str, object]:
        target: object = self.schema
        for part in reference.removeprefix("#/").split("/"):
            if not isinstance(target, dict):
                raise SchemaValidationError(f"invalid reference {reference}")
            target = target[part]
        if not isinstance(target, dict):
            raise SchemaValidationError(f"invalid reference target {reference}")
        return target

    def _valid(self, value: object, schema: dict[str, object]) -> bool:
        try:
            self._validate(value, schema, "$")
        except SchemaValidationError:
            return False
        return True

    def _validate(self, value: object, schema: dict[str, object], path: str) -> None:
        if "$ref" in schema:
            self._validate(value, self._resolve(str(schema["$ref"])), path)
            return
        negated = schema.get("not")
        if isinstance(negated, dict) and self._valid(value, negated):
            raise SchemaValidationError(f"{path}: matches a prohibited schema")
        for part in schema.get("allOf", []):
            if not isinstance(part, dict):
                raise SchemaValidationError(f"{path}: invalid allOf schema")
            self._validate(value, part, path)
        condition = schema.get("if")
        if isinstance(condition, dict) and self._valid(value, condition):
            consequence = schema.get("then")
            if isinstance(consequence, dict):
                self._validate(value, consequence, path)
        if "const" in schema and value != schema["const"]:
            raise SchemaValidationError(f"{path}: expected {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise SchemaValidationError(f"{path}: value is outside its enum")
        expected = schema.get("type")
        types = expected if isinstance(expected, list) else [expected]
        if expected is not None and not any(self._matches(value, item) for item in types):
            raise SchemaValidationError(f"{path}: wrong type")
        if isinstance(value, dict):
            required = schema.get("required", [])
            for name in required:
                if name not in value:
                    raise SchemaValidationError(f"{path}: missing {name}")
            dependent_required = schema.get("dependentRequired", {})
            if isinstance(dependent_required, dict):
                for name, dependencies in dependent_required.items():
                    if name in value:
                        if not isinstance(dependencies, list):
                            raise SchemaValidationError(
                                f"{path}: invalid dependentRequired schema"
                            )
                        for dependency in dependencies:
                            if dependency not in value:
                                raise SchemaValidationError(
                                    f"{path}: {name} requires {dependency}"
                                )
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extras = set(value) - set(properties)
                if extras:
                    raise SchemaValidationError(f"{path}: unexpected {sorted(extras)!r}")
            for name, item in properties.items():
                if name in value and isinstance(item, dict):
                    self._validate(value[name], item, f"{path}.{name}")
        if isinstance(value, list):
            item = schema.get("items")
            if isinstance(item, dict):
                for index, child in enumerate(value):
                    self._validate(child, item, f"{path}[{index}]")
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise SchemaValidationError(f"{path}: shorter than minLength")
            if "pattern" in schema and re.fullmatch(str(schema["pattern"]), value) is None:
                raise SchemaValidationError(f"{path}: does not match pattern")
            if schema.get("format") == "date-time":
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise SchemaValidationError(f"{path}: invalid date-time") from exc
        if isinstance(value, int) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if isinstance(minimum, int) and value < minimum:
                raise SchemaValidationError(f"{path}: below minimum")

    @staticmethod
    def _matches(value: object, expected: object) -> bool:
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "null": value is None,
        }.get(expected, False)


class RunCatalogContractTests(unittest.TestCase):
    def _validator(self, name: str) -> ClosedSchemaValidator:
        schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        return ClosedSchemaValidator(schema)

    def test_schemas_are_closed_and_well_formed(self) -> None:
        for name in (
            "dashboard-audit-root-registry.schema.json",
            "run-descriptor.schema.json",
            "controller-liveness.schema.json",
            "run-catalog-snapshot.schema.json",
        ):
            self.assertFalse(json.loads((SCHEMAS / name).read_text())["additionalProperties"])
            self._validator(name)

    def test_representative_fixtures_validate_or_are_rejected(self) -> None:
        cases = {
            "terminal-feature-run.json": ("run-descriptor.schema.json", True),
            "active-feature-run.json": ("run-descriptor.schema.json", True),
            "legacy-feature-run.json": ("run-descriptor.schema.json", True),
            "plan-graph.json": ("run-descriptor.schema.json", True),
            "correlated-child.json": ("run-descriptor.schema.json", True),
            "active-liveness.json": ("controller-liveness.schema.json", True),
            "stale-catalog-snapshot.json": ("run-catalog-snapshot.schema.json", True),
            "corrupt-descriptor.json": ("run-descriptor.schema.json", False),
        }
        self.assertEqual({path.name for path in FIXTURES.glob("*.json")}, set(cases))
        for filename, (schema_name, valid) in cases.items():
            fixture = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
            validator = self._validator(schema_name)
            if valid:
                validator.validate(fixture)
            else:
                with self.assertRaises(SchemaValidationError):
                    validator.validate(fixture)

    def test_liveness_contract_cannot_carry_durable_evidence(self) -> None:
        fixture = json.loads((FIXTURES / "active-liveness.json").read_text())
        fixture["manifest_hash"] = "0" * 64
        with self.assertRaises(SchemaValidationError):
            self._validator("controller-liveness.schema.json").validate(fixture)

    def test_repository_base_commit_is_a_git_sha1_not_an_artifact_sha256(self) -> None:
        fixture = json.loads((FIXTURES / "active-feature-run.json").read_text())
        self.assertEqual(len(fixture["repository"]["base_commit"]), 40)
        self._validator("run-descriptor.schema.json").validate(fixture)
        fixture["repository"]["base_commit"] = "a" * 64
        with self.assertRaises(SchemaValidationError):
            self._validator("run-descriptor.schema.json").validate(fixture)

    def test_plan_graph_lineage_fields_are_closed_and_run_kind_scoped(self) -> None:
        validator = self._validator("run-descriptor.schema.json")
        legacy_plan_graph = json.loads((FIXTURES / "plan-graph.json").read_text())
        validator.validate(legacy_plan_graph)

        lineage_descriptor = {
            **legacy_plan_graph,
            "logical_graph_id": "graph-1",
            "graph_attempt_id": "graph-1-attempt-2",
            "predecessor_attempt_id": "graph-1-attempt-1",
        }
        validator.validate(lineage_descriptor)
        lineage_descriptor["predecessor_attempt_id"] = None
        validator.validate(lineage_descriptor)

        incomplete = dict(lineage_descriptor)
        del incomplete["graph_attempt_id"]
        with self.assertRaises(SchemaValidationError):
            validator.validate(incomplete)

        feature_run = json.loads((FIXTURES / "active-feature-run.json").read_text())
        feature_run.update({
            "logical_graph_id": "graph-1",
            "graph_attempt_id": "graph-1",
            "predecessor_attempt_id": None,
        })
        with self.assertRaises(SchemaValidationError):
            validator.validate(feature_run)

    def test_unavailable_evidence_is_explicit_not_zero(self) -> None:
        snapshot = json.loads((FIXTURES / "stale-catalog-snapshot.json").read_text())
        stale = snapshot["feature_runs"][0]
        self.assertEqual(stale["liveness"]["state"], "stale")
        self.assertEqual(stale["evidence"]["state"], "unavailable")
        self.assertIsNotNone(stale["evidence"]["reason"])


if __name__ == "__main__":
    unittest.main()
