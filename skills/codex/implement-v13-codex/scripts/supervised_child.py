#!/usr/bin/env python3
"""Gate a child exec until its process identity has been durably recorded."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    """Wait for a release token and replace this process with the requested child."""
    if len(sys.argv) != 4:
        raise SystemExit("usage: supervised_child.py SPEC_JSON RELEASE_FILE EXIT_FILE")
    spec_path = Path(sys.argv[1])
    release_path = Path(sys.argv[2])
    exit_path = Path(sys.argv[3])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + float(spec.get("release_timeout_seconds", 30))
    while not release_path.exists():
        if time.monotonic() >= deadline:
            return 124
        time.sleep(0.02)
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise SystemExit("spec argv must be a nonempty string array")
    environment = spec.get("environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise SystemExit("spec environment contains an unsupported override")
    allowed = {
        "IMPLEMENT_V13_RUN_FEATURE_CHILD",
        "TMPDIR",
        "TMP",
        "TEMP",
        "CODEX_EPHEMERAL_SCRATCH",
    }
    if set(environment) - allowed:
        raise SystemExit("spec environment contains an unsupported override")
    marker = environment.get("IMPLEMENT_V13_RUN_FEATURE_CHILD")
    if marker not in {None, "1"}:
        raise SystemExit("spec environment contains an invalid controller marker")
    scratch_values = {
        environment[key]
        for key in ("TMPDIR", "TMP", "TEMP", "CODEX_EPHEMERAL_SCRATCH")
        if key in environment
    }
    if scratch_values and (
        len(scratch_values) != 1
        or not all(
            key in environment
            for key in ("TMPDIR", "TMP", "TEMP", "CODEX_EPHEMERAL_SCRATCH")
        )
        or not Path(next(iter(scratch_values))).is_absolute()
    ):
        raise SystemExit("spec environment contains an invalid scratch contract")
    child_environment = os.environ.copy()
    child_environment.update(environment)
    child = subprocess.Popen(argv, env=child_environment)
    forwarded: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        forwarded.append(signum)
        if child.poll() is None:
            child.send_signal(signum)

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        returncode = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        returncode = child.returncode if child.returncode is not None else 1
    temporary = exit_path.with_name(f".{exit_path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        record = {"exit_code": returncode}
        if forwarded:
            record["forwarded_signals"] = forwarded
        os.write(descriptor, (json.dumps(record) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, exit_path)
    directory = os.open(exit_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
