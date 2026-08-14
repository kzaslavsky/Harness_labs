"""Shared pytest-output parsing for verification-command results.

``failing_identifiers`` reads pytest's own short-summary lines
(``FAILED <nodeid>`` / ``ERROR <nodeid>``) so every consumer shares the same
stable per-test identifier vocabulary instead of inventing a parallel one.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_FAILING_IDENTIFIER_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def failing_identifiers(command: Mapping[str, Any]) -> frozenset[str] | None:
    """Derive the stable per-test failing-identifier set from one command result.

    Reads pytest's own short-summary lines (``FAILED <nodeid>`` /
    ``ERROR <nodeid>``), the same stable identifier pytest uses to re-select
    a test, so callers share this ledger's ``failure_keys`` substrate
    (``reserve(failure_keys=...)`` / ``_failure_keys``) instead of inventing a
    parallel one. Returns ``None`` when no such line is present, so a rerun
    whose output cannot be parsed this way is treated as non-comparable
    rather than guessed at.
    """
    full_text = "\n".join(str(command.get(key, "")) for key in ("stdout", "stderr"))
    identifiers = frozenset(_FAILING_IDENTIFIER_RE.findall(full_text))
    return identifiers or None
