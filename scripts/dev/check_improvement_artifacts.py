#!/usr/bin/env python3
"""Deterministic gate for committed improvement-agent artifacts (SI-01).

Validates every ``*.json`` file under a committed artifact tree (default
``docs/improvement/``) against its declared ``protocol`` --
``blocker-observation/1``, ``blocker-pattern/1``, or
``improvement-proposal/1`` -- using a hand-written JSON Schema subset
engine (``type``/``enum``/``const``/``required``/``properties``/
``additionalProperties``/``items``/``$ref``/``oneOf``/``anyOf``/``if``/
``then``/``pattern``/``minLength``/``minItems``/``minimum``/
``exclusiveMinimum``/``format: date-time``) against the schemas in
``schemas/``. No third-party dependency (no ``jsonschema``) is imported.

The engine fails closed on schema constructs it does not implement: before
validating any instance, ``check_schema_keywords`` walks each schema's
full reachable surface (``$ref``/``$defs``/``properties``/``items``/
``oneOf``/``anyOf``/``if``/``then``) and reports an unrecognized keyword as
a violation, rather than silently ignoring it.

Beyond schema shape, three cross-field/cross-artifact business rules are
hand-checked because they are not expressible as plain JSON Schema
constraints:

1. A proposal with ``status: "accepted"`` must carry a human ruling
   (non-empty ``actor``, ``statement``, ``ruled_at``).
2. Every ``success_criteria`` entry's ``file`` must be a member of its own
   ``required_paths``.
3. Every ``pattern_ids`` entry a proposal cites must exist as some
   ``blocker-pattern/1`` artifact's ``pattern_id`` elsewhere in the tree.

The Complexity-admission triple (``demonstrated_failure``,
``production_consumer``, ``end_to_end_assertion``) and the
pattern-``addressed`` requirement for ``campaign_id``/``landing_commit``
are both enforced generically: they are ``required`` (respectively via an
``if``/``then``) in the schemas themselves, so a missing field surfaces as
an ordinary schema-shape violation.

Exit status is 0 when every artifact in the tree validates cleanly against
a tree rooted at a directory that exists, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"

PROTOCOL_TO_SCHEMA = {
    "blocker-observation/1": "blocker-observation.schema.json",
    "blocker-pattern/1": "blocker-pattern.schema.json",
    "improvement-proposal/1": "improvement-proposal.schema.json",
}

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}

#: Every JSON Schema keyword this engine implements. Anything outside this
#: set found on a reachable schema node is a violation (fail closed), not a
#: silently-ignored no-op -- see ``check_schema_keywords``.
KNOWN_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema", "$id", "title", "description",
        "type", "enum", "const", "required", "properties",
        "additionalProperties", "items", "$ref", "$defs",
        "oneOf", "anyOf", "if", "then",
        "pattern", "minLength", "minItems", "minimum", "exclusiveMinimum",
        "format",
    }
)

DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def load_schema(filename: str) -> dict[str, Any]:
    if filename not in _SCHEMA_CACHE:
        with (SCHEMAS_DIR / filename).open("r", encoding="utf-8") as handle:
            _SCHEMA_CACHE[filename] = json.load(handle)
    return _SCHEMA_CACHE[filename]


def resolve_pointer(doc: Any, pointer: str) -> Any:
    if not pointer:
        return doc
    node = doc
    for part in pointer.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _check_type(instance: Any, type_def: Any) -> bool:
    types = type_def if isinstance(type_def, list) else [type_def]
    for candidate in types:
        if candidate == "object" and isinstance(instance, dict):
            return True
        if candidate == "array" and isinstance(instance, list):
            return True
        if candidate == "string" and isinstance(instance, str):
            return True
        if candidate == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if candidate == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if candidate == "boolean" and isinstance(instance, bool):
            return True
        if candidate == "null" and instance is None:
            return True
    return False


def check_schema_keywords(
    schema: Any,
    base_doc: dict[str, Any],
    base_file: str,
    seen: set[tuple[str, int]],
) -> list[str]:
    """Walk ``schema``'s full reachable surface and report any keyword this
    engine does not implement.

    Runs structurally over the schema documents themselves, before any
    instance is validated -- deliberately separate from ``validate``'s
    instance-matching recursion, so an unimplemented construct nested under
    ``if``/``oneOf``/``anyOf`` cannot be swallowed the way a raised error
    inside condition-matching would be. A schema this walk has already
    visited (tracked by ``(file, id(schema))``) is not re-walked, so a
    shared ``$defs`` entry referenced from multiple sites is vetted once.
    """
    if not isinstance(schema, dict):
        return []
    node_key = (base_file, id(schema))
    if node_key in seen:
        return []
    seen.add(node_key)

    errors: list[str] = []
    unknown = set(schema) - KNOWN_SCHEMA_KEYWORDS
    if unknown:
        errors.append(
            f"{base_file}: schema uses unimplemented keyword(s) {sorted(unknown)!r}; "
            "the checker's engine must be extended before this schema can be trusted"
        )

    if "$ref" in schema:
        filename, _, pointer = schema["$ref"].partition("#")
        if filename:
            target_doc = load_schema(filename)
            new_base_file = filename
        else:
            target_doc = base_doc
            new_base_file = base_file
        resolved = resolve_pointer(target_doc, pointer)
        errors.extend(check_schema_keywords(resolved, target_doc, new_base_file, seen))

    for key in ("if", "then"):
        if key in schema:
            errors.extend(check_schema_keywords(schema[key], base_doc, base_file, seen))
    for key in ("oneOf", "anyOf"):
        for sub in schema.get(key, []):
            errors.extend(check_schema_keywords(sub, base_doc, base_file, seen))
    if "items" in schema:
        errors.extend(check_schema_keywords(schema["items"], base_doc, base_file, seen))
    for prop_schema in schema.get("properties", {}).values():
        errors.extend(check_schema_keywords(prop_schema, base_doc, base_file, seen))
    for def_schema in schema.get("$defs", {}).values():
        errors.extend(check_schema_keywords(def_schema, base_doc, base_file, seen))

    return errors


def validate(
    instance: Any,
    schema: dict[str, Any],
    base_doc: dict[str, Any],
    base_file: str,
    path: str,
) -> list[str]:
    errors: list[str] = []

    if "$ref" in schema:
        filename, _, pointer = schema["$ref"].partition("#")
        if filename:
            target_doc = load_schema(filename)
            new_base_file = filename
        else:
            target_doc = base_doc
            new_base_file = base_file
        resolved = resolve_pointer(target_doc, pointer)
        return validate(instance, resolved, target_doc, new_base_file, path)

    if "oneOf" in schema:
        matched = [
            sub for sub in schema["oneOf"] if not validate(instance, sub, base_doc, base_file, path)
        ]
        if len(matched) != 1:
            errors.append(
                f"{path}: expected exactly one oneOf branch to match, matched {len(matched)}"
            )
        return errors

    if "anyOf" in schema:
        if not any(not validate(instance, sub, base_doc, base_file, path) for sub in schema["anyOf"]):
            errors.append(f"{path}: no anyOf branch matched")
        return errors

    if "const" in schema:
        if instance != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
        return errors

    if "enum" in schema:
        if instance not in schema["enum"]:
            errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
        return errors

    if "type" in schema and not _check_type(instance, schema["type"]):
        errors.append(f"{path}: expected type {schema['type']!r}, got {type(instance).__name__}")
        return errors

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: string shorter than minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match pattern {schema['pattern']!r}")
        if schema.get("format") == "date-time" and not DATE_TIME_RE.match(instance):
            errors.append(f"{path}: {instance!r} is not a valid format: date-time value")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {instance} at or below exclusiveMinimum {schema['exclusiveMinimum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: array shorter than minItems {schema['minItems']}")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], base_doc, base_file, f"{path}[{index}]"))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], base_doc, base_file, f"{path}.{key}"))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}: unexpected property {key!r} (additionalProperties: false)")
        if "if" in schema:
            condition_errors = validate(instance, schema["if"], base_doc, base_file, path)
            if not condition_errors and "then" in schema:
                errors.extend(validate(instance, schema["then"], base_doc, base_file, path))

    return errors


def check_accepted_ruling(instance: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if instance.get("status") != "accepted":
        return errors
    ruling = instance.get("ruling")
    if not isinstance(ruling, dict):
        errors.append(f"{path}: status 'accepted' requires a human ruling, found {ruling!r}")
        return errors
    for field in ("actor", "statement", "ruled_at"):
        value = ruling.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path}: accepted proposal ruling missing non-empty {field!r}")
    return errors


def check_success_criteria_paths(instance: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    criteria = instance.get("success_criteria")
    if not isinstance(criteria, list):
        return errors
    for index, entry in enumerate(criteria):
        if not isinstance(entry, dict):
            continue
        file_ = entry.get("file")
        required_paths = entry.get("required_paths")
        if isinstance(required_paths, list) and file_ not in required_paths:
            errors.append(
                f"{path}.success_criteria[{index}]: file {file_!r} is not a member of "
                f"its own required_paths {required_paths!r}"
            )
    return errors


def validate_artifact(payload: Any, label: str) -> list[str]:
    protocol = payload.get("protocol") if isinstance(payload, dict) else None
    schema_file = PROTOCOL_TO_SCHEMA.get(protocol)
    if schema_file is None:
        return [f"{label}: unrecognized or missing protocol {protocol!r}"]
    schema_doc = load_schema(schema_file)
    keyword_errors = check_schema_keywords(schema_doc, schema_doc, schema_file, set())
    if keyword_errors:
        return keyword_errors
    errors = validate(payload, schema_doc, schema_doc, schema_file, label)
    if protocol == "improvement-proposal/1" and isinstance(payload, dict):
        errors.extend(check_accepted_ruling(payload, label))
        errors.extend(check_success_criteria_paths(payload, label))
    return errors


def check_cited_patterns_exist(parsed: list[tuple[str, Any]]) -> list[str]:
    known_pattern_ids = {
        payload.get("pattern_id")
        for _, payload in parsed
        if isinstance(payload, dict) and payload.get("protocol") == "blocker-pattern/1"
    }
    errors: list[str] = []
    for label, payload in parsed:
        if not isinstance(payload, dict) or payload.get("protocol") != "improvement-proposal/1":
            continue
        pattern_ids = payload.get("pattern_ids")
        if not isinstance(pattern_ids, list):
            continue
        for pattern_id in pattern_ids:
            if pattern_id not in known_pattern_ids:
                errors.append(
                    f"{label}: cites pattern_id {pattern_id!r} which does not exist as "
                    "any blocker-pattern/1 artifact's pattern_id in the tree"
                )
    return errors


def check_tree(root: Path) -> list[str]:
    if not root.exists():
        return [f"{root}: root does not exist"]
    errors: list[str] = []
    parsed: list[tuple[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        label = str(path.relative_to(root))
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as exc:
            errors.append(f"{label}: invalid JSON ({exc})")
            continue
        parsed.append((label, payload))
        errors.extend(validate_artifact(payload, label))
    errors.extend(check_cited_patterns_exist(parsed))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT / "docs" / "improvement",
        help="root of a committed improvement-artifact tree (default: docs/improvement)",
    )
    args = parser.parse_args(argv)

    errors = check_tree(args.root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"{len(errors)} improvement-artifact violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
