"""Structural checks for schemas/decision.schema.json and its docs/decisions
ADR backfill (EM-C1 of the engineering-memory port).

No third-party ``jsonschema`` import: the repository has no dependency
manifest, so this is a hand-written, dependency-free structural checker
that reads and honors the schema file directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "decision.schema.json"
DECISIONS_DIR = ROOT / "docs" / "decisions"


class SchemaValidationError(ValueError):
    """Raised when a candidate decision record violates the schema."""


class ClosedSchemaValidator:
    """Small dependency-free validator for this repository-owned contract.

    Fails closed on any schema construct it does not implement: an
    unrecognized keyword, or a recognized keyword used in an unimplemented
    form (object-valued ``additionalProperties``, tuple-form ``items``),
    raises rather than being silently skipped. That check walks the full
    schema tree once, up front in `validate()`, before any value-matching
    begins -- deliberately outside of `_valid()`'s try/except, which treats
    a `SchemaValidationError` raised while testing an `if`/`not` subschema
    as an ordinary non-match. Running the check only inside that swallowed
    path would let an unimplemented construct nested under `if`/`not` pass
    unverified instead of being loudly rejected.
    """

    KNOWN_KEYWORDS = frozenset(
        {
            "$schema",
            "$id",
            "title",
            "type",
            "additionalProperties",
            "required",
            "properties",
            "items",
            "enum",
            "const",
            "format",
            "minLength",
            "allOf",
            "if",
            "then",
            "not",
        }
    )

    def __init__(self, schema: dict[str, object]) -> None:
        self.schema = schema

    def validate(self, value: object) -> None:
        # Vet the whole schema tree for unimplemented constructs up front,
        # outside of _valid()'s try/except. _valid() is used to evaluate
        # `if`/`not` subschemas and treats any SchemaValidationError as
        # "does not match"; if the unimplemented-keyword check only ran
        # inside that swallowed path, an unimplemented construct nested
        # under `if`/`not` would be silently read as a non-match (fail
        # open) instead of surfacing as the loud rejection this validator
        # promises.
        self._check_keywords(self.schema)
        self._validate(value, self.schema, "$")

    def _check_keywords(self, schema: dict[str, object]) -> None:
        unknown = set(schema) - self.KNOWN_KEYWORDS
        if unknown:
            raise SchemaValidationError(
                f"schema uses unimplemented keyword(s) {sorted(unknown)!r}; "
                "ClosedSchemaValidator must be extended before this schema can be trusted"
            )
        additional = schema.get("additionalProperties")
        if additional is not None and not isinstance(additional, bool):
            raise SchemaValidationError(
                "schema uses an object-valued additionalProperties; "
                "ClosedSchemaValidator only implements the boolean form"
            )
        items = schema.get("items")
        if isinstance(items, list):
            raise SchemaValidationError(
                "schema uses tuple-form items (a list); "
                "ClosedSchemaValidator only implements the single-schema form"
            )
        for part in schema.get("allOf", []):
            if not isinstance(part, dict):
                raise SchemaValidationError("invalid allOf schema")
            self._check_keywords(part)
        for key in ("if", "then", "not"):
            nested = schema.get(key)
            if isinstance(nested, dict):
                self._check_keywords(nested)
        for prop_schema in schema.get("properties", {}).values():
            if isinstance(prop_schema, dict):
                self._check_keywords(prop_schema)
        if isinstance(items, dict):
            self._check_keywords(items)

    def _valid(self, value: object, schema: dict[str, object]) -> bool:
        try:
            self._validate(value, schema, "$")
        except SchemaValidationError:
            return False
        return True

    def _validate(self, value: object, schema: dict[str, object], path: str) -> None:
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
        if expected is not None:
            types = expected if isinstance(expected, list) else [expected]
            if not any(self._matches(value, item) for item in types):
                raise SchemaValidationError(f"{path}: wrong type")
        if isinstance(value, dict):
            required = schema.get("required", [])
            for name in required:
                if name not in value:
                    raise SchemaValidationError(f"{path}: missing {name}")
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
            if schema.get("format") == "date-time":
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise SchemaValidationError(f"{path}: invalid date-time") from exc

    @staticmethod
    def _matches(value: object, expected: object) -> bool:
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "null": value is None,
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }.get(expected, False)


def _load_validator() -> ClosedSchemaValidator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return ClosedSchemaValidator(schema)


def _base_record(schema_version: str) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "decision_id": "decision-1",
        "run_id": "run-1",
        "timestamp": "2026-08-20T00:00:00Z",
        "actor_id": "actor-1",
        "phase": "implementation",
        "question": "Which approach?",
        "choice": "approach A",
        "alternatives": ["approach B"],
        "rationale": "approach A is simpler",
        "evidence": ["tests/test_decision_schema.py"],
        "consequences": ["future work must respect approach A"],
        "reversible": True,
        "status": "accepted",
    }


class DecisionSchemaVersionTests(unittest.TestCase):
    def test_schema_declares_expected_version_enum(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"], {"enum": ["1.0", "1.1"]})
        self.assertIs(schema["additionalProperties"], False)
        for field in ("supersedes", "concerns_paths", "valid_from_commit"):
            self.assertIn(field, schema["properties"])
            self.assertNotIn(field, schema["required"])

    def test_accepts_1_1_record_carrying_lifecycle_fields(self) -> None:
        record = _base_record("1.1")
        record["supersedes"] = ["decision-0"]
        record["concerns_paths"] = ["harness_labs/plangraph/plan_graph.py"]
        record["valid_from_commit"] = "a" * 40
        _load_validator().validate(record)

    def test_accepts_1_0_record_lacking_lifecycle_fields(self) -> None:
        record = _base_record("1.0")
        _load_validator().validate(record)

    def test_rejects_1_0_record_carrying_supersedes(self) -> None:
        record = _base_record("1.0")
        record["supersedes"] = ["decision-0"]
        with self.assertRaises(SchemaValidationError):
            _load_validator().validate(record)

    def test_rejects_1_0_record_carrying_concerns_paths(self) -> None:
        record = _base_record("1.0")
        record["concerns_paths"] = ["harness_labs/"]
        with self.assertRaises(SchemaValidationError):
            _load_validator().validate(record)

    def test_rejects_1_0_record_carrying_valid_from_commit(self) -> None:
        record = _base_record("1.0")
        record["valid_from_commit"] = "a" * 40
        with self.assertRaises(SchemaValidationError):
            _load_validator().validate(record)

    def test_rejects_record_with_undeclared_field(self) -> None:
        record = _base_record("1.1")
        record["unexpected_field"] = "not part of the schema"
        with self.assertRaises(SchemaValidationError):
            _load_validator().validate(record)


class ClosedSchemaValidatorFailsClosedTests(unittest.TestCase):
    def test_unimplemented_keyword_raises_instead_of_silently_passing(self) -> None:
        validator = ClosedSchemaValidator({"type": "object", "oneOf": [{"type": "object"}]})
        with self.assertRaises(SchemaValidationError):
            validator.validate({})

    def test_unimplemented_keyword_nested_under_not_still_raises(self) -> None:
        # Regression: the unknown-keyword check must not run only inside
        # _valid()'s try/except, or an unimplemented construct nested under
        # `not` would be swallowed as an ordinary non-match instead of
        # raising.
        validator = ClosedSchemaValidator(
            {"type": "object", "properties": {"x": {"not": {"oneOf": []}}}}
        )
        with self.assertRaises(SchemaValidationError):
            validator.validate({"x": "anything"})

    def test_unimplemented_keyword_nested_under_if_still_raises(self) -> None:
        validator = ClosedSchemaValidator(
            {
                "type": "object",
                "if": {"properties": {"x": {"oneOf": []}}},
                "then": {"required": ["y"]},
            }
        )
        with self.assertRaises(SchemaValidationError):
            validator.validate({"x": "anything"})

    def test_object_valued_additional_properties_raises(self) -> None:
        validator = ClosedSchemaValidator(
            {"type": "object", "additionalProperties": {"type": "string"}}
        )
        with self.assertRaises(SchemaValidationError):
            validator.validate({})

    def test_tuple_form_items_raises(self) -> None:
        validator = ClosedSchemaValidator(
            {"type": "array", "items": [{"type": "string"}]}
        )
        with self.assertRaises(SchemaValidationError):
            validator.validate([])

    def test_integer_and_number_types_are_matched_not_always_rejected(self) -> None:
        ClosedSchemaValidator({"type": "integer"}).validate(3)
        ClosedSchemaValidator({"type": "number"}).validate(3.5)
        with self.assertRaises(SchemaValidationError):
            ClosedSchemaValidator({"type": "integer"}).validate("3")


# sha256 hex digests of each pre-existing docs/decisions ADR/TEMPLATE file's
# body bytes (everything from the first "## " heading onward), captured from
# the pre-change tree at commit 3e04a1d5d9942c5572f819dea2bd03d56c94aa88 (the
# commit immediately preceding this run's EM-C1 backfill, i.e.
# `git show 3e04a1d5d9942c5572f819dea2bd03d56c94aa88:<path>`) before that
# backfill added header-only `Concerns-paths:` lines. Header lines above the
# first "## " heading are the only permitted difference from that commit's
# tree. Pinning the full commit sha (rather than `HEAD`) keeps this
# provenance note resolvable after the branch advances past that commit.
#
# docs/decisions/README.md is intentionally absent from this digest set.
# AC-EM-13's "each pre-existing file under docs/decisions/" is read together
# with this node's objective and the plan's [em-decisions] backfill bullet,
# both of which mandate two body edits to README.md: listing both `0006`
# decisions in the "Accepted decisions" index, and adding the header-only
# amendment exemption sentence. Those two edits are covered by their own
# content assertions in DecisionsBackfillContentTests below, not by
# byte-identity against a pre-change digest.
ADR_BODY_DIGESTS: dict[str, str] = {
    "0001-execution-first-production-lifecycle.md": (
        "d59554f2ecf96449358ec59e76228dd39b19a8c61d17345ac02c3c6d35bbc7d3"
    ),
    "0002-controller-owned-parallel-child-batches.md": (
        "5283a48ec4bcbcae3d6ebdc20344e60d995ed4bb0266e2e6b5a0141aa56899d1"
    ),
    "0003-pass-through-child-context.md": (
        "6eea5dcbb95246085d6cb499da66166c77148c53dab239c04a347749ba811eb7"
    ),
    "0004-hybrid-controller-command-kernel.md": (
        "20f4a9b51a4916c6a0a5f7e5ff23472a9c78ba77be693f987dfa4fc6405538ef"
    ),
    "0005-ledger-backed-review-fix.md": (
        "7ea087c756f77f080bcddc6e82984ca54eebb76a708a8ea78f0ac5a4666a77e4"
    ),
    "0006-parallel-plangraph-contract.md": (
        "f8f67f07b6f0323cea9f18841a957097bdb1dab611e5b79a9bcad8a45146df90"
    ),
    "0006-repository-bound-plan-approval.md": (
        "8407f4fdca68ea9fba995bea3083e05ef23e744f4009586220f890d6360d0b7d"
    ),
    "0007-in-graph-escalation-bounded-unsealing.md": (
        "abf600813906dcd5cf4bb672afae5bb213bc474b2d9c98a2c13d39f72337a008"
    ),
    "TEMPLATE.md": (
        "7f5747e2ec2b7498caf1ed6b5fb47bcb33db904f6952d0754266718c8cad967b"
    ),
}


def _body_bytes(text: str) -> bytes:
    heading_index = text.index("## ")
    return text[heading_index:].encode("utf-8")


class AdrBodyDigestTests(unittest.TestCase):
    def test_every_recorded_file_has_a_byte_identical_body(self) -> None:
        for name, digest in ADR_BODY_DIGESTS.items():
            text = (DECISIONS_DIR / name).read_text(encoding="utf-8")
            actual = hashlib.sha256(_body_bytes(text)).hexdigest()
            self.assertEqual(
                actual, digest, f"{name}: body bytes changed beyond its header block"
            )

    def test_recorded_files_cover_every_numbered_adr_and_the_template(self) -> None:
        numbered = {
            path.name
            for path in DECISIONS_DIR.glob("*.md")
            if re.match(r"^\d{4}-", path.name)
        }
        expected = numbered | {"TEMPLATE.md"}
        self.assertEqual(set(ADR_BODY_DIGESTS), expected)


class DecisionsBackfillContentTests(unittest.TestCase):
    """Proves the EM-C1 objective's affirmative backfill clauses, which
    AdrBodyDigestTests (immutability) and DecisionSchemaVersionTests (schema
    shape) do not cover on their own."""

    NUMBERED_ADR_NAMES = [
        name for name in ADR_BODY_DIGESTS if name != "TEMPLATE.md"
    ]

    def test_numbered_adrs_gained_a_concerns_paths_header_line(self) -> None:
        for name in self.NUMBERED_ADR_NAMES:
            text = (DECISIONS_DIR / name).read_text(encoding="utf-8")
            header = text[: text.index("## ")]
            self.assertRegex(
                header,
                r"(?m)^Concerns-paths: .+$",
                f"{name}: missing a Concerns-paths: header line",
            )

    def test_template_gained_the_three_optional_lines_after_status(self) -> None:
        lines = (DECISIONS_DIR / "TEMPLATE.md").read_text(encoding="utf-8").splitlines()
        status_index = next(i for i, line in enumerate(lines) if line.startswith("Status:"))
        self.assertLess(status_index, 12, "Status: must stay within the first 12 lines")
        following = lines[status_index + 1 : status_index + 4]
        self.assertTrue(following[0].startswith("Supersedes:"), following)
        self.assertTrue(following[1].startswith("Concerns-paths:"), following)
        self.assertTrue(following[2].startswith("Valid-from-commit:"), following)

    def test_readme_lists_both_0006_decisions_and_the_amendment_exemption(self) -> None:
        text = (DECISIONS_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("0006-parallel-plangraph-contract.md", text)
        self.assertIn("0006-repository-bound-plan-approval.md", text)
        self.assertIn("header-only amendment", text)


if __name__ == "__main__":
    unittest.main()
