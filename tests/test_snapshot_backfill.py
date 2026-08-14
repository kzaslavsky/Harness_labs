"""Fixture-corpus hardening tests for historical PlanGraph snapshot
reconstruction (PlanGraph node DM-07).

The fixture corpus reproduces the degraded shapes verified against the
pre-2026-08-05 primary-checkout corpus (77 dirs under ``logs/runs``,
2026-07-31 -> 2026-08-11; see the "Historical reconstruction" section of
``docs/development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md``, DM-07):

- a complete terminal graph whose token usage lives only in its child
  FeatureRun directories (the graph's own ``summary.json`` carries no
  tokens);
- a terminal graph whose manifest predates the ``summary_sha256`` field and
  has no ``summary.json`` at all (25 of the 77 real dirs);
- a terminal graph whose sole FeatureRun writes a real, verified audit
  trail with zero usage records (not zero-valued tokens -- no records);
- a launcher-style directory with no ``events.jsonl`` (3 of the 77 real
  dirs) that must be skipped with a diagnostic rather than failing the
  sweep;
- an interrupted checkpoint, eligible only with ``--include-interrupted``.

Fixtures reuse ``tests.test_plangraph_snapshot``'s real, ``AuditJournal``
-verified construction helpers rather than synthetic dicts, for the same
reason that module does: the builder under test reads run directories
through ``build_run_catalog`` / ``project_run_metrics``, both of which
require an authenticated journal.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.observability.plangraph_snapshot import SnapshotSkipped, build_snapshot
from harness_labs.observability.run_catalog import build_run_catalog
from harness_labs.plangraph.plan_graph import FeatureRunOutcome, PlanGraph, register_plan_graph

from tests.test_plangraph_snapshot import (
    SCHEMA,
    ClosedSchemaValidator,
    _ACCEPTANCE_CRITERIA,
    _build_terminal_graph,
    _decomposition,
    _init_repository,
    _minimal_graph_audit,
)


def _no_usage_launcher():
    """A PlanGraph launcher whose one child writes a real, verified audit
    trail with zero ``backend_transport`` records -- the historical "zero
    token records" shape, distinct from a FeatureRun that never ran at all
    (``usage_records == 0``, which the shared rollup must report
    ``unavailable``, never ``0``)."""

    def launcher(request):
        journal = AuditJournal(Path(request.run_dir), request.feature_run_id, actor=AuditActor("controller", "controller"))
        descriptor = {
            "protocol": "harness-run-descriptor/1", "run_kind": "feature_run", "run_id": request.feature_run_id,
            "created_at": "2026-08-09T00:00:00Z", "objective": request.run.objective, "evidence_classification": "production_lifecycle",
            "repository": {"path": str(request.run_dir), "base_branch": "main", "base_commit": request.base_commit},
            "approved_plan": None,
            "parent_correlation": {"plan_graph_id": request.plan_graph_id, "plan_node_id": request.plan_node_id, "parent_run_id": request.plan_graph_id},
        }
        raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (journal.run_dir / "descriptor.json").write_bytes(raw)
        journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
        criteria = {cid: {"id": cid, "status": "satisfied"} for cid in request.run.criteria}
        journal.finalize("succeeded", result={"status": "succeeded"}, state={"controller": {"criteria": criteria, "tasks": {}, "findings": {}}, "review_fix": {"cycles": 0}})
        return FeatureRunOutcome(
            status="succeeded", candidate_commit=request.base_commit, evidence=None,
            plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
            feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
        )

    return launcher


def _strip_summary(run_dir: Path) -> None:
    """Rewrite a finalized graph's manifest to the pre-summary-tracking
    shape (no ``summary_sha256`` -- optional per ``_validate_manifest``) and
    delete ``summary.json``, reproducing the 25-of-77 real corpus dirs whose
    manifests predate that field, without breaking ``AuditJournal.verify``."""
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("summary_sha256", None)
    manifest.pop("manifest_hash", None)
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8",
    )
    (run_dir / "summary.json").unlink()


def _merge_run_root(run_root: Path, source: Path) -> None:
    """Fold one ``_build_terminal_graph`` run root into the shared corpus
    run root, merging infrastructure dirs (``.plan-graph-budgets``, etc.)
    that both sides otherwise create independently rather than colliding."""
    for item in source.iterdir():
        destination = run_root / item.name
        if item.is_dir() and destination.is_dir():
            for child in item.iterdir():
                os.rename(child, destination / child.name)
        else:
            os.rename(item, destination)


def _make_launcher_style_dir(run_root: Path, name: str) -> Path:
    """A directory shaped like the 3 real ``logs/runs`` dirs produced by an
    ad-hoc launcher tool: decomposition/launcher-script files and a
    ``feature-runs/`` subdirectory, but no ``events.jsonl`` -- not this
    repository's audit format at all."""
    directory = run_root / name
    directory.mkdir(parents=True)
    (directory / "decomposition.json").write_text("{}", encoding="utf-8")
    (directory / "feature_run_launcher.py").write_text("# launcher\n", encoding="utf-8")
    (directory / "feature-runs").mkdir()
    return directory


class _CliMixin:
    """Shared in-process CLI invocation with isolated stdio and env."""

    def _run_main(self, module, argv: list[str]) -> tuple[int, str, str]:
        old_argv = sys.argv
        sys.argv = argv
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = module.main()
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()


class FixtureCorpusTests(unittest.TestCase, _CliMixin):
    """AC-DM07-1: backfill over a fixture corpus reproducing each historical
    degradation yields schema-valid snapshots with correct ``data_quality``
    flags, derived wall times, tokens ``unavailable`` (not zero) for
    zero-record runs, and skips launcher-style dirs with a diagnostic
    instead of failing the corpus."""

    def _build_corpus(self, root: Path) -> Path:
        run_root = root / "runs"
        run_root.mkdir()

        (root / "complete-src").mkdir()
        complete_repository, _, _, complete_result = _build_terminal_graph(root / "complete-src", graph_attempt_id="complete-graph")
        self.assertEqual(complete_result.status, "succeeded")
        _merge_run_root(run_root, root / "complete-src" / "runs")

        (root / "no-summary-src").mkdir()
        no_summary_repository, _, _, no_summary_result = _build_terminal_graph(root / "no-summary-src", graph_attempt_id="no-summary-graph")
        self.assertEqual(no_summary_result.status, "succeeded")
        _merge_run_root(run_root, root / "no-summary-src" / "runs")
        _strip_summary(run_root / "no-summary-graph")

        (root / "zero-token-src").mkdir()
        zero_token_repository, zero_token_base_commit = _init_repository(root / "zero-token-src")
        zero_token_registration = register_plan_graph(
            repository=zero_token_repository, logical_graph_id="zero-token-graph",
            decomposition=_decomposition(zero_token_base_commit),
        )
        zero_token_graph = PlanGraph(
            zero_token_repository, zero_token_registration, _no_usage_launcher(),
            run_root=run_root, graph_run_id="zero-token-graph",
        )
        zero_token_result = zero_token_graph.run()
        self.assertEqual(zero_token_result.status, "succeeded")

        interrupted_audit = _minimal_graph_audit(run_root, "interrupted-graph")
        interrupted_audit.journal.finalize("interrupted", result={"status": "interrupted"}, state=interrupted_audit.journal.checkpoint_state())

        _make_launcher_style_dir(run_root, "launcher-style-run")

        self.repositories = {
            "complete-graph": complete_repository,
            "no-summary-graph": no_summary_repository,
            "zero-token-graph": zero_token_repository,
        }
        return run_root

    def test_dry_run_sweep_reconstructs_terminal_graphs_and_skips_the_rest_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)

            catalog = build_run_catalog(run_root)
            self.assertEqual({g["run_id"] for g in catalog["plan_graphs"]}, {"complete-graph", "no-summary-graph", "zero-token-graph", "interrupted-graph"})
            launcher_diagnostics = [d for d in catalog["diagnostics"] if d["run_id"] == "launcher-style-run"]
            self.assertEqual(len(launcher_diagnostics), 1)
            self.assertEqual(launcher_diagnostics[0]["code"], "corrupt_run")
            self.assertIn("journal", launcher_diagnostics[0]["message"])

            from scripts import build_plangraph_snapshot

            code, out, err = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(run_root), "--all-completed", "--dry-run"],
            )
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["failed"], 0, report["failed_details"])
            self.assertEqual(report["reconstructed"], 3)
            self.assertEqual(set(report["reconstructed_graph_ids"]), {"complete-graph", "no-summary-graph", "zero-token-graph"})
            self.assertEqual(report["skipped"], 1)
            self.assertEqual(report["skipped_details"][0]["graph_id"], "interrupted-graph")
            self.assertIn("interrupted", report["skipped_details"][0]["reason"])
            # The launcher-style dir is not a plan-graph run at all -- it
            # never enters --all-completed's target list, so the corpus
            # sweep as a whole neither fails nor reports it as skipped/failed.
            all_reported = set(report["reconstructed_graph_ids"]) | {d["graph_id"] for d in report["skipped_details"]} | {d["graph_id"] for d in report["failed_details"]}
            self.assertNotIn("launcher-style-run", all_reported)
            self.assertFalse((run_root / ".plan-graph-snapshots").exists(), "--dry-run must not write")

            # scanned_total covers every directory build_run_catalog saw
            # (PlanGraph and FeatureRun records alike, including the
            # excluded launcher-style dir), and diagnostics is the
            # catalog's own list -- an operator can reconcile the count
            # report against the corpus without inspecting the catalog
            # separately.
            self.assertEqual(report["scanned_total"], len(catalog["plan_graphs"]) + len(catalog["feature_runs"]))
            reported_launcher_diagnostics = [d for d in report["diagnostics"] if d["run_id"] == "launcher-style-run"]
            self.assertEqual(len(reported_launcher_diagnostics), 1)
            self.assertEqual(reported_launcher_diagnostics[0]["code"], "corrupt_run")

    def test_real_run_writes_snapshots_for_every_reconstructed_graph_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)
            from scripts import build_plangraph_snapshot

            code, out, err = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(run_root), "--all-completed"],
            )
            self.assertEqual(code, 0, err)
            snapshot_dir = run_root / ".plan-graph-snapshots"
            written = {path.stem for path in snapshot_dir.glob("*.json")}
            self.assertEqual(written, {"complete-graph", "no-summary-graph", "zero-token-graph"})

    def test_complete_graph_snapshot_is_schema_valid_and_graded_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)
            snapshot = build_snapshot(run_root, "complete-graph", repository=self.repositories["complete-graph"])

        ClosedSchemaValidator(SCHEMA).validate(snapshot)
        self.assertFalse(snapshot["data_quality"]["summary_missing"])
        self.assertFalse(snapshot["data_quality"]["token_records_missing"])
        self.assertEqual(snapshot["data_quality"]["completeness"], "complete")
        self.assertEqual(snapshot["timing"]["wall_clock_ms"]["state"], "available")
        # Token usage was recorded only in the child FeatureRun directories
        # (the graph's own run directory carries no backend_transport
        # events); the shared rollup must still see it.
        self.assertEqual(snapshot["graph_metrics"]["totals"]["tokens"]["state"], "available")
        self.assertGreater(snapshot["graph_metrics"]["totals"]["tokens"]["total_tokens"], 0)
        self.assertEqual(snapshot["outcome"]["acceptance_criteria"], _ACCEPTANCE_CRITERIA)

    def test_missing_summary_graph_derives_wall_time_and_flags_summary_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)
            snapshot = build_snapshot(run_root, "no-summary-graph", repository=self.repositories["no-summary-graph"])

        ClosedSchemaValidator(SCHEMA).validate(snapshot)
        self.assertTrue(snapshot["data_quality"]["summary_missing"])
        self.assertIn(
            "graph summary.json is unavailable; wall clock is reported unavailable unless derivable from another verified source",
            snapshot["data_quality"]["reconstruction_notes"],
        )
        # Derived, not unavailable: the pre-2026-08-05 corpus's ~25
        # summary-less dirs must still report a wall clock, honestly
        # flagged as derived (partial) rather than silently guessed as
        # "available" or dropped as "unavailable".
        wall = snapshot["timing"]["wall_clock_ms"]
        self.assertEqual(wall["state"], "partial")
        self.assertIsInstance(wall["value"], int)
        self.assertGreaterEqual(wall["value"], 0)
        self.assertIn("derived from first/last verified journal event timestamps", wall["reason"])
        # The shared graph_metrics rollup's own wall-time contract
        # (summary.json only) is untouched by this snapshot-level fallback.
        self.assertEqual(snapshot["graph_metrics"]["timing"]["wall_clock_ms"]["state"], "unavailable")
        self.assertEqual(snapshot["data_quality"]["completeness"], "partial")

    def test_missing_summary_graph_with_no_events_has_no_derivable_wall_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "runs"
            run_root.mkdir()
            # A minimal, real, terminal audit trail (no node dispatch) whose
            # manifest is stripped of summary tracking and whose journal
            # carries only the handful of lifecycle events around
            # registration and finalization -- fewer than two of which may
            # legitimately share one timestamp on a fast test machine.
            audit = _minimal_graph_audit(run_root, "bare-graph", nodes={})
            audit.journal.finalize("succeeded", result={"status": "succeeded"}, state=audit.journal.checkpoint_state())
            _strip_summary(run_root / "bare-graph")

            snapshot = build_snapshot(run_root, "bare-graph")

        ClosedSchemaValidator(SCHEMA).validate(snapshot)
        self.assertTrue(snapshot["data_quality"]["summary_missing"])
        wall = snapshot["timing"]["wall_clock_ms"]
        # Either derived from at least two distinct event timestamps, or
        # honestly unavailable -- never a fabricated zero.
        self.assertIn(wall["state"], ("partial", "unavailable"))
        if wall["state"] == "unavailable":
            self.assertIsNone(wall["value"])

    def test_zero_token_records_are_unavailable_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)
            snapshot = build_snapshot(run_root, "zero-token-graph", repository=self.repositories["zero-token-graph"])

        ClosedSchemaValidator(SCHEMA).validate(snapshot)
        tokens = snapshot["graph_metrics"]["totals"]["tokens"]
        self.assertEqual(tokens["state"], "unavailable")
        self.assertIsNone(tokens["total_tokens"])
        self.assertIsNone(tokens["input_tokens"])
        self.assertIsNone(tokens["output_tokens"])
        self.assertTrue(snapshot["data_quality"]["token_records_missing"])
        self.assertIn(
            "no FeatureRun in this graph reports verified token usage",
            snapshot["data_quality"]["reconstruction_notes"],
        )
        self.assertNotEqual(snapshot["data_quality"]["completeness"], "complete")

    def test_launcher_style_dir_is_not_a_plan_graph_and_yields_no_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)
            with self.assertRaises(SnapshotSkipped):
                build_snapshot(run_root, "launcher-style-run")

    def test_interrupted_graph_requires_opt_in_then_snapshots_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._build_corpus(root)
            with self.assertRaises(SnapshotSkipped):
                build_snapshot(run_root, "interrupted-graph")
            snapshot = build_snapshot(run_root, "interrupted-graph", include_interrupted=True)
            ClosedSchemaValidator(SCHEMA).validate(snapshot)
            self.assertEqual(snapshot["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
