#!/usr/bin/env python3
"""Resolve one required broken Markdown link to one uniquely discoverable plan."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from state_io import StateError, atomic_write_bytes


def resolve(repo: Path, document: Path, broken_target: str, *, apply: bool = False) -> dict[str, str]:
    repo = repo.resolve()
    document = document.resolve()
    try:
        document.relative_to(repo)
    except ValueError:
        raise StateError("document must be inside repository") from None
    target = Path(broken_target)
    if target.is_absolute() or "#" in broken_target or "://" in broken_target:
        raise StateError("broken target must be a relative file link")
    if (document.parent / target).exists():
        raise StateError("link target is not missing")

    candidates = sorted(
        path.resolve() for path in repo.rglob(target.name)
        if path.is_file() and ".git" not in path.parts
    )
    if not candidates:
        feature = re.search(r"(?i)(q[0-9]+)", target.name)
        if feature:
            key = feature.group(1).lower()
            candidates = sorted(
                path.resolve() for path in (repo / "docs" / "archive").rglob("*.md")
                if key in path.name.lower() and "plan" in path.name.lower()
            )
    if len(candidates) != 1:
        raise StateError(f"required link recovery found {len(candidates)} candidate targets")

    replacement = Path(os.path.relpath(candidates[0], document.parent)).as_posix()
    text = document.read_text(encoding="utf-8")
    old = f"]({broken_target})"
    count = text.count(old)
    if count == 0:
        raise StateError("document does not contain the requested broken link")
    if apply:
        atomic_write_bytes(document, text.replace(old, f"]({replacement})").encode("utf-8"))
    return {
        "status": "resolved",
        "document": str(document),
        "broken_target": broken_target,
        "replacement": replacement,
        "occurrences": str(count),
        "applied": str(apply).lower(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("document", type=Path)
    parser.add_argument("broken_target")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = resolve(args.repo, args.document, args.broken_target, apply=args.apply)
    except StateError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
