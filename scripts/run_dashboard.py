#!/usr/bin/env python3
"""Run the local, read-only Harness Labs dashboard API."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.observability.dashboard_server import (
    DashboardApplication,
    DashboardError,
    create_dashboard_server,
    load_audit_root_registry,
)

# Matches scripts/run_plan_graph.py's self-registration target exactly, so a
# graph launched (in any worktree) is discovered by a dashboard started with
# no arguments at all.
_DEFAULT_AUDIT_ROOT_REGISTRY = Path.home() / ".harness_labs" / "dashboard-audit-roots.json"
_AUDIT_ROOT_REGISTRY_ENV = "HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY"


def _default_audit_root_registry_path() -> Path:
    override = os.environ.get(_AUDIT_ROOT_REGISTRY_ENV)
    return Path(override).expanduser() if override else _DEFAULT_AUDIT_ROOT_REGISTRY


def _resolve_audit_roots(explicit_roots: list[Path], registry: Path | None) -> list[Path]:
    """Resolve configured audit roots.

    Falls back to the default user-level registry only when *neither*
    ``--audit-root`` nor ``--audit-root-registry`` was supplied; an explicit
    (even if empty-of-roots) ``--audit-root-registry`` is never silently
    overridden. The default registry is loaded through the same closed,
    bounded ``load_audit_root_registry`` used for an explicit registry, so
    it is still subject to the existing 16-root cap and validation.
    """
    roots = list(explicit_roots)
    if registry is not None:
        roots.extend(load_audit_root_registry(registry))
    elif not roots:
        default_registry = _default_audit_root_registry_path()
        if default_registry.is_file():
            roots.extend(load_audit_root_registry(default_registry))
    return roots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, action="append", default=[], help="direct parent of audited run directories; repeat for multiple roots")
    parser.add_argument("--audit-root-registry", type=Path, help="closed JSON registry containing additional audit roots")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--assets-root", type=Path, help="optional compiled dashboard asset directory")
    parser.add_argument("--refresh-seconds", type=float, default=2.0)
    args = parser.parse_args()
    try:
        roots = _resolve_audit_roots(args.audit_root, args.audit_root_registry)
    except (DashboardError, OSError, UnicodeDecodeError) as exc:
        # An invalid, oversized, unreadable, or non-UTF-8 registry (default
        # or explicit) must produce a clean, actionable error, not an
        # unhandled traceback -- this is the default-registry fallback path
        # taken with no arguments at all. OSError also covers a registry
        # file that exists but cannot be opened (e.g. permission denied).
        parser.error(f"failed to load audit root registry: {exc}")
    if not roots:
        parser.error(
            "at least one --audit-root or --audit-root-registry is required "
            f"(no default registry was found at {_default_audit_root_registry_path()})"
        )
    application = DashboardApplication(roots, assets_root=args.assets_root, refresh_seconds=args.refresh_seconds)
    server = create_dashboard_server(application, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
