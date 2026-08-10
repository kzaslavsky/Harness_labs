#!/usr/bin/env python3
"""Run the local, read-only Harness Labs dashboard API."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.dashboard_server import (
    DashboardApplication,
    create_dashboard_server,
    load_audit_root_registry,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, action="append", default=[], help="direct parent of audited run directories; repeat for multiple roots")
    parser.add_argument("--audit-root-registry", type=Path, help="closed JSON registry containing additional audit roots")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--assets-root", type=Path, help="optional compiled dashboard asset directory")
    parser.add_argument("--refresh-seconds", type=float, default=2.0)
    args = parser.parse_args()
    roots = list(args.audit_root)
    if args.audit_root_registry is not None:
        roots.extend(load_audit_root_registry(args.audit_root_registry))
    if not roots:
        parser.error("at least one --audit-root or --audit-root-registry is required")
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
