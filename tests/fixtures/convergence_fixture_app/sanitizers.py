"""Test-only pre_journal_sanitizer hooks for the CC-03 capture smoke test.

Loaded by scripts/ui_fidelity_capture.py's ``--sanitizer`` flag via a
``<path>:<callable>`` spec, never imported as a real package -- the fixture
directory has no ``__init__.py`` on purpose, matching the plan's directory
grant for this fixture (a recorded S2 deviation nothing enforces until
CC-07). Each hook has the ``(kind: str, content: bytes) -> bytes`` shape the
capture script's ``pre_journal_sanitizer`` hook contract requires.
"""

from __future__ import annotations


def identity_sanitizer(kind: str, content: bytes) -> bytes:
    """Pass every artifact through unchanged."""

    return content


def failing_sanitizer(kind: str, content: bytes) -> bytes:
    """Reject every artifact, so the capture run must abort (AC-CC03-4)."""

    raise ValueError(f"failing_sanitizer: refusing to journal artifact kind={kind!r}")


def marking_sanitizer(kind: str, content: bytes) -> bytes:
    """Append a visible marker to console-log artifacts only.

    Used to prove ordering: the persisted file and the evidence digest must
    reflect this marker, which only exists because the sanitizer ran before
    the artifact was journaled and digested.
    """

    if kind == "console_log":
        return content + b"\n<!-- sanitized -->"
    return content


_fails_after_first_cell_calls = 0


def fails_after_first_cell(kind: str, content: bytes) -> bytes:
    """Pass every artifact of the first cell, then reject.

    Six artifact kinds are captured per cell (``ARTIFACT_KINDS`` in
    ``scripts/ui_fidelity_capture.py``), so this lets exactly the first
    cell's six artifacts through before rejecting the seventh call. Used to
    prove the abort is not a rollback: the artifacts a sanitizer failure
    aborts *after* remain on disk, unlike an immediate first-call failure
    (``failing_sanitizer``), which leaves nothing persisted at all.
    """

    global _fails_after_first_cell_calls
    _fails_after_first_cell_calls += 1
    if _fails_after_first_cell_calls > 6:
        raise ValueError("fails_after_first_cell: refusing further artifacts")
    return content
