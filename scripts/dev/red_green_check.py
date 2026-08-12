#!/usr/bin/env python3
"""Red/green gate for contract-burden relaxation nodes.

RED  — extract the frozen base tree from Git, copy ONLY the node's new finding
       tests into it, and require pytest to fail BEHAVIORALLY there: exit code
       1, at least one test reported as failed, zero errors. ImportError,
       collection error, usage error, or empty collection is NOT valid red
       evidence (pytest exit codes 2/3/4/5, or an "error" count in the
       summary).
GREEN — require the finding tests plus the node's regression targets to PASS
       against the candidate worktree.

Exit 0 only when the red phase failed behaviorally and the green phase passed.
The verdict is printed as JSON; a phase timeout produces a JSON verdict, not a
traceback.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run_pytest(targets: list[str], cwd: Path, timeout: float) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *targets, "-q"],
            cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "timed_out": True,
            "tail": (str(exc.stdout or "") + str(exc.stderr or ""))[-1200:],
            "failed": 0,
            "errors": 0,
        }
    output = completed.stdout + completed.stderr
    summary = {"failed": 0, "errors": 0}
    for line in reversed(output.splitlines()):
        failed = re.search(r"(\d+) failed", line)
        errors = re.search(r"(\d+) errors?\b", line)
        if failed or errors:
            summary["failed"] = int(failed.group(1)) if failed else 0
            summary["errors"] = int(errors.group(1)) if errors else 0
            break
    return {
        "exit_code": completed.returncode,
        "timed_out": False,
        "tail": output[-1200:],
        **summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="frozen base commit sha")
    parser.add_argument(
        "--finding-tests", nargs="+", required=True,
        help="new test files that must fail behaviorally on base and pass on candidate",
    )
    parser.add_argument(
        "--regression", nargs="*", default=[],
        help="existing pytest targets that must pass on the candidate",
    )
    parser.add_argument("--timeout", type=float, default=700.0,
                        help="per-phase pytest timeout in seconds")
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

        red = run_pytest(list(arguments.finding_tests), base_root, arguments.timeout)
        verdict["red"] = red
        if red["timed_out"]:
            verdict["verdict"] = "red-phase-timeout"
            print(json.dumps(verdict, indent=2))
            return 1
        if red["exit_code"] == 0:
            verdict["verdict"] = "red-phase-passed-on-base"
            print(json.dumps(verdict, indent=2))
            return 1
        if red["exit_code"] != 1 or red["failed"] < 1 or red["errors"] > 0:
            # Non-behavioral failure: collection error, ImportError, usage
            # error, or empty collection. Not admissible red evidence.
            verdict["verdict"] = "red-phase-not-behavioral"
            print(json.dumps(verdict, indent=2))
            return 1

    green = run_pytest(
        [*arguments.finding_tests, *arguments.regression], worktree, arguments.timeout
    )
    verdict["green"] = green
    if green["timed_out"]:
        verdict["verdict"] = "green-phase-timeout"
    elif green["exit_code"] == 0:
        verdict["verdict"] = "red-green-proven"
    else:
        verdict["verdict"] = "green-phase-failed"
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["verdict"] == "red-green-proven" else 1


if __name__ == "__main__":
    raise SystemExit(main())
