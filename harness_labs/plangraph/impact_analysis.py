"""Read-only static-import impact analysis for PlanGraph ``required_paths``.

All file bytes are read through an injected ``source`` callable so the same
analysis runs against a filesystem fixture in tests and against git blobs at
``base_commit`` in production (wired at admission by a later node; this
module is not wired into approval or refinement). The AST walk generalizes
the technique of ``scripts/dev/check_import_boundaries.py`` — ``ast.walk``
covers every node, so imports written inside a function body (deferred
imports) are seen, not just module-scope imports — into a repository-agnostic
module-neighborhood computation. This module does not import that script.

The neighborhood vocabulary implements only ``imported_by`` and ``imports``.
A third kind, ``defines_referenced_name`` (files defining names the target
references at module level), is deliberately omitted: outside star-imports,
which are outside static analysis, a module-level free name reaching the
target must already have arrived via an ``import``, so it is already covered
by ``imports``/``imported_by``. This is a recorded refusal, not an oversight.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

SourceReader = Callable[[str], Optional[bytes]]

IMPORTED_BY = "imported_by"
IMPORTS = "imports"


class TargetNotSupported(Exception):
    """Raised internally when a target cannot be statically analyzed.

    Callers that must never raise (``assess_required_paths``) catch this and
    translate it into ``ImpactAssessment(supported=False, reason=...)``.
    """


@dataclass(frozen=True)
class ModuleNeighborhood:
    """Static import neighborhood of one target module.

    ``imported_by`` holds repository-relative paths of files that import the
    target module (module-scope or deferred/in-function); ``imports`` holds
    repository-relative paths of in-repository modules the target imports.
    ``skipped`` holds repository-relative paths of ``repo_paths`` candidates
    that could not be parsed while scanning for importers (syntax errors,
    unreadable bytes) and were therefore excluded from ``imported_by`` rather
    than silently treated as non-importers.
    """

    imported_by: FrozenSet[str]
    imports: FrozenSet[str]
    skipped: FrozenSet[str] = field(default_factory=frozenset)

    def edges(self) -> Tuple[Tuple[str, str], ...]:
        """Neighborhood paths paired with their edge kind, sorted for a
        deterministic iteration order."""

        edges = [(path, IMPORTED_BY) for path in sorted(self.imported_by)]
        edges += [(path, IMPORTS) for path in sorted(self.imports)]
        return tuple(edges)


@dataclass(frozen=True)
class ImpactAssessment:
    """Outcome of checking a ``required_paths`` declaration against a
    target's static import neighborhood.

    ``confirmed`` holds declared neighborhood paths present in
    ``required_paths``; ``missing`` holds neighborhood paths absent from
    ``required_paths``, each paired with its edge kind. No set of
    "unrelated declared paths" is computed here: paths in ``required_paths``
    outside the neighborhood are simply not examined.
    """

    supported: bool
    reason: str
    confirmed: FrozenSet[str] = field(default_factory=frozenset)
    missing: Tuple[Tuple[str, str], ...] = ()


def _module_identity(relative_path: str) -> Tuple[str, bool]:
    """Return (dotted module name, is_package) for a repo-relative .py path."""

    parts = list(PurePosixPath(relative_path).with_suffix("").parts)
    is_package = bool(parts) and parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _package_of(dotted: str, is_package: bool) -> str:
    """The dotted name relative imports resolve against (``__package__``)."""

    if is_package:
        return dotted
    if "." not in dotted:
        return ""
    return dotted.rsplit(".", 1)[0]


def _resolve_import(node: ast.AST, dotted: str, is_package: bool) -> List[str]:
    """Resolve one Import/ImportFrom node to absolute dotted module names it
    could reference, including the ``from pkg import submodule`` form where
    the imported name is itself a submodule rather than a plain attribute."""

    targets: List[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            targets.append(alias.name)
        return targets

    if not isinstance(node, ast.ImportFrom):
        return targets

    if node.level == 0:
        if node.module:
            targets.append(node.module)
            for alias in node.names:
                targets.append(f"{node.module}.{alias.name}")
        return targets

    package = _package_of(dotted, is_package)
    package_parts = package.split(".") if package else []
    trim = node.level - 1
    if trim > len(package_parts):
        return targets
    base_parts = package_parts[: len(package_parts) - trim] if trim else package_parts
    base = ".".join(base_parts)
    if node.module:
        full = f"{base}.{node.module}" if base else node.module
        targets.append(full)
        for alias in node.names:
            targets.append(f"{full}.{alias.name}")
    else:
        for alias in node.names:
            targets.append(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _parse(relative_path: str, source: SourceReader) -> ast.Module:
    try:
        raw = source(relative_path)
    except (OSError, LookupError) as exc:
        raise TargetNotSupported(f"could not read {relative_path!r}: {exc}") from exc
    if raw is None:
        raise TargetNotSupported(f"{relative_path!r} has no source bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TargetNotSupported(f"{relative_path!r} is not UTF-8 text") from exc
    try:
        return ast.parse(text, filename=relative_path)
    except SyntaxError as exc:
        raise TargetNotSupported(
            f"{relative_path!r} has a syntax error: {exc}"
        ) from exc


def _dotted_map(repo_paths: Iterable[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for path in repo_paths:
        if not path.endswith(".py"):
            continue
        dotted, _is_package = _module_identity(path)
        if dotted:
            mapping[dotted] = path
    return mapping


def module_neighborhood(
    target_path: str, repo_paths: Iterable[str], source: SourceReader
) -> ModuleNeighborhood:
    """Static import neighborhood of ``target_path`` within ``repo_paths``.

    Raises ``TargetNotSupported`` if the target is not a parseable ``.py``
    file. ``assess_required_paths`` is the exception-free entry point;
    callers wanting the raw neighborhood for a known-good target may call
    this directly (as the tests do for AC-EM-1).
    """

    if not target_path.endswith(".py"):
        raise TargetNotSupported(f"{target_path!r} is not a Python file")

    repo_paths = list(repo_paths)
    dotted_map = _dotted_map(repo_paths)
    target_dotted, target_is_package = _module_identity(target_path)

    target_tree = _parse(target_path, source)

    imports: set = set()
    for node in ast.walk(target_tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _resolve_import(node, target_dotted, target_is_package):
            hit = dotted_map.get(name)
            if hit and hit != target_path:
                imports.add(hit)

    imported_by: set = set()
    skipped: set = set()
    for path in repo_paths:
        if path == target_path or not path.endswith(".py"):
            continue
        try:
            tree = _parse(path, source)
        except TargetNotSupported:
            skipped.add(path)
            continue
        dotted, is_package = _module_identity(path)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if target_dotted in _resolve_import(node, dotted, is_package):
                found = True
                break
        if found:
            imported_by.add(path)

    return ModuleNeighborhood(
        imported_by=frozenset(imported_by),
        imports=frozenset(imports),
        skipped=frozenset(skipped),
    )


def assess_required_paths(
    target_path: str,
    required_paths: Iterable[str],
    repo_paths: Iterable[str],
    source: SourceReader,
) -> ImpactAssessment:
    """Check whether ``required_paths`` covers ``target_path``'s static
    import neighborhood.

    Never raises: a non-``.py`` target or a ``.py`` target with a syntax
    error comes back as ``supported=False`` with a non-empty ``reason``
    instead of a clean verdict or an exception.
    """

    try:
        neighborhood = module_neighborhood(target_path, repo_paths, source)
    except TargetNotSupported as exc:
        return ImpactAssessment(supported=False, reason=str(exc))

    required = frozenset(required_paths)
    confirmed: set = set()
    missing: List[Tuple[str, str]] = []
    for path, kind in neighborhood.edges():
        if path in required:
            confirmed.add(path)
        else:
            missing.append((path, kind))

    reason = ""
    if neighborhood.skipped:
        reason = (
            "candidate importer(s) could not be parsed and were excluded "
            f"from the scan: {', '.join(sorted(neighborhood.skipped))}"
        )

    return ImpactAssessment(
        supported=True,
        reason=reason,
        confirmed=frozenset(confirmed),
        missing=tuple(missing),
    )
