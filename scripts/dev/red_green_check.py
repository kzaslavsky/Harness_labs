#!/usr/bin/env python3
"""Red/green gate for contract-burden relaxation nodes.

RED  — extract the frozen base tree from Git, copy ONLY the node's new finding
       tests into it, and require pytest to FAIL there (the old harness lacks
       the behavior).
GREEN — require the finding tests plus the node's regression targets to PASS
       against the candidate worktree.

Exit 0 only when the red phase failed and the green phase passed. The verdict
is printed as JSON and written next to the run for audit adoption.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run(argv: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="frozen base commit sha")
    parser.add_argument(
        "--finding-tests", nargs="+", required=True,
        help="new test files that must fail on base and pass on candidate",
    )
    parser.add_argument(
        "--regression", nargs="*", default=[],
        help="existing pytest targets that must pass on the candidate",
    )
    parser.add_argument("--timeout", type=float, default=1500.0)
    arguments = parser.parse_args()

    worktree = Path.cwd().resolve()
    for test in arguments.finding_tests:
        if not (worktree / test).is_file():
            print(json.dumps({"verdict": "error", "missing_finding_test": test}))
            return 2

    verdict: dict[str, object] = {"base": arguments.base}
    with tempfile.TemporaryDirectory(prefix="red-green-base-") as tmp:
        base_root = Path(tmp) / "base"
        base_root.mkdir()
        archive = Path(tmp) / "base.tar"
        with archive.open("wb") as stream:
            extract = subprocess.run(
                ["git", "archive", arguments.base],
                cwd=worktree, stdout=stream, stderr=subprocess.PIPE, check=False,
            )
        if extract.returncode != 0:
            print(json.dumps({
                "verdict": "error",
                "git_archive_stderr": extract.stderr.decode()[-500:],
            }))
            return 2
        with tarfile.open(archive) as tar:
            tar.extractall(base_root, filter="data")
        for test in arguments.finding_tests:
            target = base_root / test
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(worktree / test, target)

        red = run(
            [sys.executable, "-m", "pytest", *arguments.finding_tests, "-q"],
            cwd=base_root, timeout=arguments.timeout,
        )
        verdict["red_exit_code"] = red.returncode
        verdict["red_tail"] = (red.stdout + red.stderr)[-1500:]
        if red.returncode == 0:
            verdict["verdict"] = "red-phase-passed-on-base"
            print(json.dumps(verdict, indent=2))
            return 1

    green = run(
        [sys.executable, "-m", "pytest",
         *arguments.finding_tests, *arguments.regression, "-q"],
        cwd=worktree, timeout=arguments.timeout,
    )
    verdict["green_exit_code"] = green.returncode
    verdict["green_tail"] = (green.stdout + green.stderr)[-1500:]
    verdict["verdict"] = (
        "red-green-proven" if green.returncode == 0 else "green-phase-failed"
    )
    print(json.dumps(verdict, indent=2))
    return 0 if green.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
