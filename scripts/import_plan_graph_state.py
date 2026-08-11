#!/usr/bin/env python3
"""Refuse unsafe import of legacy sequential PlanGraph state."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "legacy PlanGraph state import is incompatible with immutable registered attempts",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
