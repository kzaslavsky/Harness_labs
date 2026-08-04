#!/usr/bin/env python3
"""Execute an explicit, bounded runtime-smoke command manifest."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from state_io import atomic_write_json


def run_manifest(manifest_path: Path, cwd: Path, output: Path) -> dict[str, Any]:
    """Run argv-array checks without shell interpolation."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks = manifest.get("checks")
    if not isinstance(checks, list):
        raise ValueError("smoke manifest checks must be an array")
    results: list[dict[str, Any]] = []
    for check in checks:
        argv = check.get("argv") if isinstance(check, dict) else None
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise ValueError("each smoke check requires a nonempty string argv")
        started = time.monotonic()
        completed = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(check.get("timeout_seconds", 300)),
        )
        results.append(
            {
                "id": check.get("id"),
                "argv": argv,
                "exit_code": completed.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
                "passed": completed.returncode == 0,
            }
        )
    document = {
        "protocol": "implement-v13-codex/smoke-result/1",
        "status": "passed" if all(item["passed"] for item in results) else "failed",
        "checks": results,
    }
    atomic_write_json(output, document)
    return document


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("cwd", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    document = run_manifest(args.manifest, args.cwd, args.output)
    print(json.dumps(document, sort_keys=True))
    return 0 if document["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
