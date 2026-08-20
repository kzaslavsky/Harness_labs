"""Tests for measurer commissioning (DTR-F4, ``dtr-mc``).

Covers AC-MC-1 (stability classification against a declared divergence
threshold, exercised over real stub-driver captures in CI, never skipped),
AC-MC-2 (chronically unstable cells surface as explicit ruling requests and
block success until ruled), and AC-MC-3 (inspector recall calibration
against a seed-findings file in the exact ``finding_intake --batch``
envelope shape, plus the core module's own plangraph-import-free layering).

A handful of ``scripts/commission_measurer.py`` CLI smoke tests exercise the
subprocess wiring (real stub-driver capture -> stability report; seed
envelope -> recall report), both sealed via ``CampaignArtifactStore``.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.measurer_commissioning import (
    MeasurerCommissioningError,
    RECALL_REPORT_PROTOCOL,
    STABILITY_REPORT_PROTOCOL,
    build_stability_report,
    load_seed_findings,
    score_inspector_recall,
    stability_exit_code,
)
from harness_labs.plangraph.convergence_campaign import CampaignArtifactStore
from harness_labs.plangraph.finding_intake import draft_findings_batch, seal_findings

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "ui_fidelity_capture.py"
COMMISSION_SCRIPT = REPO_ROOT / "scripts" / "commission_measurer.py"
FIXTURE_APP = REPO_ROOT / "tests" / "fixtures" / "convergence_fixture_app"
MATRIX_MINIMAL = FIXTURE_APP / "matrix_minimal.json"


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing}" if existing else str(REPO_ROOT)
    )
    return env


def _full_recall_inspector(findings):
    """A module-level inspector fixture, resolvable by the
    ``scripts/commission_measurer.py recall`` CLI's ``module:callable``
    reference (a subprocess importing this test module by dotted path):
    recovers every seed finding's key."""

    return [[finding["file"], finding["subject"]] for finding in findings]


# ---------------------------------------------------------------------------
# AC-MC-1 / AC-MC-2: stability classification, divergence threshold,
# ruling requests, nonzero exit until ruled
# ---------------------------------------------------------------------------


class StabilityClassificationTests(unittest.TestCase):
    def test_all_cells_identical_across_runs_are_stable_and_threshold_is_recorded(self) -> None:
        runner = lambda attempt: {"cell-a": "v1", "cell-b": "v2"}  # noqa: E731
        report = build_stability_report(
            ["cell-a", "cell-b"], runs=4, runner=runner, divergence_threshold=0.0,
        )
        self.assertEqual(report["protocol"], STABILITY_REPORT_PROTOCOL)
        self.assertEqual(report["divergence_threshold"], 0.0)
        self.assertEqual(report["runs"], 4)
        self.assertEqual(report["cells"]["cell-a"]["divergence"], 0.0)
        self.assertTrue(report["cells"]["cell-a"]["stable"])
        self.assertEqual(report["unstable_cells"], [])
        self.assertTrue(report["success"])
        self.assertEqual(stability_exit_code(report), 0)

    def test_divergent_cell_exceeding_threshold_is_unstable_and_requests_a_ruling(self) -> None:
        sequence = ["a", "a", "a", "b"]  # 1/4 divergence
        runner = lambda attempt: {"flaky": sequence[attempt]}  # noqa: E731
        report = build_stability_report(
            ["flaky"], runs=4, runner=runner, divergence_threshold=0.1,
        )
        self.assertFalse(report["cells"]["flaky"]["stable"])
        self.assertAlmostEqual(report["cells"]["flaky"]["divergence"], 0.25)
        self.assertEqual(report["unstable_cells"], ["flaky"])
        self.assertEqual(report["unruled_unstable_cells"], ["flaky"])
        self.assertEqual(len(report["ruling_requests"]), 1)
        self.assertIn("flaky", report["ruling_requests"][0]["message"])
        self.assertFalse(report["success"])
        self.assertEqual(stability_exit_code(report), 1)

    def test_divergence_exactly_at_threshold_is_stable(self) -> None:
        sequence = ["a", "a", "a", "b"]
        runner = lambda attempt: {"cell": sequence[attempt]}  # noqa: E731
        report = build_stability_report(
            ["cell"], runs=4, runner=runner, divergence_threshold=0.25,
        )
        self.assertTrue(report["cells"]["cell"]["stable"])
        self.assertTrue(report["success"])

    def test_excluded_ruling_resolves_an_unstable_cell_and_records_the_reason(self) -> None:
        sequence = ["a", "a", "a", "b"]
        runner = lambda attempt: {"flaky": sequence[attempt]}  # noqa: E731
        rulings = {
            "flaky": {"disposition": "excluded", "reason": "known viewport race, tracked in TICKET-1"},
        }
        report = build_stability_report(
            ["flaky"], runs=4, runner=runner, divergence_threshold=0.1, rulings=rulings,
        )
        self.assertIn("flaky", report["unstable_cells"])
        self.assertEqual(report["unruled_unstable_cells"], [])
        self.assertEqual(report["ruling_requests"], [])
        self.assertTrue(report["success"])
        self.assertEqual(stability_exit_code(report), 0)
        self.assertEqual(report["rulings"]["flaky"]["disposition"], "excluded")
        self.assertEqual(
            report["rulings"]["flaky"]["reason"], "known viewport race, tracked in TICKET-1",
        )

    def test_threshold_amended_ruling_requires_a_numeric_amended_threshold(self) -> None:
        sequence = ["a", "a", "a", "b"]
        runner = lambda attempt: {"flaky": sequence[attempt]}  # noqa: E731
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report(
                ["flaky"], runs=4, runner=runner, divergence_threshold=0.1,
                rulings={"flaky": {"disposition": "threshold_amended", "reason": "known race"}},
            )

    def test_threshold_amended_ruling_with_reason_resolves_the_cell(self) -> None:
        sequence = ["a", "a", "a", "b"]
        runner = lambda attempt: {"flaky": sequence[attempt]}  # noqa: E731
        report = build_stability_report(
            ["flaky"], runs=4, runner=runner, divergence_threshold=0.1,
            rulings={
                "flaky": {
                    "disposition": "threshold_amended", "reason": "known race",
                    "amended_threshold": 0.3,
                },
            },
        )
        self.assertEqual(report["unruled_unstable_cells"], [])
        self.assertTrue(report["success"])
        self.assertEqual(report["rulings"]["flaky"]["amended_threshold"], 0.3)

    def test_threshold_amended_ruling_that_does_not_cover_the_divergence_does_not_resolve(
        self,
    ) -> None:
        sequence = ["a", "a", "a", "b"]  # 0.25 divergence
        runner = lambda attempt: {"flaky": sequence[attempt]}  # noqa: E731
        report = build_stability_report(
            ["flaky"], runs=4, runner=runner, divergence_threshold=0.1,
            rulings={
                "flaky": {
                    "disposition": "threshold_amended", "reason": "known race",
                    "amended_threshold": 0.0,
                },
            },
        )
        self.assertEqual(report["unruled_unstable_cells"], ["flaky"])
        self.assertEqual(len(report["ruling_requests"]), 1)
        self.assertFalse(report["success"])
        self.assertEqual(stability_exit_code(report), 1)
        self.assertEqual(report["rulings"]["flaky"]["amended_threshold"], 0.0)

    def test_ruling_with_no_reason_is_rejected(self) -> None:
        runner = lambda attempt: {"cell": "v"}  # noqa: E731
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report(
                ["cell"], runs=2, runner=runner, divergence_threshold=0.0,
                rulings={"cell": {"disposition": "excluded", "reason": "   "}},
            )

    def test_ruling_with_invalid_disposition_is_rejected(self) -> None:
        runner = lambda attempt: {"cell": "v"}  # noqa: E731
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report(
                ["cell"], runs=2, runner=runner, divergence_threshold=0.0,
                rulings={"cell": {"disposition": "ignore", "reason": "whatever"}},
            )

    def test_multiple_unstable_cells_each_need_their_own_ruling(self) -> None:
        values = ["x", "y", "x", "y"]
        runner = lambda attempt: {"a": values[attempt], "b": values[attempt]}  # noqa: E731
        report = build_stability_report(
            ["a", "b"], runs=4, runner=runner, divergence_threshold=0.0,
            rulings={"a": {"disposition": "excluded", "reason": "known"}},
        )
        self.assertEqual(set(report["unstable_cells"]), {"a", "b"})
        self.assertEqual(report["unruled_unstable_cells"], ["b"])
        self.assertFalse(report["success"])
        self.assertEqual(stability_exit_code(report), 1)

    def test_runner_missing_a_cell_raises(self) -> None:
        runner = lambda attempt: {"only-one": "v"}  # noqa: E731
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report(
                ["only-one", "missing"], runs=2, runner=runner, divergence_threshold=0.0,
            )

    def test_empty_capture_matrix_raises(self) -> None:
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report([], runs=2, runner=lambda attempt: {}, divergence_threshold=0.0)

    def test_out_of_range_divergence_threshold_raises(self) -> None:
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report(
                ["cell"], runs=2, runner=lambda attempt: {"cell": "v"}, divergence_threshold=1.5,
            )

    def test_non_positive_runs_raises(self) -> None:
        with self.assertRaises(MeasurerCommissioningError):
            build_stability_report(
                ["cell"], runs=0, runner=lambda attempt: {"cell": "v"}, divergence_threshold=0.0,
            )


class StabilityStubDriverIntegrationTests(unittest.TestCase):
    """AC-MC-1: the classification is exercised over real stub-driver
    captures (a genuine subprocess run of ``scripts/ui_fidelity_capture.py
    --driver stub``, not a fake or mocked runner) in CI, unconditionally --
    this test carries no skip decorator and never skips."""

    def test_stability_report_classifies_real_stub_driver_captures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            attempts: dict[int, dict] = {}

            def runner(attempt: int) -> dict:
                if attempt not in attempts:
                    out_dir = work_root / f"attempt-{attempt}"
                    argv = [
                        sys.executable, str(CAPTURE_SCRIPT),
                        "--app-dir", str(FIXTURE_APP), "--matrix", str(MATRIX_MINIMAL),
                        "--out", str(out_dir), "--driver", "stub",
                    ]
                    completed = subprocess.run(
                        argv, capture_output=True, text=True, timeout=60, env=_subprocess_env(),
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    receipt = json.loads((out_dir / "receipt.json").read_text(encoding="utf-8"))
                    attempts[attempt] = {
                        cell["cell_id"]: cell["end_state_digests"]["read_1"]
                        for cell in receipt["cells"]
                        if cell["status"] != "unreachable"
                    }
                return attempts[attempt]

            capture_matrix = sorted(runner(0))
            self.assertIn("index.html|desktop|light|none", capture_matrix)
            self.assertIn("flaky.html|desktop|light|none", capture_matrix)

            # divergence_threshold=1.0 keeps this integration proof
            # deterministic regardless of real timing jitter in the
            # deliberately-flaky fixture route: the classification math
            # itself still runs, unskipped, over genuine stub-driver output.
            report = build_stability_report(
                capture_matrix, runs=3, runner=runner, divergence_threshold=1.0,
            )

        self.assertEqual(report["protocol"], STABILITY_REPORT_PROTOCOL)
        self.assertEqual(report["divergence_threshold"], 1.0)
        self.assertEqual(report["runs"], 3)
        # index.html carries no dynamic content: identical across every
        # independent stub-driver capture, so its divergence is genuinely
        # (not just by threshold generosity) zero.
        index_cell = report["cells"]["index.html|desktop|light|none"]
        self.assertEqual(index_cell["divergence"], 0.0)
        self.assertTrue(index_cell["stable"])
        self.assertIn("flaky.html|desktop|light|none", report["cells"])
        self.assertTrue(report["success"])
        self.assertEqual(stability_exit_code(report), 0)


# ---------------------------------------------------------------------------
# AC-MC-3: inspector recall against the finding_intake --batch envelope
# shape, plus the core module's own layering
# ---------------------------------------------------------------------------


class RecallScoringTests(unittest.TestCase):
    def _seed_envelope_path(self, tmp_path: Path) -> Path:
        """A real ``finding_intake --batch`` sealed envelope -- the exact
        artifact ``scripts/report_finding.py --batch`` emits -- built via
        the real ``draft_findings_batch``/``seal_findings`` pipeline against
        two symbols this very module defines, then read back as bytes and
        written out as a plain seed-findings file. Proves the envelope this
        test feeds ``load_seed_findings`` is genuinely that shape, not a
        hand-authored approximation of it."""

        findings = draft_findings_batch(
            [
                "`build_stability_report` in `harness_labs/core/measurer_commissioning.py` "
                "must record the divergence threshold",
                "`score_inspector_recall` in `harness_labs/core/measurer_commissioning.py` "
                "must reject an empty seed list",
            ],
            repo_root=REPO_ROOT, target="dtr-mc-seed",
        )
        store = CampaignArtifactStore(tmp_path / "artifacts")
        record = seal_findings(findings, store)
        seed_path = tmp_path / "seed-findings.json"
        seed_path.write_bytes(store.open_bytes(record.digest))
        return seed_path

    def test_load_seed_findings_reads_the_finding_intake_batch_envelope_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_path = self._seed_envelope_path(Path(tmp))
            seed_findings = load_seed_findings(seed_path)
        self.assertEqual(len(seed_findings), 2)
        self.assertTrue(all("file" in f and "subject" in f for f in seed_findings))
        subjects = {f["subject"] for f in seed_findings}
        self.assertEqual(subjects, {"build_stability_report", "score_inspector_recall"})

    def test_score_inspector_recall_full_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_findings = load_seed_findings(self._seed_envelope_path(Path(tmp)))

        report = score_inspector_recall(seed_findings, inspector=_full_recall_inspector)

        self.assertEqual(report["protocol"], RECALL_REPORT_PROTOCOL)
        self.assertEqual(report["seed_count"], 2)
        self.assertEqual(report["recall"], 1.0)
        self.assertEqual(report["missed"], [])

    def test_score_inspector_recall_partial_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_findings = load_seed_findings(self._seed_envelope_path(Path(tmp)))

        def inspector(findings):
            return [[findings[0]["file"], findings[0]["subject"]]]

        report = score_inspector_recall(seed_findings, inspector=inspector)
        self.assertEqual(report["recall"], 0.5)
        self.assertEqual(len(report["matched"]), 1)
        self.assertEqual(len(report["missed"]), 1)

    def test_score_inspector_recall_rejects_an_empty_seed_list(self) -> None:
        with self.assertRaises(MeasurerCommissioningError):
            score_inspector_recall([], inspector=lambda findings: [])

    def test_score_inspector_recall_rejects_a_malformed_inspector_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_findings = load_seed_findings(self._seed_envelope_path(Path(tmp)))
        with self.assertRaises(MeasurerCommissioningError):
            score_inspector_recall(seed_findings, inspector=lambda findings: "not-pairs")

    def test_load_seed_findings_rejects_a_non_envelope_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad.json"
            bad_path.write_text(json.dumps(["just", "a", "list"]), encoding="utf-8")
            with self.assertRaises(MeasurerCommissioningError):
                load_seed_findings(bad_path)


class ImportBoundaryTests(unittest.TestCase):
    """AC-MC-3 / AC-MC-5: the core module imports nothing from
    ``harness_labs.plangraph`` -- checked directly here (a static AST scan,
    matching how ``tests/test_import_boundaries.py``'s own checker works)
    as well as by that generic layer-boundary test."""

    def test_module_imports_nothing_from_plangraph(self) -> None:
        source_path = REPO_ROOT / "harness_labs" / "core" / "measurer_commissioning.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertFalse(
                    node.module.startswith("harness_labs.plangraph"),
                    f"measurer_commissioning.py imports from plangraph: {node.module}",
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("harness_labs.plangraph"),
                        f"measurer_commissioning.py imports plangraph module: {alias.name}",
                    )


# ---------------------------------------------------------------------------
# scripts/commission_measurer.py CLI smoke tests
# ---------------------------------------------------------------------------


class CommissionMeasurerCLITests(unittest.TestCase):
    def test_stability_subcommand_seals_a_report_and_exits_zero_when_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign_root = Path(tmp) / "campaign"
            out_path = Path(tmp) / "stability-report.json"
            argv = [
                sys.executable, str(COMMISSION_SCRIPT), "stability",
                "--app-dir", str(FIXTURE_APP), "--matrix", str(MATRIX_MINIMAL),
                "--driver", "stub", "--runs", "2", "--divergence-threshold", "1.0",
                "--campaign-root", str(campaign_root), "--out", str(out_path),
            ]
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, env=_subprocess_env(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(report["protocol"], STABILITY_REPORT_PROTOCOL)
            self.assertIn("sealed as digest", completed.stdout)
            store = CampaignArtifactStore(campaign_root / "artifacts")
            self.assertTrue(store.contains(_digest_from_stdout(completed.stdout)))

    def test_stability_subcommand_works_without_an_operator_exported_pythonpath(self) -> None:
        """Regression for MC-CLI-ENV: ``_run_capture_attempt`` must inject
        ``PYTHONPATH`` itself for the ``ui_fidelity_capture.py`` child, since
        that script does no ``sys.path`` insertion of its own. Spawns the CLI
        with ``PYTHONPATH`` stripped from the environment -- the CLI's own
        top-of-file ``sys.path.insert`` still lets *this* process import
        ``harness_labs.core``, but the capture-script grandchild only
        succeeds if the fix threads a repo-rooted ``PYTHONPATH`` through to
        it explicitly."""

        with tempfile.TemporaryDirectory() as tmp:
            campaign_root = Path(tmp) / "campaign"
            out_path = Path(tmp) / "stability-report.json"
            argv = [
                sys.executable, str(COMMISSION_SCRIPT), "stability",
                "--app-dir", str(FIXTURE_APP), "--matrix", str(MATRIX_MINIMAL),
                "--driver", "stub", "--runs", "2", "--divergence-threshold", "1.0",
                "--campaign-root", str(campaign_root), "--out", str(out_path),
            ]
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(report["protocol"], STABILITY_REPORT_PROTOCOL)

    def test_stability_subcommand_accepts_a_valid_rulings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign_root = Path(tmp) / "campaign"
            out_path = Path(tmp) / "stability-report.json"
            rulings_path = Path(tmp) / "rulings.json"
            rulings_path.write_text(json.dumps({}), encoding="utf-8")
            argv = [
                sys.executable, str(COMMISSION_SCRIPT), "stability",
                "--app-dir", str(FIXTURE_APP), "--matrix", str(MATRIX_MINIMAL),
                "--driver", "stub", "--runs", "2", "--divergence-threshold", "1.0",
                "--rulings-file", str(rulings_path),
                "--campaign-root", str(campaign_root), "--out", str(out_path),
            ]
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, env=_subprocess_env(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(report["rulings"], {})

    def test_stability_subcommand_rejects_a_malformed_rulings_file(self) -> None:
        """Regression for MC-CLI-RULING-UNTESTED: a non-mapping
        ``--rulings-file`` payload must produce the CLI's own ``error:``
        path and exit 2, not an uncaught ``AttributeError`` traceback."""

        with tempfile.TemporaryDirectory() as tmp:
            campaign_root = Path(tmp) / "campaign"
            out_path = Path(tmp) / "stability-report.json"
            rulings_path = Path(tmp) / "rulings.json"
            rulings_path.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")
            argv = [
                sys.executable, str(COMMISSION_SCRIPT), "stability",
                "--app-dir", str(FIXTURE_APP), "--matrix", str(MATRIX_MINIMAL),
                "--driver", "stub", "--runs", "2", "--divergence-threshold", "1.0",
                "--rulings-file", str(rulings_path),
                "--campaign-root", str(campaign_root), "--out", str(out_path),
            ]
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, env=_subprocess_env(),
            )
            self.assertEqual(completed.returncode, 2, completed.stdout)
            self.assertIn("error:", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(out_path.exists())

    def test_stability_subcommand_exits_nonzero_and_requests_a_ruling_for_an_unstable_unruled_cell(
        self,
    ) -> None:
        """Regression for MC-CLI-RULING-UNTESTED-R2: every other CLI
        stability test uses ``--divergence-threshold 1.0`` (always stable),
        so ``cmd_stability``'s own ``return stability_exit_code(report)`` and
        its ``RULING REQUIRED:`` stderr emission were never exercised
        through the command itself, only at the ``build_stability_report``
        unit layer. The shipped stub driver is fully deterministic, so a
        real subprocess capture can never actually diverge across attempts
        -- the CLI's injected-runner seam is substituted here instead, the
        same way the driver's own ``measure`` step treats capture as
        abstract."""

        from scripts import commission_measurer

        attempts = [{"only": "a"}, {"only": "b"}, {"only": "a"}]

        def fake_runner(attempt: int) -> dict[str, str]:
            return dict(attempts[attempt])

        with tempfile.TemporaryDirectory() as tmp:
            campaign_root = Path(tmp) / "campaign"
            out_path = Path(tmp) / "stability-report.json"
            args = argparse.Namespace(
                app_dir=str(FIXTURE_APP), matrix=str(MATRIX_MINIMAL), driver="stub",
                capture_python=None, runs=len(attempts), divergence_threshold=0.0,
                rulings_file=None, campaign_root=str(campaign_root), out=str(out_path),
            )
            original_stability_runner = commission_measurer._stability_runner
            commission_measurer._stability_runner = lambda **kwargs: fake_runner
            try:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    exit_code = commission_measurer.cmd_stability(args)
            finally:
                commission_measurer._stability_runner = original_stability_runner

            self.assertEqual(exit_code, 1)
            self.assertIn("RULING REQUIRED:", stderr.getvalue())
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertFalse(report["success"])
            self.assertEqual(report["unruled_unstable_cells"], ["only"])

    def test_recall_subcommand_seals_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings = draft_findings_batch(
                [
                    "`build_stability_report` in `harness_labs/core/measurer_commissioning.py` "
                    "must record the divergence threshold",
                ],
                repo_root=REPO_ROOT, target="dtr-mc-seed",
            )
            store = CampaignArtifactStore(tmp_path / "seed-artifacts")
            record = seal_findings(findings, store)
            seed_path = tmp_path / "seed-findings.json"
            seed_path.write_bytes(store.open_bytes(record.digest))

            campaign_root = tmp_path / "campaign"
            out_path = tmp_path / "recall-report.json"
            argv = [
                sys.executable, str(COMMISSION_SCRIPT), "recall",
                "--seed-findings", str(seed_path),
                "--inspector", "tests.test_measurer_commissioning:_full_recall_inspector",
                "--campaign-root", str(campaign_root), "--out", str(out_path),
            ]
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, env=_subprocess_env(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(report["protocol"], RECALL_REPORT_PROTOCOL)
            self.assertEqual(report["recall"], 1.0)
            self.assertIn("sealed as digest", completed.stdout)


def _digest_from_stdout(stdout: str) -> str:
    marker = "sealed as digest "
    index = stdout.index(marker) + len(marker)
    return stdout[index:].split()[0].strip()


if __name__ == "__main__":
    unittest.main()
