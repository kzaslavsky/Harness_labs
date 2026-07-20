#!/usr/bin/env python3
"""Validate foundational Harness Labs documents and machine contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LINK_PATTERN = re.compile(r"(?<!!)\[[^]]*\]\(([^)]+)\)")
STATUS_PATTERN = re.compile(r"\*{0,2}Status:\*{0,2}\s+\S+", re.IGNORECASE)
REQUIRED_FILES = (
    Path("AGENTS.md"),
    Path("README.md"),
    Path("docs/architecture/harness-contract.md"),
    Path("docs/architecture/context-engineering.md"),
    Path("docs/observability/logging-and-metrics.md"),
    Path("docs/decisions/README.md"),
    Path("docs/development/INDEX.md"),
    Path("docs/development/NEXT_STEPS.md"),
)


def documentation_files() -> list[Path]:
    return [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "logs" / "README.md",
        *sorted((ROOT / "docs").rglob("*.md")),
    ]


def check_required_files(errors: list[str]) -> None:
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")


def check_documentation(errors: list[str]) -> None:
    for path in documentation_files():
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        lines = path.read_text(encoding="utf-8").splitlines()
        if path.name != "README.md" and not any(STATUS_PATTERN.search(line) for line in lines[:12]):
            errors.append(f"{relative}: missing Status near the top")
        for number, line in enumerate(lines, 1):
            for match in LINK_PATTERN.finditer(line):
                target = match.group(1).split("#", 1)[0].strip()
                if not target or target.startswith(("#", "http://", "https://", "mailto:", "/")):
                    continue
                resolved = (path.parent / target).resolve()
                try:
                    resolved.relative_to(ROOT)
                except ValueError:
                    errors.append(f"{relative}:{number}: link escapes repository: {target}")
                else:
                    if not resolved.exists():
                        errors.append(f"{relative}:{number}: missing link target: {target}")


def check_schemas(errors: list[str]) -> None:
    schema_root = ROOT / "schemas"
    schemas = sorted(schema_root.glob("*.schema.json"))
    if not schemas:
        errors.append("no JSON schemas found")
        return
    ids: set[str] = set()
    for path in schemas:
        relative = path.relative_to(ROOT)
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: invalid JSON ({exc})")
            continue
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"{relative}: must use JSON Schema draft 2020-12")
        identifier = schema.get("$id")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{relative}: missing $id")
        elif identifier in ids:
            errors.append(f"{relative}: duplicate $id: {identifier}")
        else:
            ids.add(identifier)
        if schema.get("type") != "object":
            errors.append(f"{relative}: root type must be object")


def main() -> int:
    errors: list[str] = []
    check_required_files(errors)
    check_documentation(errors)
    check_schemas(errors)
    if errors:
        print("Repository contract check failed:")
        print("\n".join(f"  {error}" for error in errors))
        return 1
    print("Repository contract check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
