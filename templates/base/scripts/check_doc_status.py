#!/usr/bin/env python3
"""Check non-exempt Markdown files for a Status header near the top."""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXEMPT_ROOTS = {
    ".agents", ".claude", ".git", ".pytest_cache", ".venv", "venv",
    "node_modules", "logs", "docs/archive", "learnings/_human_only",
}
EXEMPT_FILES = {"README.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE.md"}
EXEMPT_PATTERNS = {"*_prompt*.md"}
STATUS_PATTERN = re.compile(r"\*{0,2}Status:\*{0,2}\s+\S+", re.IGNORECASE)


def is_exempt(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    if relative in EXEMPT_FILES:
        return True
    if any(relative == root or relative.startswith(root + "/") for root in EXEMPT_ROOTS):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in EXEMPT_PATTERNS)


def has_status(path: Path) -> bool:
    try:
        return any(STATUS_PATTERN.search(line) for _, line in zip(range(12), path.open(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return False


def main() -> int:
    missing = [path.relative_to(ROOT) for path in sorted(ROOT.rglob("*.md")) if not is_exempt(path) and not has_status(path)]
    if missing:
        print("Documentation status check failed:")
        print("\n".join(f"  MISSING: {path}" for path in missing))
        return 1
    print("Documentation status check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
