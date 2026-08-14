"""Finding test for CB2-02: red_green_check.py phase timeouts become
machine-readable to the existing verification-failure classifier.

Self-contained by construction (the red/green gate copies only this file into
the frozen base tree): it resolves the sibling ``red_green_check.py`` by a
path relative to its own location, so it always exercises whichever copy
(base or candidate) lives alongside it in the tree currently under test, and
it imports nothing that does not already exist at the base commit. All
sandbox-repo construction happens inside test methods (never setUp/
setUpClass), and every assertion is a controlled ``assert*`` so a base-harness
rejection surfaces as a pytest FAILED, never an ERROR.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness_labs.featurerun.feature_run import classify_verification_failure

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "red_green_check.py"

_GIT_IDENTITY = ["-c", "user.email=probe@example.com", "-c", "user.name=probe"]


def _git(case: unittest.TestCase, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *_GIT_IDENTITY, *args], cwd=cwd, text=True, capture_output=True, check=False,
    )
    case.assertEqual(completed.returncode, 0, f"git {args} failed: {completed.stderr}")
    return completed


def _make_sandbox_repo(
    case: unittest.TestCase, root: Path, probe_body: str = "assert True"
) -> tuple[Path, str]:
    """A throwaway one-commit git repo that ``red_green_check.py`` can archive.

    Runs inside the calling test method so any construction failure is
    reported by pytest as FAILED (a controlled assertion), not ERROR.
    """

    sandbox = root / "sandbox"
    (sandbox / "tests").mkdir(parents=True)
    (sandbox / "tests" / "test_probe.py").write_text(f"def test_probe():\n    {probe_body}\n")
    _git(case, ["init", "-q"], sandbox)
    _git(case, ["add", "."], sandbox)
    _git(case, ["commit", "-q", "-m", "probe"], sandbox)
    base_sha = _git(case, ["rev-parse", "HEAD"], sandbox).stdout.strip()
    return sandbox, base_sha


class RelaxGateTimeoutClassificationTests(unittest.TestCase):
    """Drives real phase timeouts through the sibling red_green_check.py."""

    def test_red_phase_timeout_exits_124_and_classifies_infrastructure_transient(self) -> None:
        # A timeout far below any real pytest startup cost forces the red
        # phase to time out deterministically without relying on a sleep.
        with tempfile.TemporaryDirectory(prefix="relax-gate-red-timeout-") as tmp:
            sandbox, base_sha = _make_sandbox_repo(self, Path(tmp))
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--base", base_sha,
                    "--finding-tests", "tests/test_probe.py",
                    "--timeout", "0.01",
                ],
                cwd=sandbox, text=True, capture_output=True, check=False,
            )

        self.assertEqual(
            completed.returncode, 124,
            f"expected exit 124 on phase timeout, got {completed.returncode}; "
            f"stdout={completed.stdout!r}",
        )
        verdict = json.loads(completed.stdout)
        self.assertIs(verdict.get("timed_out"), True)
        self.assertEqual(verdict.get("verdict"), "red-phase-timeout")

        # Mirrors the shape feature_run._run_verification_command builds for
        # classify_verification_failure: the gate's own exit code and
        # captured text, with the *outer* subprocess call's timed_out flag
        # (unrelated to the gate's internal per-phase timeout) left False.
        command = {
            "exit_code": completed.returncode,
            "timed_out": False,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        result = classify_verification_failure(command)
        self.assertEqual(result["classification"], "infrastructure_transient")
        self.assertEqual(result["rule_id"], "timeout-exit-124")

    def test_green_phase_timeout_exits_124_with_top_level_timed_out_marker(self) -> None:
        # The committed probe fails immediately on base (no sleep), so the
        # red phase finishes well inside the timeout and the gate proceeds
        # to green. The regression target exists only in the worktree (it
        # is added after the base commit, so git archive never picks it up)
        # and sleeps past the timeout, so only the green phase can time out.
        with tempfile.TemporaryDirectory(prefix="relax-gate-green-timeout-") as tmp:
            sandbox, base_sha = _make_sandbox_repo(self, Path(tmp), probe_body="assert False")
            (sandbox / "tests" / "test_slow_regression.py").write_text(
                "import time\n\n\ndef test_slow():\n    time.sleep(10)\n"
            )
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--base", base_sha,
                    "--finding-tests", "tests/test_probe.py",
                    "--regression", "tests/test_slow_regression.py",
                    "--timeout", "2",
                ],
                cwd=sandbox, text=True, capture_output=True, check=False,
            )

        self.assertEqual(
            completed.returncode, 124,
            f"expected exit 124 on green-phase timeout, got {completed.returncode}; "
            f"stdout={completed.stdout!r}",
        )
        verdict = json.loads(completed.stdout)
        self.assertIs(verdict.get("timed_out"), True)
        self.assertEqual(verdict.get("verdict"), "green-phase-timeout")

    def test_non_timeout_verdict_shape_is_pinned(self) -> None:
        # A run that never times out must keep its current exit code and
        # shape byte-for-byte: no top-level timed_out key is introduced.
        with tempfile.TemporaryDirectory(prefix="relax-gate-shape-") as tmp:
            sandbox, base_sha = _make_sandbox_repo(self, Path(tmp))
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--base", base_sha,
                    "--finding-tests", "tests/test_probe.py",
                    "--timeout", "60",
                ],
                cwd=sandbox, text=True, capture_output=True, check=False,
            )

        self.assertEqual(completed.returncode, 1)
        verdict = json.loads(completed.stdout)
        self.assertEqual(verdict.get("verdict"), "red-phase-passed-on-base")
        self.assertNotIn("timed_out", verdict)


if __name__ == "__main__":
    unittest.main()
