#!/usr/bin/env python3
"""Measurer commissioning CLI (DTR-F4): pre-campaign calibration.

Drives both calibrations ``harness_labs.core.measurer_commissioning``
implements and seals both reports via
``harness_labs.plangraph.convergence_campaign.CampaignArtifactStore`` -- the
one artifact store every other campaign artifact already goes through, never
a second store:

* ``stability``: runs the capture matrix ``--runs`` times through
  ``scripts/ui_fidelity_capture.py`` (``--driver stub`` in CI; ``auto``/
  ``real`` follow the capture CLI's own driver-selection contract) and
  classifies each cell stable/unstable against ``--divergence-threshold``.
  Exits nonzero while any cell is chronically unstable and unruled
  (``--rulings-file``, a JSON mapping of cell id to ``{"disposition":
  "excluded" | "threshold_amended", "reason": ...}``).
* ``recall``: scores an injected inspector (``--inspector``, a
  ``module:callable`` reference, resolved the same way
  ``scripts/run_convergence_campaign.py`` resolves its sanitizer hook)
  against a seed-findings file (``--seed-findings``, the
  ``finding_intake --batch`` envelope shape).

Both reports are sealed under ``<--campaign-root>/artifacts``; the sealed
digests are ``build_campaign_config``'s ``stability_report_digest`` and
``recall_report_digest`` (``harness_labs/plangraph/convergence_campaign.py``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.core.measurer_commissioning import (  # noqa: E402
    MeasurerCommissioningError,
    build_stability_report,
    load_seed_findings,
    score_inspector_recall,
    stability_exit_code,
)
from harness_labs.plangraph.convergence_campaign import (  # noqa: E402
    CampaignArtifactStore,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "ui_fidelity_capture.py"


def _capture_subprocess_env() -> dict[str, str]:
    """The environment for a ``ui_fidelity_capture.py`` child process.

    That script imports ``harness_labs.core`` at module level and does no
    ``sys.path`` insertion of its own, so a bare ``sys.executable
    ui_fidelity_capture.py`` child (``sys.path[0]`` is the script's own
    directory, not the repo root) fails with ``ModuleNotFoundError`` unless
    the repo root is already on ``PYTHONPATH`` -- prepend it here so the
    subcommand works out of the box, the same way every other caller of the
    capture script must.
    """

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing}" if existing else str(REPO_ROOT)
    )
    return env


def _resolve_reference(reference: str) -> Callable[..., Any]:
    """Resolve a ``module:callable`` reference, matching
    ``scripts.run_convergence_campaign.resolve_pre_journal_sanitizer``'s own
    ``module:callable`` convention."""

    module_name, _, attribute = reference.partition(":")
    if not module_name or not attribute:
        raise MeasurerCommissioningError(
            f"reference {reference!r} must be a 'module:callable' reference"
        )
    try:
        module = importlib.import_module(module_name)
        hook = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise MeasurerCommissioningError(
            f"reference {reference!r} could not be resolved: {exc}"
        ) from exc
    if not callable(hook):
        raise MeasurerCommissioningError(f"reference {reference!r} is not callable")
    return hook


def _run_capture_attempt(
    *,
    app_dir: Path,
    matrix_path: Path,
    driver: str,
    python_path: str | None,
    out_root: Path,
    attempt: int,
) -> dict[str, Any]:
    """One full capture-matrix run via the shipped capture CLI, reduced to
    ``{cell_id: end_state_digest}`` -- the per-cell signal
    ``build_stability_report`` compares across attempts. Reachable cells
    only: an ``unreachable`` cell has no end-state digest to compare."""

    out_dir = out_root / f"attempt-{attempt}"
    argv = [
        sys.executable, str(CAPTURE_SCRIPT),
        "--app-dir", str(app_dir),
        "--matrix", str(matrix_path),
        "--out", str(out_dir),
        "--driver", driver,
    ]
    if python_path:
        argv += ["--python", python_path]
    completed = subprocess.run(
        argv, capture_output=True, text=True, env=_capture_subprocess_env(),
    )
    if completed.returncode != 0:
        raise MeasurerCommissioningError(
            f"capture attempt {attempt} exited {completed.returncode}: {completed.stderr}"
        )
    receipt = json.loads((out_dir / "receipt.json").read_text(encoding="utf-8"))
    return {
        cell["cell_id"]: cell["end_state_digests"]["read_1"]
        for cell in receipt["cells"]
        if cell["status"] != "unreachable"
    }


def _stability_runner(
    *,
    app_dir: Path,
    matrix_path: Path,
    driver: str,
    python_path: str | None,
    out_root: Path,
) -> Callable[[int], dict[str, Any]]:
    cache: dict[int, dict[str, Any]] = {}

    def runner(attempt: int) -> dict[str, Any]:
        if attempt not in cache:
            cache[attempt] = _run_capture_attempt(
                app_dir=app_dir, matrix_path=matrix_path, driver=driver,
                python_path=python_path, out_root=out_root, attempt=attempt,
            )
        return cache[attempt]

    return runner


def _seal_report(report: dict[str, Any], *, campaign_root: Path, out: Path | None) -> tuple[Path, Any]:
    destination = out or Path(tempfile.mkstemp(suffix=".json")[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    store = CampaignArtifactStore(campaign_root / "artifacts")
    record = store.seal(destination, media_type="application/json", retention="campaign")
    return destination, record


def cmd_stability(args: argparse.Namespace) -> int:
    work_dir = Path(tempfile.mkdtemp(prefix="commission-stability-"))
    runner = _stability_runner(
        app_dir=Path(args.app_dir), matrix_path=Path(args.matrix), driver=args.driver,
        python_path=args.capture_python, out_root=work_dir,
    )

    try:
        rulings = None
        if args.rulings_file:
            rulings = json.loads(Path(args.rulings_file).read_text(encoding="utf-8"))
            if not isinstance(rulings, dict):
                raise MeasurerCommissioningError(
                    f"--rulings-file {args.rulings_file} must hold a JSON object "
                    "mapping cell id to a ruling"
                )
        capture_matrix = sorted(runner(0))
        report = build_stability_report(
            capture_matrix, runs=args.runs, runner=runner,
            divergence_threshold=args.divergence_threshold, rulings=rulings,
        )
    except MeasurerCommissioningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    destination, record = _seal_report(
        report, campaign_root=Path(args.campaign_root),
        out=Path(args.out) if args.out else None,
    )
    for request in report["ruling_requests"]:
        print(f"RULING REQUIRED: {request['message']}", file=sys.stderr)
    print(f"stability report written to {destination}, sealed as digest {record.digest}")
    return stability_exit_code(report)


def cmd_recall(args: argparse.Namespace) -> int:
    try:
        seed_findings = load_seed_findings(Path(args.seed_findings))
        inspector = _resolve_reference(args.inspector)
        report = score_inspector_recall(seed_findings, inspector=inspector)
    except MeasurerCommissioningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    destination, record = _seal_report(
        report, campaign_root=Path(args.campaign_root),
        out=Path(args.out) if args.out else None,
    )
    print(
        f"recall report written to {destination}, sealed as digest {record.digest} "
        f"(recall={report['recall']:.4f})"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stability = subparsers.add_parser(
        "stability",
        help="run the capture matrix N times and classify per-cell stability",
    )
    stability.add_argument("--app-dir", required=True)
    stability.add_argument("--matrix", required=True)
    stability.add_argument("--driver", default="stub", choices=("auto", "stub", "real"))
    stability.add_argument("--capture-python", default=None)
    stability.add_argument("--runs", type=int, default=5)
    stability.add_argument("--divergence-threshold", type=float, required=True)
    stability.add_argument("--rulings-file", default=None)
    stability.add_argument("--campaign-root", required=True)
    stability.add_argument("--out", default=None)
    stability.set_defaults(func=cmd_stability)

    recall = subparsers.add_parser(
        "recall", help="score an injected inspector's recall against a seed-findings envelope",
    )
    recall.add_argument("--seed-findings", required=True)
    recall.add_argument(
        "--inspector", required=True,
        help="'module:callable' reference to the injected inspector callable",
    )
    recall.add_argument("--campaign-root", required=True)
    recall.add_argument("--out", default=None)
    recall.set_defaults(func=cmd_recall)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
