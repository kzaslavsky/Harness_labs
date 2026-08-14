#!/usr/bin/env python3
"""Enforce the GraphRun layer boundaries by static AST analysis.

Layers and their allowed imports (GRAPHRUN_RESTRUCTURE_PLAN.md):

    core          -> core
    featurerun    -> core, featurerun
    plangraph     -> core, featurerun, plangraph
    observability -> core, observability
    graphrun      -> core, featurerun, plangraph, observability, graphrun

``harness_labs/__init__.py`` is the compatibility surface and is exempt.
Tests are not checked.

The walk covers **all** AST nodes, so deferred (in-function) imports are
seen — the ``development_policy`` -> ``feature_run_policy`` cycle that a
module-level-only scan misses is exactly why.

Phasing is tree-derived and flagless: a violation is an ERROR when the
importing module already lives inside its layer directory; while the module
is still flat (its layer directory move has not happened), the violation is
reported as a WARNING against its *future* layer from ``FUTURE_LAYERS``.
Exit status is 1 only on errors.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = "harness_labs"
LAYERS = ("core", "featurerun", "plangraph", "observability", "graphrun")

ALLOWED = {
    "core": {"core"},
    "featurerun": {"core", "featurerun"},
    "plangraph": {"core", "featurerun", "plangraph"},
    "observability": {"core", "observability"},
    "graphrun": {"core", "featurerun", "plangraph", "observability", "graphrun"},
}

# Future layer of every flat module (drives warn-mode before its move).
FUTURE_LAYERS = {
    "attempts": "core",
    "audit": "core",
    "usage": "core",
    "git_transaction": "core",
    "text_executor": "core",
    "backends": "core",
    "composition": "core",
    "agent_sessions": "core",
    "claude_agent_session": "core",
    "codex_agent_session": "core",
    "omlx_agent_session": "core",
    "claude_task_executor": "core",
    "model_capability_executor": "core",
    "codex_delegation": "core",
    "capability_broker": "core",
    "controller_commands": "core",
    "controller_evidence": "core",
    "controller_results": "core",
    "controller_kernel": "core",
    "controller_live": "core",
    "controller_live_scenarios": "core",
    "controller_projection": "core",
    "controller_scheduler": "core",
    "controller_coordinator": "core",
    "controller_run": "core",
    "coordinator_dispatcher": "core",
    "coordinator_schema": "core",
    "development_policy": "core",
    "test_output": "core",
    "feature_run": "featurerun",
    "feature_run_policy": "featurerun",
    "review_fix": "featurerun",
    "plan_graph": "plangraph",
    "plan_graph_audit": "plangraph",
    "plan_graph_contract": "plangraph",
    "plan_approval": "plangraph",
    "plan_graph_integration": "plangraph",
    "plan_graph_budget": "plangraph",
    "plan_graph_authority": "plangraph",
    "run_metrics": "observability",
    "run_metrics_index": "observability",
    "run_catalog": "observability",
    "dashboard_server": "observability",
    "agent_mixture": "graphrun",
}


def module_identity(path: Path, root: Path) -> tuple[str, str, bool]:
    """Return (dotted module name, layer, placed) for a package file."""

    rel = path.relative_to(root.parent)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    dotted = ".".join(parts)
    if len(parts) >= 2 and parts[1] in LAYERS:
        return dotted, parts[1], True
    stem = parts[-1] if len(parts) > 1 else ""
    return dotted, FUTURE_LAYERS.get(stem, ""), False


def resolve_target(node: ast.AST, current_parts: list[str]) -> list[str]:
    """Resolve an import node to dotted in-package target names."""

    targets: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                targets.append(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            if node.module and (
                node.module == PACKAGE or node.module.startswith(PACKAGE + ".")
            ):
                targets.append(node.module)
        else:
            base = current_parts[: len(current_parts) - node.level]
            if base and base[0] == PACKAGE:
                if node.module:
                    targets.append(".".join(base + node.module.split(".")))
                else:
                    for alias in node.names:
                        targets.append(".".join(base + [alias.name]))
    return targets


def target_layer(dotted: str) -> tuple[str, bool]:
    """Return (layer, placed) for a dotted in-package target."""

    parts = dotted.split(".")
    if len(parts) >= 2 and parts[1] in LAYERS:
        return parts[1], True
    if len(parts) >= 2:
        return FUTURE_LAYERS.get(parts[1], ""), False
    return "", False


def check(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == root / "__init__.py":
            continue
        dotted, layer, placed = module_identity(path, root)
        if not layer:
            continue
        current_parts = dotted.split(".")
        if not path.stem == "__init__":
            pass
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in resolve_target(node, current_parts):
                t_layer, _t_placed = target_layer(target)
                if not t_layer or t_layer in ALLOWED[layer]:
                    continue
                message = (
                    f"{path}:{node.lineno}: {layer} module `{dotted}` "
                    f"imports {t_layer}-layer `{target}`"
                )
                if placed:
                    errors.append(message)
                else:
                    warnings.append(message)
    return errors, warnings


def import_closure(module_stem: str, root: Path) -> set[str]:
    """Static in-package import closure of one module, deferred imports
    included. Returns flat/qualified stems of every reachable module."""

    def locate(stem: str) -> Path | None:
        flat = root / f"{stem}.py"
        if flat.exists():
            return flat
        for layer in LAYERS:
            placed = root / layer / f"{stem}.py"
            if placed.exists():
                return placed
        return None

    seen: set[str] = set()
    frontier = [module_stem]
    while frontier:
        stem = frontier.pop()
        if stem in seen:
            continue
        seen.add(stem)
        path = locate(stem)
        if path is None:
            continue
        dotted_parts = (
            [PACKAGE, path.parent.name, stem]
            if path.parent != root
            else [PACKAGE, stem]
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in resolve_target(node, dotted_parts):
                parts = target.split(".")
                stem_candidate = parts[-1] if len(parts) > 1 else None
                if stem_candidate and (
                    stem_candidate in FUTURE_LAYERS or locate(stem_candidate)
                ):
                    frontier.append(stem_candidate)
    seen.discard(module_stem)
    return seen


def closure_layers(module_stem: str, root: Path) -> dict[str, str]:
    """Map each closure member to its (current or future) layer."""

    result = {}
    for stem in import_closure(module_stem, root):
        layer = FUTURE_LAYERS.get(stem, "")
        for candidate_layer in LAYERS:
            if (root / candidate_layer / f"{stem}.py").exists():
                layer = candidate_layer
        if layer:
            result[stem] = layer
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[2] / PACKAGE
    errors, warnings = check(root)
    for line in warnings:
        print(f"WARN  {line}")
    for line in errors:
        print(f"ERROR {line}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
