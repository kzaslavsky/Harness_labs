#!/usr/bin/env python3
"""Deterministic gate for workaround retirement and diagnosis closure.

`check_tree(root)` inspects one checkout (the experiment launcher and the
living diagnosis) and returns every pending retirement/closure as a list of
strings:

1. BASE_INSTRUCTIONS no longer pins bare criterion ids or frozen
   required_capabilities (those pins exist only because of the gates CB-01
   removed).
2. Plan-graph criteria bind with source "plan", not the "operator" workaround.
3. assemble_decomposition no longer mechanically appends objectives/criterion
   statements to plan sections (the compliance transformation CB-02 removed).
4. Every diagnosis item a program-1 node resolved records its landing node
   ("landed (CB-…" appears at least 6 times; no worklist item section still
   says "Status:** open" for items 1, 2, 5, 7 at minimum).
5. (CB2-08) The inert CB-1 launcher no longer pins the `claims_rule` block
   into its review/fix/verify stage instructions (item 11's workaround).
6. (CB2-08) Diagnosis items 8, 10, 11, 14, and 16 each carry a "landed
   (CB2-NN, commit <40-hex sha>)" closure and no residual "open" or
   "landed in part" status.

`--dual-phase --base <sha> --regression <targets...>` runs `check_tree`
against a `git archive` extraction of `<sha>` (RED phase — must name every
one of CB2-08's own pending markers, i.e. the claims_rule pin plus each of
items 8, 10, 11, 14, 16, since the RED_BASE checkout predates this program)
and then against the live worktree (GREEN phase — must find none), followed
by the regression pytest targets on the live worktree. Exit 0 only when RED
named every expected marker, GREEN found none, and the regression suite
passed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_REL = Path("experiments/run_burden_plan_graph.py")
DIAGNOSIS_REL = Path("docs/development/contract-burden-reduction.md")

# Items whose closure CB2-08 records: (second half of) 8, (general half of)
# 10, 11, 14, 16.
RETIREMENT_ITEMS = (8, 10, 11, 14, 16)

_LANDED_CB2_CLOSURE = re.compile(
    r"landed \([^)]*CB2-\d{2},\s*commit\s*`?[0-9a-fA-F]{40}[^)]*\)"
)
_STATUS_OPEN = re.compile(r"\*\*Status:\*\*\s*open\b", re.IGNORECASE)
_LANDED_IN_PART = re.compile(r"\blanded in part\b", re.IGNORECASE)
_STRIKETHROUGH = re.compile(r"~~.*?~~", re.DOTALL)


def _item_section(diagnosis: str, item_number: int) -> str | None:
    match = re.search(
        rf"### {item_number}\..*?(?=\n### |\n## )", diagnosis, re.DOTALL
    )
    return match.group(0) if match else None


def _expected_red_markers() -> list[str]:
    """Failure strings this node's own CB2-08 assertions must produce on
    RED_BASE — the claims_rule pin plus each of RETIREMENT_ITEMS. A RED
    phase that doesn't name every one of these is not proof that this
    node's own checks distinguish base from candidate."""
    markers = [
        "runner still pins the claims_rule block into review/fix/verify "
        "stage instructions (item 11 workaround not retired)"
    ]
    for item_number in RETIREMENT_ITEMS:
        markers.append(
            f"diagnosis item {item_number}: pending retirement — no "
            "landed (CB2-NN, commit <sha>) closure"
        )
    return markers


def check_tree(root: Path) -> list[str]:
    failures: list[str] = []
    runner = (root / RUNNER_REL).read_text(encoding="utf-8")
    if "bare criterion ids" in runner:
        failures.append("runner still pins bare criterion ids in BASE_INSTRUCTIONS")
    if "required_capabilities and details schema unchanged" in runner.replace("\n", " "):
        failures.append("runner still pins frozen required_capabilities in BASE_INSTRUCTIONS")
    if '"source": "operator"' in runner:
        failures.append('runner still binds criteria with source "operator"')
    if '"source": "plan"' not in runner:
        failures.append('runner does not bind criteria with source "plan"')
    if "additions" in runner and "criterion statements" not in runner:
        # The normalization loop appends objective/criterion strings; its
        # signature variable is `additions`.
        failures.append("assemble_decomposition still normalizes sections mechanically")
    if "claims_rule" in runner or "CLAIMS CONTRACT" in runner:
        failures.append(
            "runner still pins the claims_rule block into review/fix/verify "
            "stage instructions (item 11 workaround not retired)"
        )

    diagnosis = (root / DIAGNOSIS_REL).read_text(encoding="utf-8")
    if len(re.findall(r"landed \(CB-", diagnosis)) < 6:
        failures.append("diagnosis records fewer than 6 landed items")
    for item in ("### 1\\.", "### 2\\.", "### 5\\.", "### 7\\."):
        section = re.search(item + r".*?(?=\n### |\n## )", diagnosis, re.S)
        if section and re.search(r"\*\*Status:\*\* open\b", section.group(0)):
            failures.append(f"diagnosis section {item} still open")

    for item_number in RETIREMENT_ITEMS:
        section = _item_section(diagnosis, item_number)
        if section is None:
            failures.append(f"diagnosis item {item_number}: section not found")
            continue
        current = _STRIKETHROUGH.sub("", section)
        if not _LANDED_CB2_CLOSURE.search(current):
            failures.append(
                f"diagnosis item {item_number}: pending retirement — no "
                "landed (CB2-NN, commit <sha>) closure"
            )
        if _STATUS_OPEN.search(current):
            failures.append(f"diagnosis item {item_number}: pending closure — status still open")
        if _LANDED_IN_PART.search(current):
            failures.append(
                f"diagnosis item {item_number}: pending closure — status still landed in part"
            )

    return failures


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


def dual_phase(base_sha: str, regression: list[str], timeout: float) -> int:
    verdict: dict[str, object] = {"base": base_sha}
    with tempfile.TemporaryDirectory(prefix="workaround-retirement-base-") as tmp:
        base_root = Path(tmp) / "base"
        base_root.mkdir()
        archive = Path(tmp) / "base.tar"
        with archive.open("wb") as stream:
            extract = subprocess.run(
                ["git", "archive", base_sha],
                cwd=ROOT, stdout=stream, stderr=subprocess.PIPE, check=False,
            )
        if extract.returncode != 0:
            verdict["verdict"] = "error"
            verdict["git_archive_stderr"] = extract.stderr.decode()[-500:]
            print(json.dumps(verdict, indent=2))
            return 2
        with tarfile.open(archive) as tar:
            tar.extractall(base_root, filter="data")

        red_failures = check_tree(base_root)
        verdict["red"] = {"failures": red_failures}
        missing_markers = [
            marker for marker in _expected_red_markers() if marker not in red_failures
        ]
        if missing_markers:
            # RED must name every one of this node's own pending
            # retirement/closure assertions, not merely be non-empty —
            # an unrelated legacy failure on base is not proof that CB2-08's
            # own checks distinguish base from candidate. Mirrors
            # red_green_check.py's "red-phase-passed-on-base" rejection.
            verdict["verdict"] = "red-phase-not-item-scoped"
            verdict["missing_markers"] = missing_markers
            print(json.dumps(verdict, indent=2))
            return 1

    green_failures = check_tree(ROOT)
    verdict["green"] = {"failures": green_failures}
    if green_failures:
        verdict["verdict"] = "green-phase-failed"
        print(json.dumps(verdict, indent=2))
        return 1

    if regression:
        pytest_result = run_pytest(regression, ROOT, timeout)
        verdict["regression"] = pytest_result
        if pytest_result["timed_out"]:
            verdict["verdict"] = "regression-timeout"
            verdict["timed_out"] = True
            print(json.dumps(verdict, indent=2))
            return 124
        if pytest_result["exit_code"] != 0:
            verdict["verdict"] = "regression-failed"
            print(json.dumps(verdict, indent=2))
            return 1

    verdict["verdict"] = "red-green-proven"
    print(json.dumps(verdict, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dual-phase", action="store_true")
    parser.add_argument("--base", default=None, help="RED_BASE commit sha (dual-phase only)")
    parser.add_argument(
        "--regression", nargs="*", default=[],
        help="pytest targets that must pass on the candidate (dual-phase only)",
    )
    parser.add_argument(
        "--timeout", type=float, default=3300.0,
        help="regression pytest timeout in seconds (dual-phase only)",
    )
    arguments = parser.parse_args()

    if arguments.dual_phase:
        if not arguments.base:
            print(json.dumps({"verdict": "error", "reason": "--dual-phase requires --base"}))
            return 2
        return dual_phase(arguments.base, arguments.regression, arguments.timeout)

    failures = check_tree(ROOT)
    print(json.dumps({"failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
