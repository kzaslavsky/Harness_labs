"""Persist and forward image artifacts produced by a verification command.

A deterministic verification command (in practice, a pytest invocation) may
write images -- screenshots, reference crops, pixel diffs -- while proving a
visual contract. Those images normally land under pytest's ``tmp_path``
fixture, which is deleted when the process exits, so the only thing that ever
reached the next repair round was the assertion's text. This module keeps the
pixels:

* :func:`pytest_basetemp_argv` redirects pytest's temporary tree at a stable,
  controller-owned directory via pytest's own ``--basetemp`` flag, which
  ``tmp_path``/``tmp_path_factory`` already honour. No test-file change is
  needed anywhere in the target repository.
* :func:`capture_failure_images` copies the images a *failing* run left behind
  into the run's evidence catalog, so they become durable audit artifacts with
  stable filesystem paths.
* :func:`attached_image_paths` reads those paths back out of a repair
  attempt's controller-supplied context, so an executor can hand the worker
  the actual pixels.

Every entry point is a no-op when the command is not pytest-shaped, when the
run produced no images, or when the feature is switched off, so a node whose
verification produces no images sees byte-identical behaviour.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CAPTURE_ENV_VAR",
    "FAILURE_IMAGE_CONTEXT_KEY",
    "SCOPE_EMPTY",
    "SCOPE_FAILING_TESTS",
    "SCOPE_WHOLE_TREE_NO_IDENTIFIERS",
    "SCOPE_WHOLE_TREE_NO_MATCH",
    "CapturedImages",
    "attached_image_paths",
    "capture_failure_images",
    "image_capture_enabled",
    "pytest_basetemp_argv",
]

# Key carried on the recorded verification-command mapping (which every repair
# context already embeds as ``failed_verification``) and therefore the key an
# executor reads back.
FAILURE_IMAGE_CONTEXT_KEY = "image_artifacts"

# Kill switch: set to 0/false/no/off to restore the exact prior behaviour.
CAPTURE_ENV_VAR = "HARNESS_VERIFICATION_IMAGE_CAPTURE"

# Bounds. These keep a pathological run (a test tree that writes hundreds of
# screenshots) from flooding the audit directory or the worker's context.
_DEFAULT_IMAGE_LIMIT = 6
_DEFAULT_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_SINGLE_BYTES = 4 * 1024 * 1024

_IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# The conventional trailing role a visual-diff helper gives each member of one
# comparison. Used only to group and rank; absent conventions fall back to
# newest-first over ungrouped files.
_ROLE_RE = re.compile(r"-(diff|actual|reference|expected|baseline|current)$")
_ROLE_ORDER = {
    "reference": 0,
    "expected": 0,
    "baseline": 0,
    "actual": 1,
    "current": 1,
    "diff": 2,
}

_FALSE_VALUES = {"0", "false", "no", "off"}

# How a capture arrived at the files it kept. Only the first is the intended
# path; the other two mean the selection fell through to the whole temporary
# tree and may therefore carry a passing test's images.
SCOPE_FAILING_TESTS = "failing-tests"
SCOPE_WHOLE_TREE_NO_IDENTIFIERS = "whole-tree-no-failing-identifiers"
SCOPE_WHOLE_TREE_NO_MATCH = "whole-tree-no-directory-match"
SCOPE_EMPTY = "empty"


@dataclass(frozen=True)
class CapturedImages:
    """One capture's persisted images, how they were chosen, and what they cost.

    ``scope`` is carried out of the selection rather than discarded because the
    fallback below is silent by construction: a scoping rule that matches
    nothing still yields a full-looking result set. The caller records it so a
    broken rule shows up in the audit trail instead of only in the pixels.
    """

    descriptors: tuple[dict[str, Any], ...] = ()
    scope: str = SCOPE_EMPTY
    total_bytes: int = 0
    limit: int = 0
    total_bytes_limit: int = 0
    considered: int = 0

    def __bool__(self) -> bool:
        return bool(self.descriptors)

    def __len__(self) -> int:
        return len(self.descriptors)


def image_capture_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether verification image capture is switched on."""

    source = os.environ if environ is None else environ
    raw = str(source.get(CAPTURE_ENV_VAR, "")).strip().lower()
    return raw not in _FALSE_VALUES if raw else True


def _is_pytest_argv(argv: Sequence[str]) -> bool:
    """Return whether ``argv`` invokes pytest, conservatively."""

    if not argv:
        return False
    head = Path(argv[0]).name.lower()
    if head.startswith("pytest") or head.startswith("py.test"):
        return True
    # ``python -m pytest ...`` (and ``.venv/bin/python3 -m pytest``).
    if not head.startswith("python"):
        return False
    for index, value in enumerate(argv[1:], start=1):
        if value == "-m":
            return index + 1 < len(argv) and argv[index + 1] == "pytest"
        if not value.startswith("-"):
            return False
    return False


def pytest_basetemp_argv(
    argv: Sequence[str],
    basetemp: Path | None,
) -> tuple[str, ...]:
    """Return ``argv`` with ``--basetemp`` appended when that is meaningful.

    Returns the input unchanged -- same values, same order -- unless every
    condition holds: capture is enabled, a basetemp was supplied, the command
    is pytest-shaped, and the caller has not already chosen a basetemp itself.
    A caller's own ``--basetemp`` always wins.
    """

    original = tuple(argv)
    if basetemp is None or not image_capture_enabled():
        return original
    if not _is_pytest_argv(original):
        return original
    if any(
        value == "--basetemp" or value.startswith("--basetemp=")
        for value in original
    ):
        return original
    return original + ("--basetemp", str(basetemp))


def _failing_test_dir_prefixes(command: Mapping[str, Any]) -> frozenset[str]:
    """Return the ``tmp_path`` directory-name stems of the failing tests.

    pytest's ``tmp_path`` fixture builds its directory name in ``_pytest``'s
    ``_mk_tmp``: it replaces every non-word character in the test's node name
    with ``_``, truncates to 30 characters, and hands that to
    ``mktemp(..., numbered=True)``, which appends a decimal ordinal. So
    ``test_import_region_matches[desktop]`` becomes
    ``test_import_region_matches_des0`` -- note that the truncation lands mid
    word, and that a parametrized test's variants can only be told apart by
    the ordinal once the stem is truncated.

    Reconstructing the stem from the reported failing node ids lets a capture
    keep the images the *failures* wrote and drop every passing test's.
    """

    # Imported lazily so this module stays importable in isolation.
    from harness_labs.core.test_output import failing_identifiers

    identifiers = failing_identifiers(command)
    if not identifiers:
        return frozenset()
    prefixes = set()
    for identifier in identifiers:
        name = identifier.rpartition("::")[2] or identifier
        prefixes.add(re.sub(r"\W", "_", name)[:30])
    return frozenset(prefixes)


def _matches_failing_test(relative: Path, stems: frozenset[str]) -> bool:
    """Return whether ``relative`` sits under a failing test's ``tmp_path``.

    The directory must be exactly one of the stems followed by pytest's
    ordinal digits. A prefix test would be wrong in both directions: it lets
    ``test_short0``'s stem claim ``test_short_but_longer_name0``, and it lets
    every stem claim pytest's ``<stem>current`` convenience symlink, which
    would then double-count the same pixels.
    """

    if not stems:
        return False
    top = relative.parts[0] if relative.parts else ""
    return any(
        top.startswith(stem) and top[len(stem) :].isdigit() for stem in stems
    )


def _group_key(path: Path) -> str:
    return _ROLE_RE.sub("", path.stem)


def _role_rank(path: Path) -> int:
    match = _ROLE_RE.search(path.stem)
    return _ROLE_ORDER.get(match.group(1), 3) if match else 3


def _select_images(
    basetemp: Path,
    command: Mapping[str, Any],
    *,
    limit: int,
) -> tuple[list[Path], str, int]:
    """Return the most explanatory images the failing run left in ``basetemp``.

    Also returns how the pool was chosen and how many image files were
    considered, so the caller can record a fallback rather than absorb it.
    """

    candidates: list[Path] = []
    for path in sorted(basetemp.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_SINGLE_BYTES:
                continue
        except OSError:
            continue
        candidates.append(path)
    if not candidates:
        return [], SCOPE_EMPTY, 0

    stems = _failing_test_dir_prefixes(command)
    scoped = [
        path
        for path in candidates
        if _matches_failing_test(path.relative_to(basetemp), stems)
    ]
    # The fallback is deliberately kept: dropping every image when the scoping
    # rule matches nothing would silently discard the only pixels the round
    # produced, and a test may legitimately write outside its own tmp_path.
    # But the fallback is exactly what made a broken scoping rule invisible, so
    # the reason is returned and audited rather than swallowed. A partial
    # mapping is still the better set.
    if scoped:
        pool, scope = scoped, SCOPE_FAILING_TESTS
    else:
        pool = candidates
        scope = (
            SCOPE_WHOLE_TREE_NO_IDENTIFIERS
            if not stems
            else SCOPE_WHOLE_TREE_NO_MATCH
        )

    groups: dict[str, list[Path]] = {}
    for path in pool:
        groups.setdefault(f"{path.parent}/{_group_key(path)}", []).append(path)

    def group_rank(members: list[Path]) -> tuple[int, float]:
        has_diff = any(_role_rank(member) == _ROLE_ORDER["diff"] for member in members)
        newest = max(
            (member.stat().st_mtime for member in members),
            default=0.0,
        )
        # A group carrying a diff is the one the run actually flagged.
        return (0 if has_diff else 1, -newest)

    ordered = [
        sorted(members, key=lambda path: (_role_rank(path), path.name))
        for _, members in sorted(groups.items(), key=lambda item: group_rank(item[1]))
    ]
    # Round-robin over the distinct directories (one per failing test) so a
    # budget of six images spreads across the failures instead of spending
    # itself on whichever test happened to sort first.
    by_directory: dict[str, list[list[Path]]] = {}
    for members in ordered:
        by_directory.setdefault(str(members[0].parent), []).append(members)

    selected: list[Path] = []
    while len(selected) < limit and any(by_directory.values()):
        for queue in by_directory.values():
            if not queue:
                continue
            for member in queue.pop(0):
                if len(selected) >= limit:
                    return selected, scope, len(pool)
                selected.append(member)
    return selected, scope, len(pool)


def capture_failure_images(
    *,
    command: Mapping[str, Any],
    basetemp: Path | None,
    evidence: Any,
    producer_task_id: str,
    limit: int = _DEFAULT_IMAGE_LIMIT,
    total_bytes: int = _DEFAULT_TOTAL_BYTES,
) -> CapturedImages:
    """Copy a failing run's image artifacts into the evidence catalog.

    Returns a :class:`CapturedImages` carrying one descriptor per persisted
    image (``relative_path``, ``evidence_ref``, ``path``, ``sha256``,
    ``size_bytes``, ``media_type``), the scope the selection actually used, and
    the bytes spent -- falsy when there is nothing to persist. Never raises: an
    unreadable temporary tree degrades to the prior text-only behaviour rather
    than failing an otherwise well-formed verification round.
    """

    empty = CapturedImages(limit=limit, total_bytes_limit=total_bytes)
    if basetemp is None or not image_capture_enabled():
        return empty
    if command.get("exit_code") == 0:
        return empty
    try:
        if not basetemp.is_dir():
            return empty
        chosen, scope, considered = _select_images(basetemp, command, limit=limit)
    except OSError:
        return empty

    # ``EvidenceRecord.audit_path`` is relative to the audit run directory;
    # the executors need an absolute path they can hand to a subprocess.
    audit = getattr(evidence, "audit", None)
    run_dir = getattr(audit, "run_dir", None)

    descriptors: list[dict[str, Any]] = []
    budget = total_bytes
    for path in chosen:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if len(raw) > budget:
            break
        media_type = _IMAGE_SUFFIXES[path.suffix.lower()]
        try:
            record = evidence.add(
                kind="verification-failure-image",
                content=raw,
                media_type=media_type,
                producer_task_id=producer_task_id,
            )
        except Exception:  # noqa: BLE001 - evidence failure must not fail a round
            continue
        budget -= len(raw)
        stored = record.audit_path
        absolute = (
            str(Path(run_dir) / stored)
            if stored and run_dir is not None
            else None
        )
        descriptors.append(
            {
                "relative_path": str(path.relative_to(basetemp)),
                "evidence_ref": record.ref,
                "path": absolute,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "media_type": media_type,
            }
        )
    return CapturedImages(
        descriptors=tuple(descriptors),
        scope=scope if descriptors else SCOPE_EMPTY,
        total_bytes=total_bytes - budget,
        limit=limit,
        total_bytes_limit=total_bytes,
        considered=considered,
    )


def attached_image_paths(context: Mapping[str, Any]) -> tuple[Path, ...]:
    """Return existing on-disk paths for images captured from a prior failure.

    Reads ``context["failed_verification"]["image_artifacts"]`` -- the shape
    :func:`capture_failure_images` records -- and keeps only entries whose file
    is still present. Returns an empty tuple for every context that has no such
    entry, which is every context outside a verification repair round.
    """

    if not image_capture_enabled():
        return ()
    failed = context.get("failed_verification")
    if not isinstance(failed, Mapping):
        return ()
    descriptors = failed.get(FAILURE_IMAGE_CONTEXT_KEY)
    if not isinstance(descriptors, Sequence) or isinstance(descriptors, (str, bytes)):
        return ()
    paths: list[Path] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, Mapping):
            continue
        raw = descriptor.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        candidate = Path(raw)
        try:
            if candidate.is_file():
                paths.append(candidate)
        except OSError:
            continue
    return tuple(paths)
