"""SI-01: improvement artifact schemas and their stdlib-only checker.

Three closed contracts (``blocker-observation/1``, ``blocker-pattern/1``,
``improvement-proposal/1``) plus ``scripts/dev/check_improvement_artifacts.py``,
a hand-written (no ``jsonschema``) validator for committed artifact trees.

The ``classification`` and ``evidence_classification`` enums are asserted
equal to enums loaded at runtime from ``schemas/block-escalation.json``,
``schemas/retry-budget-ledger.json``, and ``schemas/audit-event.schema.json``
-- never retyped literals that could drift silently. The ``resolution``
vocabulary has no external source: it is defined exactly once, in
``blocker-observation.schema.json``'s ``$defs``, and every other use site
references it by ``$ref``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"
CHECKER_PATH = REPO_ROOT / "scripts" / "dev" / "check_improvement_artifacts.py"
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "improvement" / "artifacts"

spec = importlib.util.spec_from_file_location("check_improvement_artifacts", CHECKER_PATH)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def _load(name: str) -> dict[str, Any]:
    with (SCHEMAS_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _collect_enums_by_key(node: Any, key: str, found: list[list[Any]]) -> None:
    """Recursively collect every ``enum`` list attached to a property named
    ``key`` anywhere in a JSON Schema document."""
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, dict) and isinstance(value.get("enum"), list):
            found.append(value["enum"])
        for child in node.values():
            _collect_enums_by_key(child, key, found)
    elif isinstance(node, list):
        for child in node:
            _collect_enums_by_key(child, key, found)


def _collect_all_enums(node: Any, found: list[list[Any]]) -> None:
    """Recursively collect every ``enum`` list anywhere in a document,
    regardless of the property name it is attached to."""
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            found.append(node["enum"])
        for child in node.values():
            _collect_all_enums(child, found)
    elif isinstance(node, list):
        for child in node:
            _collect_all_enums(child, found)


def _single_enum(doc: dict[str, Any], key: str, source_label: str) -> list[Any]:
    found: list[list[Any]] = []
    _collect_enums_by_key(doc, key, found)
    assert found, f"{source_label} defines no {key!r} enum"
    dedup = {tuple(entry) for entry in found}
    assert len(dedup) == 1, (
        f"{source_label} defines {len(dedup)} inconsistent {key!r} enum variants: {dedup}"
    )
    return list(next(iter(dedup)))


def _all_ref_strings(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.append(ref)
        for child in node.values():
            _all_ref_strings(child, found)
    elif isinstance(node, list):
        for child in node:
            _all_ref_strings(child, found)


def _all_object_schemas(node: Any, found: list[dict[str, Any]]) -> None:
    """Recursively collect every JSON-Schema-object node (identified by the
    presence of a ``properties`` key). ``if``/``then`` fragments are
    conditional matchers, not closed data contracts, so their subtrees are
    skipped."""
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            found.append(node)
        for key, child in node.items():
            if key in ("if", "then"):
                continue
            _all_object_schemas(child, found)
    elif isinstance(node, list):
        for child in node:
            _all_object_schemas(child, found)


class ProtocolConstsAndClosureTests(unittest.TestCase):
    SCHEMAS = {
        "blocker-observation/1": "blocker-observation.schema.json",
        "blocker-pattern/1": "blocker-pattern.schema.json",
        "improvement-proposal/1": "improvement-proposal.schema.json",
    }

    def test_protocol_consts_present(self) -> None:
        for protocol, filename in self.SCHEMAS.items():
            doc = _load(filename)
            self.assertEqual(
                doc.get("properties", {}).get("protocol", {}).get("const"),
                protocol,
                f"{filename} does not pin protocol const {protocol!r}",
            )

    def test_additional_properties_false_at_every_object_level(self) -> None:
        for filename in self.SCHEMAS.values():
            doc = _load(filename)
            object_schemas = []
            _all_object_schemas(doc, object_schemas)
            self.assertTrue(object_schemas, f"{filename} defines no object schemas")
            for schema in object_schemas:
                self.assertIs(
                    schema.get("additionalProperties"),
                    False,
                    f"{filename}: an object schema with properties "
                    f"{sorted(schema['properties'])} is missing additionalProperties: false",
                )


class EnumReuseTests(unittest.TestCase):
    """AC-SI01-1: classification/evidence_classification must equal the
    source schemas' enums, loaded at runtime -- not retyped copies."""

    def test_classification_enum_matches_block_escalation_source(self) -> None:
        source = _single_enum(
            _load("block-escalation.json"), "classification", "block-escalation.json"
        )
        for filename in ("blocker-observation.schema.json", "blocker-pattern.schema.json"):
            ours = _single_enum(_load(filename), "classification", filename)
            self.assertEqual(
                sorted(ours), sorted(source),
                f"{filename}'s classification enum has drifted from block-escalation.json",
            )

    def test_classification_enum_matches_retry_budget_ledger_source(self) -> None:
        source = _single_enum(
            _load("retry-budget-ledger.json"), "classification", "retry-budget-ledger.json"
        )
        for filename in ("blocker-observation.schema.json", "blocker-pattern.schema.json"):
            ours = _single_enum(_load(filename), "classification", filename)
            self.assertEqual(
                sorted(ours), sorted(source),
                f"{filename}'s classification enum has drifted from retry-budget-ledger.json",
            )

    def test_evidence_classification_enum_matches_audit_event_source(self) -> None:
        source = _single_enum(
            _load("audit-event.schema.json"),
            "evidence_classification",
            "audit-event.schema.json",
        )
        ours = _single_enum(
            _load("blocker-observation.schema.json"),
            "evidence_classification",
            "blocker-observation.schema.json",
        )
        self.assertEqual(
            sorted(ours), sorted(source),
            "blocker-observation.schema.json's evidence_classification enum has "
            "drifted from audit-event.schema.json",
        )


class ResolutionVocabularyTests(unittest.TestCase):
    """The resolution vocabulary has no external source: it must be defined
    exactly once (blocker-observation.schema.json's $defs) and referenced
    by $ref, never retyped, from every other use site."""

    RESOLUTION_MEMBERS = {
        "self_recovered",
        "repair_attempt",
        "retry_renewed",
        "operator_intervention",
        "prompt_workaround",
        "transferred",
        "unresolved_blocked",
    }

    def test_defined_exactly_once_in_blocker_observation_defs(self) -> None:
        doc = _load("blocker-observation.schema.json")
        resolution_def = doc.get("$defs", {}).get("resolution")
        self.assertIsInstance(resolution_def, dict, "no $defs/resolution in blocker-observation.schema.json")
        self.assertEqual(set(resolution_def["enum"]), self.RESOLUTION_MEMBERS)

        # It must not be retyped verbatim as a bare enum literal anywhere else
        # in the three schemas -- every other occurrence must be a $ref.
        for filename in (
            "blocker-observation.schema.json",
            "blocker-pattern.schema.json",
            "improvement-proposal.schema.json",
        ):
            # Reuse the already-parsed doc for self-comparison so identity
            # checks against resolution_def's own enum list are meaningful
            # (a fresh _load() would re-parse into unrelated list objects).
            other_doc = doc if filename == "blocker-observation.schema.json" else _load(filename)
            all_enums: list[list[Any]] = []
            _collect_all_enums(other_doc, all_enums)
            retyped = [
                e for e in all_enums
                if set(e) == self.RESOLUTION_MEMBERS and e is not resolution_def.get("enum")
            ]
            if filename == "blocker-observation.schema.json":
                # exactly the one definition itself is allowed
                self.assertEqual(
                    len(retyped), 0,
                    f"{filename} retypes the resolution vocabulary outside $defs/resolution",
                )
            else:
                self.assertEqual(
                    retyped, [],
                    f"{filename} retypes the resolution vocabulary instead of $ref-ing it",
                )

    def test_referenced_via_ref_from_every_use_site(self) -> None:
        observation_doc = _load("blocker-observation.schema.json")
        self.assertEqual(
            observation_doc["properties"]["resolution"],
            {"$ref": "#/$defs/resolution"},
            "blocker-observation.schema.json's resolution field must $ref its own $defs entry",
        )

        pattern_doc = _load("blocker-pattern.schema.json")
        fixes_employed = pattern_doc["properties"]["fixes_employed"]
        self.assertEqual(
            fixes_employed.get("items"),
            {"$ref": "blocker-observation.schema.json#/$defs/resolution"},
            "blocker-pattern.schema.json's fixes_employed must $ref "
            "blocker-observation.schema.json's resolution $defs entry",
        )

        # No schema may define its own competing $defs/resolution entry.
        for filename in ("blocker-pattern.schema.json", "improvement-proposal.schema.json"):
            doc = _load(filename)
            self.assertNotIn(
                "resolution", doc.get("$defs", {}),
                f"{filename} defines a competing $defs/resolution instead of referencing "
                "blocker-observation.schema.json's",
            )

    def test_tripwire_source_schemas_have_not_drifted_toward_resolution(self) -> None:
        """If a future edit to the *source* enum schemas grows an enum that
        overlaps >=50% with the resolution vocabulary, this must fail loudly
        rather than let the overlap go unnoticed -- at that point resolution
        should be re-derived from that schema instead of minted here."""
        source_files = (
            "block-escalation.json",
            "retry-budget-ledger.json",
            "audit-event.schema.json",
        )
        for filename in source_files:
            doc = _load(filename)
            enums: list[list[Any]] = []
            _collect_all_enums(doc, enums)
            for enum in enums:
                members = set(enum)
                overlap = members & self.RESOLUTION_MEMBERS
                ratio = len(overlap) / len(self.RESOLUTION_MEMBERS)
                self.assertLess(
                    ratio, 0.5,
                    f"{filename} defines an enum {sorted(members)} that now overlaps "
                    f">=50% ({sorted(overlap)}) with the resolution vocabulary minted in "
                    "blocker-observation.schema.json's $defs; adopt this schema as "
                    "resolution's source instead of letting the overlap sit unnoticed",
                )


class CheckerFixtureTests(unittest.TestCase):
    """AC-SI01-2 / AC-SI01-3: drive the checker over the passing fixture
    tree and each individually isolated failing fixture case."""

    def test_valid_tree_exits_zero(self) -> None:
        errors = checker.check_tree(FIXTURES_ROOT / "valid")
        self.assertEqual(errors, [])

    def test_valid_tree_via_cli_exits_zero(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CHECKER_PATH), "--root", str(FIXTURES_ROOT / "valid")],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_proposal_missing_complexity_admission_field_exits_nonzero(self) -> None:
        errors = checker.check_tree(
            FIXTURES_ROOT / "invalid" / "proposal-missing-complexity-admission"
        )
        self.assertTrue(
            any("demonstrated_failure" in e for e in errors), errors
        )

    def test_accepted_proposal_without_human_ruling_exits_nonzero(self) -> None:
        errors = checker.check_tree(
            FIXTURES_ROOT / "invalid" / "proposal-accepted-without-ruling"
        )
        self.assertTrue(
            any("human ruling" in e for e in errors), errors
        )

    def test_success_criterion_file_absent_from_required_paths_exits_nonzero(self) -> None:
        errors = checker.check_tree(
            FIXTURES_ROOT / "invalid" / "proposal-success-criteria-file-not-in-required-paths"
        )
        self.assertTrue(
            any("required_paths" in e for e in errors), errors
        )

    def test_success_criterion_not_intake_shaped_exits_nonzero(self) -> None:
        errors = checker.check_tree(
            FIXTURES_ROOT / "invalid" / "proposal-success-criteria-not-intake-shaped"
        )
        self.assertTrue(
            any("oneOf" in e or "assertion" in e for e in errors), errors
        )

    def test_addressed_pattern_missing_campaign_and_landing_commit_exits_nonzero(self) -> None:
        errors = checker.check_tree(
            FIXTURES_ROOT / "invalid" / "pattern-addressed-missing-campaign"
        )
        self.assertTrue(any("campaign_id" in e for e in errors), errors)
        self.assertTrue(any("landing_commit" in e for e in errors), errors)

    def test_proposal_citing_nonexistent_pattern_exits_nonzero(self) -> None:
        errors = checker.check_tree(
            FIXTURES_ROOT / "invalid" / "proposal-cites-nonexistent-pattern"
        )
        self.assertTrue(
            any("pattern-does-not-exist-01" in e for e in errors), errors
        )

    def test_missing_root_exits_nonzero_instead_of_vacuously_zero(self) -> None:
        missing_root = FIXTURES_ROOT / "does-not-exist"
        self.assertFalse(missing_root.exists())
        errors = checker.check_tree(missing_root)
        self.assertTrue(errors, "a missing root must not pass vacuously")

    def test_cli_exits_nonzero_for_missing_root(self) -> None:
        missing_root = FIXTURES_ROOT / "does-not-exist"
        completed = subprocess.run(
            [sys.executable, str(CHECKER_PATH), "--root", str(missing_root)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)

    def test_cli_exits_nonzero_for_each_invalid_fixture_case(self) -> None:
        invalid_root = FIXTURES_ROOT / "invalid"
        cases = sorted(p for p in invalid_root.iterdir() if p.is_dir())
        self.assertTrue(cases, "no invalid fixture cases found")
        for case in cases:
            completed = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--root", str(case)],
                capture_output=True, text=True,
            )
            self.assertEqual(
                completed.returncode, 1,
                f"{case.name} did not exit nonzero: stdout={completed.stdout!r}",
            )

    def test_checker_imports_stdlib_only(self) -> None:
        import ast

        tree = ast.parse(CHECKER_PATH.read_text(encoding="utf-8"))
        stdlib_allowed = {
            "__future__", "argparse", "json", "re", "sys", "pathlib", "typing",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertIn(top, stdlib_allowed, f"non-stdlib import: {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                self.assertIn(top, stdlib_allowed, f"non-stdlib import: {node.module}")


class ComplexityAdmissionRequiredFieldsTests(unittest.TestCase):
    """AC-SI01-2: 'exits nonzero on a proposal missing any Complexity-
    admission field' rests entirely on improvement-proposal.schema.json's
    'required' array; pin its membership so silently dropping one of the
    three fields from 'required' cannot go unnoticed while the suite stays
    green (only demonstrated_failure has a dedicated missing-field
    fixture)."""

    COMPLEXITY_ADMISSION_FIELDS = {
        "demonstrated_failure", "production_consumer", "end_to_end_assertion",
    }

    def test_complexity_admission_triple_is_required(self) -> None:
        doc = _load("improvement-proposal.schema.json")
        required = set(doc.get("required", []))
        missing = self.COMPLEXITY_ADMISSION_FIELDS - required
        self.assertFalse(
            missing,
            f"improvement-proposal.schema.json's 'required' array dropped "
            f"Complexity-admission field(s) {sorted(missing)}",
        )


class SchemaEngineFailsClosedTests(unittest.TestCase):
    """The checker's schema engine must fail closed on schema constructs it
    does not implement, rather than silently ignoring them."""

    def test_check_schema_keywords_flags_unimplemented_keyword(self) -> None:
        schema = {"type": "string", "maxLength": 5}
        errors = checker.check_schema_keywords(schema, schema, "inline", set())
        self.assertTrue(any("maxLength" in e for e in errors), errors)

    def test_check_schema_keywords_flags_keyword_nested_under_ref_and_properties(self) -> None:
        doc = {
            "$defs": {"bad": {"type": "string", "patternProperties": {}}},
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/bad"}},
        }
        errors = checker.check_schema_keywords(doc, doc, "inline", set())
        self.assertTrue(any("patternProperties" in e for e in errors), errors)

    def test_check_schema_keywords_flags_keyword_nested_under_if_then_oneof_anyof(self) -> None:
        for wrapper in (
            {"if": {"type": "object", "uniqueItems": True}, "then": {}},
            {"oneOf": [{"type": "string", "uniqueItems": True}]},
            {"anyOf": [{"type": "string", "uniqueItems": True}]},
        ):
            errors = checker.check_schema_keywords(wrapper, wrapper, "inline", set())
            self.assertTrue(any("uniqueItems" in e for e in errors), (wrapper, errors))

    def test_real_schemas_pass_keyword_vetting(self) -> None:
        for filename in (
            "blocker-observation.schema.json",
            "blocker-pattern.schema.json",
            "improvement-proposal.schema.json",
        ):
            doc = checker.load_schema(filename)
            errors = checker.check_schema_keywords(doc, doc, filename, set())
            self.assertEqual(errors, [], f"{filename}: {errors}")

    def test_format_date_time_rejects_non_conforming_string(self) -> None:
        schema = {"type": "string", "format": "date-time"}
        errors = checker.validate("yesterday", schema, schema, "inline", "$.ruled_at")
        self.assertTrue(errors, "a non-ISO8601 string must fail format: date-time")

    def test_format_date_time_accepts_conforming_string(self) -> None:
        schema = {"type": "string", "format": "date-time"}
        errors = checker.validate(
            "2026-08-21T10:00:00Z", schema, schema, "inline", "$.ruled_at"
        )
        self.assertEqual(errors, [])


class IntakeShapeTests(unittest.TestCase):
    """AC-SI01-3: every success_criteria entry is finding-shaped."""

    def test_valid_proposal_success_criteria_are_intake_shaped(self) -> None:
        with (FIXTURES_ROOT / "valid" / "improvement-proposal.json").open() as handle:
            payload = json.load(handle)
        for entry in payload["success_criteria"]:
            self.assertTrue(entry["file"])
            self.assertTrue(entry["subject"])
            self.assertTrue(entry["required_paths"])
            self.assertIn(entry["file"], entry["required_paths"])
            assertion = entry["assertion"]
            has_argv_and_timeout = "argv" in assertion and "timeout_seconds" in assertion
            has_signature_absence = "signature_absent" in assertion
            self.assertTrue(has_argv_and_timeout or has_signature_absence)

    def test_valid_proposal_success_criteria_pass_the_real_finding_intake_validator(self) -> None:
        """Prove -- not just assert -- that a success_criteria entry seeds
        delta-to-run finding intake without transformation, by running it
        through convergence_ledger's actual ``_validate_finding``, the same
        function ``ConvergenceLedger.ingest_audit`` calls on every finding.
        A future tightening of that envelope (e.g. making a currently-
        optional field required) will fail this test instead of drifting
        unnoticed."""
        from harness_labs.plangraph.convergence_ledger import _validate_finding

        with (FIXTURES_ROOT / "valid" / "improvement-proposal.json").open() as handle:
            payload = json.load(handle)
        for entry in payload["success_criteria"]:
            envelope = _validate_finding(entry)
            self.assertEqual(envelope["file"], entry["file"])
            self.assertEqual(envelope["subject"], entry["subject"])
            self.assertEqual(envelope["required_paths"], entry["required_paths"])


if __name__ == "__main__":
    unittest.main()
