#!/usr/bin/env python3
"""Compile normative role-result schemas into the Codex provider dialect."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from state_io import StateError, canonical_bytes, sha256_bytes


COMPILER_VERSION = "implement-v13-codex/response-schema-compiler/1"
PRODUCTION_RESPONSE_SCHEMA_NAMES = (
    "closure-test-result.schema.json",
    "code-review.schema.json",
    "feature-coordinator-result.schema.json",
    "fix-result.schema.json",
    "implementation-result.schema.json",
    "phase-child.schema.json",
    "plan-review.schema.json",
    "plan.schema.json",
    "repair-design-result.schema.json",
    "repair-design-review-result.schema.json",
    "revised-plan.schema.json",
    "role-result.schema.json",
    "targeted-review.schema.json",
    "test-result.schema.json",
    "ui-walk-plan.schema.json",
    "ui-walk-result.schema.json",
)

# This is deliberately the small subset used by this package's role-result
# schemas. Expanding it requires provider certification and mutation tests.
_TRANSPORT_KEYWORDS = {
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "anyOf",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
}
_ANNOTATION_KEYWORDS = {"$schema", "$id", "title", "description", "examples"}
_LOCAL_ONLY_KEYWORDS = {"uniqueItems", "minProperties", "maxProperties"}


def _compile_node(value: Any, path: str) -> Any:
    if isinstance(value, list):
        return [_compile_node(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if not isinstance(value, dict):
        return copy.deepcopy(value)

    unsupported = sorted(
        set(value) - _TRANSPORT_KEYWORDS - _ANNOTATION_KEYWORDS - _LOCAL_ONLY_KEYWORDS
    )
    if unsupported:
        raise StateError(
            f"normative response schema uses unsupported provider semantics at {path}: "
            + ", ".join(unsupported)
        )
    result: dict[str, Any] = {}
    for key, child in value.items():
        if key in _ANNOTATION_KEYWORDS or key in _LOCAL_ONLY_KEYWORDS:
            continue
        if key == "properties":
            if not isinstance(child, dict):
                raise StateError(f"response schema properties must be an object at {path}")
            result[key] = {
                name: _compile_node(schema, f"{path}.properties.{name}")
                for name, schema in sorted(child.items())
            }
        elif key == "$defs":
            if not isinstance(child, dict):
                raise StateError(f"response schema $defs must be an object at {path}")
            result[key] = {
                name: _compile_node(schema, f"{path}.$defs.{name}")
                for name, schema in sorted(child.items())
            }
        elif key in {"items"}:
            result[key] = _compile_node(child, f"{path}.{key}")
        elif key == "anyOf":
            if not isinstance(child, list) or not child:
                raise StateError(f"response schema anyOf must be a nonempty array at {path}")
            result[key] = [
                _compile_node(item, f"{path}.anyOf[{index}]")
                for index, item in enumerate(child)
            ]
        else:
            result[key] = copy.deepcopy(child)

    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties")
        if not isinstance(properties, dict):
            raise StateError(f"provider object schema requires properties at {path}")
        normative_required = value.get("required", [])
        if (
            not isinstance(normative_required, list)
            or any(not isinstance(item, str) for item in normative_required)
            or len(normative_required) != len(set(normative_required))
            or not set(normative_required).issubset(properties)
        ):
            raise StateError(f"normative object required set is invalid at {path}")
        if set(normative_required) != set(properties):
            raise StateError(
                f"normative response object requires required == properties at {path}"
            )
        result["required"] = list(properties)
        result["additionalProperties"] = False
    return result


def validate_provider_schema(schema: dict[str, Any]) -> None:
    """Reject transport schemas outside the certified strict provider subset."""

    def walk(value: Any, path: str, *, map_values: bool = False) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        if not map_values:
            unknown = sorted(set(value) - _TRANSPORT_KEYWORDS)
            if unknown:
                raise StateError(
                    f"Codex response schema uses unsupported keyword at {path}: "
                    + ", ".join(unknown)
                )
            if ("const" in value or "enum" in value) and "type" not in value:
                raise StateError(
                    f"Codex response schema requires explicit type at {path}"
                )
            if value.get("type") == "array" and "items" not in value:
                raise StateError(
                    f"Codex response schema requires array items at {path}"
                )
            if "anyOf" in value and (
                not isinstance(value["anyOf"], list) or not value["anyOf"]
            ):
                raise StateError(
                    f"Codex response schema requires a nonempty anyOf at {path}"
                )
            if value.get("type") == "object" or "properties" in value:
                properties = value.get("properties")
                required = value.get("required")
                if not isinstance(properties, dict):
                    raise StateError(
                        f"Codex response schema requires object properties at {path}"
                    )
                if value.get("additionalProperties") is not False:
                    raise StateError(
                        f"Codex response schema requires additionalProperties=false at {path}"
                    )
                if (
                    not isinstance(required, list)
                    or any(not isinstance(item, str) for item in required)
                    or len(required) != len(set(required))
                    or set(required) != set(properties)
                ):
                    raise StateError(
                        f"Codex response schema requires required == properties at {path}"
                    )
        for key, child in value.items():
            if key in {"properties", "$defs"}:
                if not isinstance(child, dict):
                    raise StateError(f"Codex response schema {key} must be an object at {path}")
                for name, nested in child.items():
                    walk(nested, f"{path}.{key}.{name}")
            elif key in {"items"}:
                walk(child, f"{path}.{key}")
            elif key == "anyOf":
                walk(child, f"{path}.anyOf")

    walk(schema, "$")


def compile_transport_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return one deterministic provider transport schema."""
    if not isinstance(schema, dict):
        raise StateError("normative response schema must be an object")
    compiled = _compile_node(schema, "$")
    validate_provider_schema(compiled)
    return compiled


def canonical_schema_hashes(
    source_schema: dict[str, Any], transport_schema: dict[str, Any]
) -> dict[str, str]:
    """Return hashes over canonical semantic and provider representations."""
    return {
        "compiler_version": COMPILER_VERSION,
        "schema_source_sha256": sha256_bytes(canonical_bytes(source_schema)),
        "schema_transport_sha256": sha256_bytes(canonical_bytes(transport_schema)),
    }


def compile_schema_file(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError(f"normative response schema is unreadable: {type(exc).__name__}") from exc
    if not isinstance(source, dict):
        raise StateError("normative response schema must be an object")
    transport = compile_transport_schema(source)
    return source, transport, canonical_schema_hashes(source, transport)


def production_response_schema_paths(package_root: Path) -> list[Path]:
    paths = [package_root / "schemas" / name for name in PRODUCTION_RESPONSE_SCHEMA_NAMES]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise StateError(f"production response schema registry is incomplete: {', '.join(missing)}")
    return paths
