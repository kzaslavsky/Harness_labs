#!/usr/bin/env python3
"""Check relative Markdown links without following paths outside the project."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.resolve()
EXEMPT_ROOTS = {".agents", ".claude", ".git", ".pytest_cache", ".venv", "venv", "node_modules", "logs", "docs/archive", "learnings/_human_only"}
LINK_PATTERN = re.compile(r"(?<!!)\[[^]]*\]\(([^)]+)\)")


def is_exempt(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    return any(relative == root or relative.startswith(root + "/") for root in EXEMPT_ROOTS)


def is_relative_target(target: str) -> bool:
    return not target.startswith(("#", "http://", "https://", "mailto:", "/"))


def main() -> int:
    broken: list[str] = []
    for markdown in sorted(ROOT.rglob("*.md")):
        if is_exempt(markdown):
            continue
        try:
            lines = markdown.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            broken.append(f"{markdown.relative_to(ROOT)}: unreadable ({exc})")
            continue
        for number, line in enumerate(lines, 1):
            for match in LINK_PATTERN.finditer(line):
                target = match.group(1).split("#", 1)[0].strip()
                if not target or not is_relative_target(target):
                    continue
                resolved = (markdown.parent / target).resolve()
                try:
                    resolved.relative_to(ROOT)
                except ValueError:
                    broken.append(f"{markdown.relative_to(ROOT)}:{number} escapes project: {target}")
                else:
                    if not resolved.exists():
                        broken.append(f"{markdown.relative_to(ROOT)}:{number} missing: {target}")
    if broken:
        print("Documentation link check failed:")
        print("\n".join(f"  {item}" for item in broken))
        return 1
    print("Documentation link check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
