from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.observability import graph_metrics
from harness_labs.observability.dashboard_server import (
    MAX_FILE_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SNAPSHOT_FILES,
    MIN_STALE_SNAPSHOT_SECONDS,
    STALE_SNAPSHOT_REFRESH_MULTIPLIER,
    DashboardApplication,
    DashboardError,
    _DashboardHandler,
    _apply_cumulative_node_metrics,
    build_run_detail,
    load_audit_root_registry,
)
from harness_labs.observability.plangraph_snapshot import SNAPSHOT_DIRNAME, build_snapshot, write_snapshot
from harness_labs.plangraph.plan_graph_audit import PlanGraphAudit
from scripts.dashboard_fixture_run import create_fixture
from scripts.run_dashboard import _resolve_audit_roots, main


def _registration_binding(graph_run_id: str) -> dict[str, str]:
    return {"logical_graph_id": graph_run_id, "registration_protocol": "plan-graph-registration/1",
            "registration_digest": "0" * 64, "graph_attempt_id": graph_run_id}


class DashboardApiTests(unittest.TestCase):
    def _run(self, root: Path, run_id: str = "run-1") -> None:
        journal = AuditJournal(root / run_id, run_id, actor=AuditActor("test", "test"))
        descriptor = {"protocol": "harness-run-descriptor/1", "run_kind": "feature_run", "run_id": run_id, "created_at": "2026-08-09T00:00:00Z", "objective": "test", "evidence_classification": "production_lifecycle", "repository": {"path": "/repo", "base_branch": "main", "base_commit": "a" * 40}, "approved_plan": None, "parent_correlation": None}
        raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (journal.run_dir / "descriptor.json").write_bytes(raw)
        journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
        journal.checkpoint("running", journal.checkpoint_state())

    def _request(self, app: DashboardApplication, method: str, path: str):
        handler = object.__new__(_DashboardHandler)
        handler.app = app
        handler.path = path
        handler.headers = {}
        sent = []
        handler._send = lambda status, body, **kwargs: sent.append((status, body, kwargs))
        getattr(handler, "do_" + method)()
        return sent[0]

    def test_catalog_endpoints_are_projected_and_etag_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            app = DashboardApplication(root, refresh_seconds=60)
            status, health, _ = self._request(app, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(health)["status"], "ok")
            status, catalog, _ = self._request(app, "GET", "/api/catalog")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(catalog)["feature_runs"][0]["run_id"], "run-1")
            status, details, _ = self._request(app, "GET", "/api/feature-runs/run-1")
            self.assertEqual(status, 200)
            self.assertIn("lifecycle", json.loads(details))
            self.assertEqual(self._request(app, "GET", "/api/plan-graphs")[0], 200)
            transport = type("Transport", (), {"headers": {}, "wfile": io.BytesIO(), "send_response": lambda self, value: setattr(self, "status", value), "send_header": lambda self, key, value: setattr(self, key.lower().replace("-", "_"), value), "end_headers": lambda self: None})()
            _DashboardHandler._send(transport, 200, catalog)
            transport.headers = {"If-None-Match": transport.etag}
            _DashboardHandler._send(transport, 200, catalog)
            self.assertEqual(transport.status, 304)

    def test_node_metrics_accumulate_across_plan_attempts_without_double_counting(self) -> None:
        def metrics(tokens: int, peak: int, stage: str) -> dict:
            totals = {"calls": 1, "input_tokens": tokens - 5, "cached_input_tokens": 0, "output_tokens": 5, "total_tokens": tokens, "duration_ms": 10, "wall_clock_ms": 20, "peak_input_tokens": peak, "cost": {"state": "estimated", "usd": 0.1, "reason": "estimate", "sources": ["pricing"], "estimated_records": 1, "long_context_records": 0}}
            row = {"label": "implement", **totals}
            return {"protocol": "harness-run-detail-metrics/1", "totals": totals, "quality": {"criteria_total": 1}, "by_phase": [row], "by_agent": [], "by_agent_type": [], "by_model": [], "by_effort": [], "by_backend": [], "stages": [{"label": stage}], "provenance": {"usage_records": 1, "collection_method": "verified", "peak_context_definition": "peak"}}

        catalog = {"plan_graphs": [
            {"run_id": "graph-1", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-1"}]},
            {"run_id": "graph-2", "created_at": "2026-08-09T01:00:00Z", "plan_digest": "plan", "predecessor_attempt_id": "graph-1", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-2"}]},
            {"run_id": "graph-3", "created_at": "2026-08-09T02:00:00Z", "plan_digest": "plan", "predecessor_attempt_id": "graph-2", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-3"}]},
        ]}
        details = {"try-1": {"metrics": metrics(100, 60, "first")}, "try-2": {"metrics": metrics(40, 25, "second")}, "try-3": {"metrics": metrics(10, 8, "third")}}
        _apply_cumulative_node_metrics(catalog, details)
        self.assertEqual(details["try-1"]["metrics"]["totals"]["total_tokens"], 100)
        cumulative = details["try-2"]["metrics"]
        self.assertEqual(cumulative["totals"]["total_tokens"], 140)
        self.assertEqual(cumulative["totals"]["peak_input_tokens"], 60)
        self.assertEqual(cumulative["provenance"]["attempt_count"], 2)
        self.assertEqual([row["label"] for row in cumulative["by_try"]], ["try-1", "try-2"])
        self.assertEqual([row["feature_run_id"] for row in cumulative["stages"]], ["try-1", "try-2"])
        self.assertEqual(details["try-3"]["metrics"]["totals"]["total_tokens"], 150)
        # DM-01: the merge is now served through graph_metrics; the merged
        # document also carries a labelled cumulative-quality block.
        self.assertEqual(cumulative["cumulative_quality"]["try_count"], 2)
        third = details["try-3"]["metrics"]["cumulative_quality"]
        self.assertEqual(third["try_count"], 3)

    def test_feature_run_endpoint_serves_cumulative_quality_across_plan_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()

            def run_with_review_fix(run_id: str, cycles: int) -> None:
                journal = AuditJournal(root / run_id, run_id, actor=AuditActor("test", "test"))
                descriptor = {"protocol": "harness-run-descriptor/1", "run_kind": "feature_run", "run_id": run_id, "created_at": "2026-08-09T00:00:00Z", "objective": "test", "evidence_classification": "production_lifecycle", "repository": {"path": "/repo", "base_branch": "main", "base_commit": "a" * 40}, "approved_plan": None, "parent_correlation": None}
                raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
                (journal.run_dir / "descriptor.json").write_bytes(raw)
                journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
                journal.checkpoint("running", {"controller": {"criteria": {}, "findings": {}}, "review_fix": {"cycles": cycles}})

            run_with_review_fix("try-1", cycles=2)
            run_with_review_fix("try-2", cycles=1)
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-1", plan=str(plan), plan_sha256=plan_sha,
                base_commit="a" * 40, registration_binding=_registration_binding("graph-1"),
                objective="first attempt",
                nodes={"FR-1": {"status": "failed", "feature_run_id": "try-1", "depends_on": []}},
                functionality_tests=(),
            )
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-2", plan=str(plan), plan_sha256=plan_sha,
                base_commit="a" * 40, registration_binding=_registration_binding("graph-2"),
                objective="second attempt retries the node",
                nodes={"FR-1": {"status": "running", "feature_run_id": "try-2", "depends_on": []}},
                functionality_tests=(),
            )
            # Accumulation follows recorded ancestry, not plan-digest
            # chronology: graph-2 names graph-1 via the predecessor-link
            # sidecar (the mechanism repair successors actually use), which
            # run_catalog projects into predecessor_attempt_id.
            (root / "graph-2" / "predecessor-link.json").write_text(
                json.dumps({
                    "protocol": "plan-graph-predecessor-link/1",
                    "predecessor_graph_run_id": "graph-1",
                }),
                encoding="utf-8",
            )
            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/feature-runs/try-2")

        self.assertEqual(status, 200)
        metrics = json.loads(body)["metrics"]
        # Cumulative across both tries (this exercises dashboard_server's
        # public delegation to graph_metrics end-to-end over the live HTTP path).
        self.assertEqual(metrics["cumulative_quality"]["review_cycles"], 3)
        self.assertEqual(metrics["cumulative_quality"]["try_count"], 2)
        # Latest-try quality (current-state) is retained, not summed.
        self.assertEqual(metrics["quality"]["review_cycles"], 1)

    def test_cumulative_reused_try_adds_no_spend_and_wall_busy_are_all_or_unavailable(self) -> None:
        def metrics(tokens: int, wall: int | None, busy: int | None) -> dict:
            totals = {"calls": 1, "input_tokens": tokens - 5, "cached_input_tokens": 0, "output_tokens": 5, "total_tokens": tokens, "duration_ms": 30, "wall_clock_ms": wall, "busy_ms": busy, "peak_input_tokens": tokens - 5, "cost": {"state": "estimated", "usd": 0.1, "reason": "estimate", "sources": ["pricing"], "estimated_records": 1, "long_context_records": 0}}
            return {"protocol": "harness-run-detail-metrics/1", "totals": totals, "quality": {}, "by_phase": [], "by_agent": [], "by_agent_type": [], "by_model": [], "by_effort": [], "by_backend": [], "stages": [], "provenance": {"usage_records": 1, "collection_method": "verified", "peak_context_definition": "peak"}}

        catalog = {"plan_graphs": [
            {"run_id": "graph-1", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-1"}]},
            {"run_id": "graph-2", "created_at": "2026-08-09T01:00:00Z", "plan_digest": "plan", "predecessor_attempt_id": "graph-1", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-2"}]},
            # The newest attempt REUSES the sealed node: its planned run
            # directory never existed, so a reuse contributes no new spend.
            {"run_id": "graph-3", "created_at": "2026-08-09T02:00:00Z", "plan_digest": "plan", "predecessor_attempt_id": "graph-2", "nodes": [{"node_id": "FR-1", "feature_run_id": "graph-3-FR-1", "reused_from_attempt": "graph-2"}]},
        ]}
        details = {"try-1": {"metrics": metrics(100, wall=20, busy=8)}, "try-2": {"metrics": metrics(40, wall=None, busy=9)}}

        _apply_cumulative_node_metrics(catalog, details)

        cumulative = details["try-2"]["metrics"]
        self.assertEqual(cumulative["provenance"]["attempt_count"], 2)
        self.assertEqual([row["label"] for row in cumulative["by_try"]], ["try-1", "try-2"])
        self.assertEqual(cumulative["totals"]["total_tokens"], 140)
        self.assertEqual(cumulative["totals"]["duration_ms"], 60)
        # One try is still running (no summary): a partial wall sum would
        # falsely display summed agent time exceeding wall time.
        self.assertIsNone(cumulative["totals"]["wall_clock_ms"])
        # Tries are sequential, so per-try busy unions add without overlap.
        self.assertEqual(cumulative["totals"]["busy_ms"], 17)

    def test_reused_node_resolves_origin_run_through_catalog_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
            commit = "f" * 40
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-root", plan=str(plan), plan_sha256=plan_sha,
                base_commit="a" * 40, registration_binding=_registration_binding("graph-root"),
                objective="origin attempt",
                nodes={"CB-01": {"status": "succeeded", "feature_run_id": "graph-root-CB-01", "depends_on": [], "candidate_commit": commit}},
                functionality_tests=(),
            )
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-attempt-1", plan=str(plan), plan_sha256=plan_sha,
                base_commit="a" * 40, registration_binding=_registration_binding("graph-attempt-1"),
                objective="first successor reuses the sealed node",
                nodes={"CB-01": {"status": "succeeded", "feature_run_id": "graph-attempt-1-CB-01", "depends_on": [], "candidate_commit": commit, "reused_from_attempt": "graph-root"}},
                functionality_tests=(),
            )
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-attempt-2", plan=str(plan), plan_sha256=plan_sha,
                base_commit="a" * 40, registration_binding=_registration_binding("graph-attempt-2"),
                objective="second successor reuses across two hops",
                nodes={"CB-01": {"status": "succeeded", "feature_run_id": "graph-attempt-2-CB-01", "depends_on": [], "candidate_commit": commit, "reused_from_attempt": "graph-attempt-1"}},
                functionality_tests=(),
            )
            self._run(root, "graph-root-CB-01")
            (root / "graph-root-CB-01" / "descriptor.json").unlink()
            app = DashboardApplication(root, refresh_seconds=60)
            status, catalog_body, _ = self._request(app, "GET", "/api/catalog")
            detail_status, detail_body, _ = self._request(app, "GET", "/api/feature-runs/graph-root-CB-01")

        self.assertEqual(status, 200)
        graphs = {graph["run_id"]: graph for graph in json.loads(catalog_body)["plan_graphs"]}
        two_hop = graphs["graph-attempt-2"]["nodes"][0]
        self.assertEqual(two_hop["correlation"]["state"], "reused")
        self.assertEqual(two_hop["correlation"]["origin_attempt_id"], "graph-root")
        self.assertEqual(two_hop["correlation"]["origin_feature_run_id"], "graph-root-CB-01")
        self.assertEqual(two_hop["evidence"]["state"], "partial")
        one_hop = graphs["graph-attempt-1"]["nodes"][0]
        self.assertEqual(one_hop["correlation"]["origin_attempt_id"], "graph-root")
        # The origin run's verified detail (and thus its metrics) stays
        # inspectable for a node clicked on the successor attempt.
        self.assertEqual(detail_status, 200)
        self.assertIn("metrics", json.loads(detail_body))

    def test_plan_graph_endpoint_discovers_lineage_bearing_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-attempt-2", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-attempt-2"),
                objective="API lineage discovery", nodes={},
                functionality_tests=(),
            )
            app = DashboardApplication(root, refresh_seconds=60)
            status, catalog_body, _ = self._request(app, "GET", "/api/catalog")
            detail_status, detail_body, _ = self._request(app, "GET", "/api/plan-graphs/graph-attempt-2")

        self.assertEqual(status, 200)
        graph = json.loads(catalog_body)["plan_graphs"][0]
        self.assertEqual(graph["run_id"], "graph-attempt-2")
        self.assertEqual(graph["logical_graph_id"], "graph-attempt-2")
        self.assertEqual(graph["graph_attempt_id"], "graph-attempt-2")
        self.assertIsNone(graph["predecessor_attempt_id"])
        self.assertEqual(graph["retention_constraints"]["state"], "unavailable")
        self.assertEqual(detail_status, 200)
        self.assertEqual(json.loads(detail_body)["run_id"], "graph-attempt-2")

    def test_descriptorless_child_with_matching_run_id_is_id_matched_and_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-1", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-1"),
                objective="id-matched correlation",
                nodes={"CB-01": {"status": "running", "feature_run_id": "graph-1-CB-01", "depends_on": []}},
                functionality_tests=(),
            )
            self._run(root, "graph-1-CB-01")
            (root / "graph-1-CB-01" / "descriptor.json").unlink()
            app = DashboardApplication(root, refresh_seconds=60)
            status, catalog_body, _ = self._request(app, "GET", "/api/catalog")
            detail_status, detail_body, _ = self._request(app, "GET", "/api/feature-runs/graph-1-CB-01")

        self.assertEqual(status, 200)
        catalog = json.loads(catalog_body)
        node = catalog["plan_graphs"][0]["nodes"][0]
        self.assertEqual(node["feature_run_id"], "graph-1-CB-01")
        self.assertEqual(node["evidence"]["state"], "partial")
        self.assertEqual(node["evidence"]["reason"], "correlated by exact run id; descriptor attestation absent")
        run = catalog["feature_runs"][0]
        self.assertEqual(run["kind"], "legacy_feature_run")
        self.assertEqual(run["correlation"]["state"], "id_matched")
        self.assertEqual(run["correlation"]["plan_graph_id"], "graph-1")
        self.assertEqual(run["correlation"]["plan_node_id"], "CB-01")
        self.assertEqual(catalog["ungrouped_feature_runs"], [])
        self.assertEqual(detail_status, 200)
        self.assertIn("metrics", json.loads(detail_body))

    def test_catalog_etag_is_stable_across_refreshes_without_a_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            app = DashboardApplication(root, refresh_seconds=60)
            _, first_catalog, first_options = self._request(app, "GET", "/api/catalog")
            first_etag = first_options["etag"]
            refreshed = json.loads(first_catalog)
            refreshed["generated_at"] = "2026-08-09T00:00:02Z"
            with patch("harness_labs.observability.dashboard_server.RunCatalog.snapshot", return_value=refreshed):
                app._snapshot = app._build_snapshot()
            _, refreshed_catalog, refreshed_options = self._request(app, "GET", "/api/catalog")
            self.assertNotEqual(first_catalog, refreshed_catalog)
            self.assertEqual(refreshed_options["etag"], first_etag)
            transport = type("Transport", (), {"headers": {"If-None-Match": first_etag}, "wfile": io.BytesIO(), "send_response": lambda self, value: setattr(self, "status", value), "send_header": lambda self, key, value: setattr(self, key.lower().replace("-", "_"), value), "end_headers": lambda self: None})()
            _DashboardHandler._send(transport, 200, refreshed_catalog, etag=refreshed_options["etag"])
            self.assertEqual(transport.status, 304)

    def test_catalog_does_not_eagerly_project_feature_run_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            app = DashboardApplication(root, refresh_seconds=60)
            with patch("harness_labs.observability.dashboard_server.build_run_detail", wraps=build_run_detail) as projector:
                self.assertEqual(self._request(app, "GET", "/api/catalog")[0], 200)
                projector.assert_not_called()
                self.assertEqual(self._request(app, "GET", "/api/feature-runs/run-1")[0], 200)
                self.assertEqual(projector.call_count, 1)

    def test_only_get_is_supported_and_paths_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            app = DashboardApplication(root, refresh_seconds=60)
            self.assertEqual(self._request(app, "POST", "/api/catalog")[0], 405)
            self.assertEqual(self._request(app, "GET", "/api/feature-runs/%2e%2e")[0], 404)
            self.assertEqual(self._request(app, "GET", "/events.jsonl")[0], 404)
            self.assertEqual(self._request(app, "GET", "/api/catalog?ignored=true")[0], 404)

    def test_duplicate_ids_are_isolated_and_not_served(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = DashboardApplication(root, refresh_seconds=60)
            duplicate = {"protocol": "harness-run-catalog-snapshot/1", "revision": "x", "generated_at": "2026-08-09T00:00:00Z", "source_root": str(root), "availability": {"state": "available", "reason": None}, "diagnostics": [], "plan_graphs": [], "feature_runs": [{"run_id": "same", "kind": "feature_run", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "none"}, "evidence": {"state": "available", "reason": None}, "correlation": None}, {"run_id": "same", "kind": "feature_run", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "none"}, "evidence": {"state": "available", "reason": None}, "correlation": None}], "ungrouped_feature_runs": []}
            with patch("harness_labs.observability.dashboard_server.RunCatalog.snapshot", return_value=duplicate):
                snapshot = app._build_snapshot()
            self.assertEqual(snapshot.catalog_value["feature_runs"], [])
            self.assertEqual(snapshot.catalog_value["diagnostics"][0]["code"], "ambiguous_run_id")
            self.assertNotIn("same", snapshot.run_details)

    def test_multiple_roots_merge_and_cross_root_children_remain_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_a = Path(directory) / "root-a"
            root_b = Path(directory) / "root-b"
            create_fixture(root_a)
            root_b.mkdir()
            shutil.move(str(root_a / "completed-child"), str(root_b / "completed-child"))

            app = DashboardApplication([root_a, root_b], refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/catalog")
            self.assertEqual(status, 200)
            catalog = json.loads(body)
            self.assertEqual(catalog["source_roots"], [str(root_a.resolve()), str(root_b.resolve())])
            child = next(run for run in catalog["feature_runs"] if run["run_id"] == "completed-child")
            self.assertEqual(child["source_root"], str(root_b.resolve()))
            graph = next(graph for graph in catalog["plan_graphs"] if graph["run_id"] == "completed-graph")
            self.assertEqual(graph["nodes"][0]["evidence"]["state"], "available")
            self.assertEqual(self._request(app, "GET", "/api/feature-runs/completed-child")[0], 200)

    def test_duplicate_ids_across_roots_are_withheld_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_a = Path(directory) / "root-a"
            root_b = Path(directory) / "root-b"
            root_a.mkdir()
            root_b.mkdir()
            self._run(root_a, "same")
            self._run(root_b, "same")

            app = DashboardApplication([root_a, root_b], refresh_seconds=60)
            catalog = json.loads(self._request(app, "GET", "/api/catalog")[1])
            self.assertEqual(catalog["feature_runs"], [])
            diagnostic = next(item for item in catalog["diagnostics"] if item["code"] == "ambiguous_run_id")
            self.assertEqual(diagnostic["run_id"], "same")
            self.assertEqual(self._request(app, "GET", "/api/feature-runs/same")[0], 404)

    def test_root_registry_is_closed_and_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "roots.json"
            registry.write_text(json.dumps({
                "protocol": "harness-dashboard-audit-root-registry/1",
                "audit_roots": ["one/logs/runs", "two/logs/runs"],
            }), encoding="utf-8")
            self.assertEqual(load_audit_root_registry(registry), (
                Path(directory).resolve() / "one/logs/runs",
                Path(directory).resolve() / "two/logs/runs",
            ))
            registry.write_text(json.dumps({
                "protocol": "harness-dashboard-audit-root-registry/1",
                "audit_roots": ["one"],
                "scan_home": True,
            }), encoding="utf-8")
            with self.assertRaisesRegex(DashboardError, "registry is invalid"):
                load_audit_root_registry(registry)

    def test_symlinked_audit_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            link = Path(directory) / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - platforms without symlink support
                self.skipTest(str(exc))
            with self.assertRaises(DashboardError):
                DashboardApplication(link)

    def test_oversized_run_is_isolated_without_hiding_healthy_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "healthy-run")
            oversized = root / "oversized-run"
            oversized.mkdir()
            with (oversized / "events.jsonl").open("wb") as stream:
                stream.truncate(4 * 1024 * 1024 + 1)

            app = DashboardApplication(root, refresh_seconds=60)
            status, catalog_body, _ = self._request(app, "GET", "/api/catalog")

            self.assertEqual(status, 200)
            catalog = json.loads(catalog_body)
            self.assertEqual(catalog["availability"]["state"], "partial")
            self.assertEqual(
                [run["run_id"] for run in catalog["feature_runs"]],
                ["healthy-run", "oversized-run"],
            )
            self.assertEqual(catalog["feature_runs"][1]["status"], "corrupt")
            self.assertEqual(catalog["diagnostics"][0]["run_id"], "oversized-run")

    def test_normal_executor_artifact_volume_remains_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "dense-run")
            artifacts = root / "dense-run" / "artifacts"
            for index in range(140):
                (artifacts / f"transport-{index:03d}.jsonl").touch()
            app = DashboardApplication(root, refresh_seconds=60)
            self.assertEqual(self._request(app, "GET", "/api/feature-runs/dense-run")[0], 200)

    def test_oversized_detail_does_not_make_catalog_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            with patch(
                "harness_labs.observability.dashboard_server.build_run_detail",
                return_value={"blob": "x" * MAX_RESPONSE_BYTES},
            ):
                app = DashboardApplication(root, refresh_seconds=60)
                self.assertEqual(self._request(app, "GET", "/api/catalog")[0], 200)
                self.assertEqual(
                    self._request(app, "GET", "/api/feature-runs/run-1")[0],
                    404,
                )

    def test_dot_prefixed_infrastructure_directories_are_excluded_from_the_run_directory_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "healthy-run")
            # A single real .plan-graph-snapshots directory is the realistic
            # shape; many are created here to exceed MAX_RUN_DIRECTORIES if
            # the dot-dir skip in _validate_audit_tree regresses, making the
            # gap directly observable through catalog availability.
            for index in range(600):
                (root / f".plan-graph-snapshots-fixture-{index}").mkdir()
            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["availability"]["state"], "available")

    def test_pytest_scratch_symlinks_do_not_disqualify_a_run(self) -> None:
        """The verification basetemp is pruned, but real evidence is not."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "healthy-run")
            scratch = root / "healthy-run" / "verification-tmp" / "post_implementation"
            scratch.mkdir(parents=True)
            (scratch / "stage0").mkdir()
            # Exactly the bookkeeping symlink pytest plants beside its
            # numbered basetemp directories.
            (scratch / "stagecurrent").symlink_to(scratch / "stage0")
            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/catalog")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["availability"]["state"], "available")
        self.assertEqual(payload["diagnostics"], [])

    def test_symlinks_outside_the_verification_scratch_still_reject_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "healthy-run")
            (root / "healthy-run" / "artifacts").mkdir(exist_ok=True)
            (root / "healthy-run" / "artifacts" / "escape").symlink_to(root)
            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/catalog")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            [item["message"] for item in payload["diagnostics"]],
            ["run contains a symlink"],
        )


class PlanGraphMetricsEndpointTests(unittest.TestCase):
    """AC-DM04-1: GET /api/plan-graph-metrics/<id>."""

    def _request(self, app: DashboardApplication, method: str, path: str):
        handler = object.__new__(_DashboardHandler)
        handler.app = app
        handler.path = path
        handler.headers = {}
        sent = []
        handler._send = lambda status, body, **kwargs: sent.append((status, body, kwargs))
        getattr(handler, "do_" + method)()
        return sent[0]

    def test_serves_the_rollup_for_live_and_terminal_graphs_and_404s_for_unknown_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            app = DashboardApplication(root, refresh_seconds=60)
            live_status, live_body, _ = self._request(app, "GET", "/api/plan-graph-metrics/active-graph")
            terminal_status, terminal_body, _ = self._request(app, "GET", "/api/plan-graph-metrics/completed-graph")
            missing_status, _, _ = self._request(app, "GET", "/api/plan-graph-metrics/does-not-exist")

        self.assertEqual(live_status, 200)
        live = json.loads(live_body)
        self.assertEqual(live["protocol"], graph_metrics.PROTOCOL)
        self.assertEqual(live["run_id"], "active-graph")
        self.assertIsNotNone(live["timing"]["started_at"])
        # Live graphs never serve a computed wall clock -- the client derives
        # elapsed from started_at -- and the fixture's children never record
        # backend_transport usage, so tokens must degrade, never fabricate 0.
        self.assertEqual(live["timing"]["wall_clock_ms"]["state"], "unavailable")
        self.assertIn("live; elapsed is derived client-side", live["timing"]["wall_clock_ms"]["reason"])
        self.assertEqual(live["totals"]["tokens"]["state"], "unavailable")

        self.assertEqual(terminal_status, 200)
        terminal = json.loads(terminal_body)
        self.assertEqual(terminal["run_id"], "completed-graph")
        self.assertEqual(terminal["counts"]["logical_nodes"], 1)
        # Pins the _graph_own_summary wiring: a terminal graph's own
        # verified summary.json makes a computed wall clock available (never
        # "unavailable", unlike the live case above), which is what
        # parallelism is derived from.
        self.assertEqual(terminal["timing"]["wall_clock_ms"]["state"], "available")
        self.assertIsInstance(terminal["timing"]["wall_clock_ms"]["value"], int)
        self.assertGreaterEqual(terminal["timing"]["wall_clock_ms"]["value"], 0)

        self.assertEqual(missing_status, 404)

    def test_rollup_is_cached_within_one_catalog_revision_and_recomputes_only_after_a_revision_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            app = DashboardApplication(root, refresh_seconds=60)
            with patch(
                "harness_labs.observability.dashboard_server.graph_metrics.compute_graph_metrics",
                wraps=graph_metrics.compute_graph_metrics,
            ) as spy:
                first_status, first_body, _ = self._request(app, "GET", "/api/plan-graph-metrics/completed-graph")
                second_status, second_body, _ = self._request(app, "GET", "/api/plan-graph-metrics/completed-graph")
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(first_body, second_body)
                # A refresh that reproduces the same content-derived revision
                # (nothing on disk changed) must carry the cache forward
                # rather than recompute -- the cache is keyed to the
                # revision, not to the _Snapshot instance.
                same_revision_snapshot = app._build_snapshot()
                self.assertEqual(same_revision_snapshot.revision, app._snapshot.revision)
                app._snapshot = same_revision_snapshot
                same_revision_status, same_revision_body, _ = self._request(app, "GET", "/api/plan-graph-metrics/completed-graph")
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(same_revision_body, first_body)
                # A genuine content change produces a new revision: its own,
                # separate, initially-empty cache.
                changed = dict(app._snapshot.catalog_value)
                changed["diagnostics"] = list(changed["diagnostics"]) + [
                    {"code": "probe", "message": "probe", "run_id": None, "source_root": None}
                ]
                with patch("harness_labs.observability.dashboard_server.RunCatalog.snapshot", return_value=changed):
                    new_revision_snapshot = app._build_snapshot()
                self.assertNotEqual(new_revision_snapshot.revision, app._snapshot.revision)
                app._snapshot = new_revision_snapshot
                third_status, _, _ = self._request(app, "GET", "/api/plan-graph-metrics/completed-graph")
                self.assertEqual(spy.call_count, 2)
        self.assertEqual(
            (first_status, second_status, same_revision_status, third_status),
            (200, 200, 200, 200),
        )

    def test_a_computation_failure_for_one_graph_degrades_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            app = DashboardApplication(root, refresh_seconds=60)
            with patch(
                "harness_labs.observability.dashboard_server.graph_metrics.compute_graph_metrics",
                side_effect=RuntimeError("boom"),
            ):
                status, body, _ = self._request(app, "GET", "/api/plan-graph-metrics/completed-graph")
                # A sibling graph's own metrics and the catalog itself are
                # entirely unaffected by the failure above.
                catalog_status, _, _ = self._request(app, "GET", "/api/catalog")

        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["run_id"], "completed-graph")
        self.assertEqual(document["error"]["state"], "unavailable")
        self.assertIn("boom", document["error"]["reason"])
        self.assertEqual(catalog_status, 200)


class SnapshotsEndpointTests(unittest.TestCase):
    """AC-DM04-2: GET /api/snapshots and GET /api/snapshots/<id>."""

    def _request(self, app: DashboardApplication, method: str, path: str):
        handler = object.__new__(_DashboardHandler)
        handler.app = app
        handler.path = path
        handler.headers = {}
        sent = []
        handler._send = lambda status, body, **kwargs: sent.append((status, body, kwargs))
        getattr(handler, "do_" + method)()
        return sent[0]

    def test_listing_flags_missing_snapshots_and_reflects_a_snapshot_written_after_the_first_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            app = DashboardApplication(root, refresh_seconds=60)
            status, body, options = self._request(app, "GET", "/api/snapshots")
            self.assertEqual(status, 200)
            listing = json.loads(body)
            self.assertEqual(listing["bounds"], {"max_snapshot_files_per_root": MAX_SNAPSHOT_FILES, "max_file_bytes": MAX_FILE_BYTES})
            entries = {entry["run_id"]: entry for entry in listing["snapshots"]}
            # completed-graph is terminal with no snapshot file on disk yet;
            # the live active-graph is never flagged (only terminal graphs are).
            self.assertTrue(entries["completed-graph"]["snapshot_missing"])
            self.assertNotIn("active-graph", entries)
            first_etag = options["etag"]

            document = build_snapshot(root, "completed-graph")
            write_snapshot(root, document)
            status2, body2, options2 = self._request(app, "GET", "/api/snapshots")

        self.assertEqual(status2, 200)
        self.assertNotEqual(options2["etag"], first_etag)
        listing2 = json.loads(body2)
        entries2 = {entry["run_id"]: entry for entry in listing2["snapshots"]}
        self.assertFalse(entries2["completed-graph"]["snapshot_missing"])
        self.assertEqual(entries2["completed-graph"]["completeness"], document["data_quality"]["completeness"])
        self.assertEqual(entries2["completed-graph"]["display_name"], document["display_name"])

    def test_oversize_symlinked_and_malformed_snapshot_files_degrade_to_diagnostics_not_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            document = build_snapshot(root, "completed-graph")
            write_snapshot(root, document)  # one healthy entry
            snapshots_dir = root / SNAPSHOT_DIRNAME

            with (snapshots_dir / "oversize-graph.json").open("wb") as stream:
                stream.truncate(MAX_FILE_BYTES + 1)
            (snapshots_dir / "malformed-graph.json").write_text("not json", encoding="utf-8")
            target = snapshots_dir / "unlinked-target.json"
            target.write_text(json.dumps({"protocol": "plangraph-metrics-snapshot/1"}), encoding="utf-8")
            symlinked = snapshots_dir / "symlinked-graph.json"
            try:
                symlinked.symlink_to(target)
            except OSError as exc:
                self.skipTest(str(exc))

            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/snapshots")

        self.assertEqual(status, 200)
        listing = json.loads(body)
        healthy_ids = {entry["run_id"] for entry in listing["snapshots"] if not entry["snapshot_missing"]}
        self.assertIn("completed-graph", healthy_ids)
        codes = {diagnostic["code"] for diagnostic in listing["diagnostics"]}
        self.assertIn("snapshot_malformed", codes)
        self.assertIn("snapshot_symlink_rejected", codes)

    def test_snapshot_document_endpoint_serves_and_rejects_symlinks_and_unknown_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            document = build_snapshot(root, "completed-graph")
            write_snapshot(root, document)
            app = DashboardApplication(root, refresh_seconds=60)

            status, body, _ = self._request(app, "GET", "/api/snapshots/completed-graph")
            missing_status, _, _ = self._request(app, "GET", "/api/snapshots/does-not-exist")

            target = root / SNAPSHOT_DIRNAME / "unlinked-target.json"
            target.write_text(json.dumps({"protocol": "plangraph-metrics-snapshot/1"}), encoding="utf-8")
            symlinked = root / SNAPSHOT_DIRNAME / "linked-graph.json"
            try:
                symlinked.symlink_to(target)
            except OSError as exc:
                self.skipTest(str(exc))
            symlink_status, _, _ = self._request(app, "GET", "/api/snapshots/linked-graph")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["identity"]["run_id"], "completed-graph")
        self.assertEqual(missing_status, 404)
        self.assertEqual(symlink_status, 404)

    def test_snapshot_document_endpoint_rejects_a_symlinked_snapshots_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            real_dir = Path(directory) / "real-snapshots"
            real_dir.mkdir()
            document = build_snapshot(root, "completed-graph")
            write_snapshot(root, document, output_dir=real_dir)
            linked_dir = root / SNAPSHOT_DIRNAME
            try:
                linked_dir.symlink_to(real_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))

            app = DashboardApplication(root, refresh_seconds=60)
            status, _, _ = self._request(app, "GET", "/api/snapshots/completed-graph")

        self.assertEqual(status, 404)

    def test_invalid_utf8_snapshot_file_degrades_to_a_diagnostic_not_a_handler_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            document = build_snapshot(root, "completed-graph")
            write_snapshot(root, document)  # one healthy entry
            snapshots_dir = root / SNAPSHOT_DIRNAME
            (snapshots_dir / "invalid-utf8-graph.json").write_bytes(b'{"protocol": "plangraph-metrics-snapshot/1", "b\xff\xfe": true}')

            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/snapshots")
            document_status, _, _ = self._request(app, "GET", "/api/snapshots/invalid-utf8-graph")

        self.assertEqual(status, 200)
        listing = json.loads(body)
        healthy_ids = {entry["run_id"] for entry in listing["snapshots"] if not entry["snapshot_missing"]}
        self.assertIn("completed-graph", healthy_ids)
        codes = {diagnostic["code"] for diagnostic in listing["diagnostics"]}
        self.assertIn("snapshot_malformed", codes)
        self.assertEqual(document_status, 404)

    def test_listing_sorts_safely_when_finished_at_types_are_heterogeneous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fixture(root)
            snapshots_dir = root / SNAPSHOT_DIRNAME
            snapshots_dir.mkdir(exist_ok=True)

            def _write(run_id: str, finished_at: Any) -> None:
                (snapshots_dir / f"{run_id}.json").write_text(json.dumps({
                    "protocol": "plangraph-metrics-snapshot/1",
                    "identity": {"run_id": run_id, "logical_graph_id": run_id, "graph_attempt_id": run_id},
                    "display_name": run_id,
                    "status": "succeeded",
                    "timing": {"finished_at": finished_at},
                    "graph_metrics": {"totals": {}},
                    "data_quality": {},
                }), encoding="utf-8")

            _write("string-finished", "2026-08-09T00:00:00Z")
            _write("numeric-finished", 12345)

            app = DashboardApplication(root, refresh_seconds=60)
            status, body, _ = self._request(app, "GET", "/api/snapshots")

        self.assertEqual(status, 200)
        listing = json.loads(body)
        entries = {entry["run_id"]: entry for entry in listing["snapshots"]}
        self.assertEqual(entries["string-finished"]["finished_at"], "2026-08-09T00:00:00Z")
        # A non-string finished_at degrades to unavailable instead of
        # raising a TypeError when sorted alongside the well-formed string
        # entry above.
        self.assertIsNone(entries["numeric-finished"]["finished_at"])


class RunDashboardDefaultRegistryTests(unittest.TestCase):
    """AC-DM04-3: scripts/run_dashboard.py default-registry fallback."""

    def test_falls_back_to_the_default_registry_only_when_neither_flag_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "dashboard-audit-roots.json"
            root_a = Path(directory) / "root-a"
            root_a.mkdir()
            registry_path.write_text(json.dumps({
                "protocol": "harness-dashboard-audit-root-registry/1",
                "audit_roots": [str(root_a)],
            }), encoding="utf-8")

            with patch.dict(os.environ, {"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(registry_path)}):
                self.assertEqual(_resolve_audit_roots([], None), [root_a])
                root_b = Path(directory) / "root-b"
                root_b.mkdir()
                # An explicit --audit-root suppresses the default-registry
                # fallback entirely, even though no --audit-root-registry
                # was given.
                self.assertEqual(_resolve_audit_roots([root_b], None), [root_b])

            with patch.dict(os.environ, {"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": ""}):
                with patch("scripts.run_dashboard._DEFAULT_AUDIT_ROOT_REGISTRY", Path(directory) / "does-not-exist.json"):
                    self.assertEqual(_resolve_audit_roots([], None), [])

    def test_explicit_audit_root_registry_is_never_overridden_by_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            explicit_registry = Path(directory) / "explicit.json"
            root_a = Path(directory) / "root-a"
            root_a.mkdir()
            explicit_registry.write_text(json.dumps({
                "protocol": "harness-dashboard-audit-root-registry/1",
                "audit_roots": [str(root_a)],
            }), encoding="utf-8")
            default_registry = Path(directory) / "default.json"
            root_b = Path(directory) / "root-b"
            root_b.mkdir()
            default_registry.write_text(json.dumps({
                "protocol": "harness-dashboard-audit-root-registry/1",
                "audit_roots": [str(root_b)],
            }), encoding="utf-8")
            with patch("scripts.run_dashboard._DEFAULT_AUDIT_ROOT_REGISTRY", default_registry):
                self.assertEqual(_resolve_audit_roots([], explicit_registry), [root_a])

    def test_a_broken_default_registry_produces_a_clean_error_not_an_unhandled_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "dashboard-audit-roots.json"
            registry_path.write_text("not json", encoding="utf-8")
            with patch.dict(os.environ, {"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(registry_path)}):
                with patch("sys.argv", ["run_dashboard.py"]):
                    with self.assertRaises(SystemExit) as ctx:
                        main()
        self.assertEqual(ctx.exception.code, 2)

    def test_an_unreadable_default_registry_produces_a_clean_error_not_an_unhandled_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "dashboard-audit-roots.json"
            registry_path.write_text(json.dumps({
                "protocol": "harness-dashboard-audit-root-registry/1",
                "audit_roots": [str(Path(directory) / "root-a")],
            }), encoding="utf-8")
            registry_path.chmod(0o000)
            try:
                with patch.dict(os.environ, {"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(registry_path)}):
                    with patch("sys.argv", ["run_dashboard.py"]):
                        with self.assertRaises(SystemExit) as ctx:
                            main()
                self.assertEqual(ctx.exception.code, 2)
            finally:
                registry_path.chmod(0o644)


class DashboardHealthTests(unittest.TestCase):
    """Health must answer "is the served data still being updated?".

    Reporting that a snapshot exists is not the same question: a snapshot is
    only ever replaced by a *successful* refresh, so once refresh starts
    failing the dashboard serves frozen data indefinitely. That is exactly
    what happened when /api/catalog outgrew the 1 MiB response cap -- the
    dashboard served a stale snapshot for ~10.5 hours while health reported
    "ok" the entire time.
    """

    _run = DashboardApiTests._run
    _request = DashboardApiTests._request

    def _age_snapshot(self, app: DashboardApplication, seconds: float) -> None:
        current = app._snapshot
        assert current is not None
        app._snapshot = replace(current, created_at=current.created_at - seconds)

    def test_degraded_while_refresh_fails_and_a_stale_snapshot_is_served(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            # The scheduler is stubbed out so the assertions observe exactly
            # the refreshes this test drives, not a background thread.
            with patch.object(DashboardApplication, "_schedule_refresh", lambda self: None):
                app = DashboardApplication(root, refresh_seconds=60)
                self.assertEqual(json.loads(app.health())["status"], "ok")
                self._age_snapshot(app, 3600)
                with patch.object(
                    DashboardApplication,
                    "_build_snapshot",
                    side_effect=DashboardError("response exceeds size limit"),
                ):
                    app.refresh()
                    app.refresh()

                    report = json.loads(app.health())
                    self.assertEqual(report["status"], "degraded")
                    self.assertEqual(report["consecutive_refresh_failures"], 2)
                    self.assertIn("response exceeds size limit", report["refresh_error"])
                    self.assertIn("3600", report["reason"].replace(".0s", ""))
                    self.assertGreaterEqual(report["snapshot_age_seconds"], 3600)

                    # Frozen *but serving*: the catalog still answers 200 with
                    # the stale body, which is why health is the only place
                    # the freeze can surface.
                    self.assertEqual(self._request(app, "GET", "/api/catalog")[0], 200)
                    self.assertEqual(self._request(app, "GET", "/api/health")[0], 503)

    def test_recovers_to_ok_once_a_refresh_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            with patch.object(DashboardApplication, "_schedule_refresh", lambda self: None):
                app = DashboardApplication(root, refresh_seconds=60)
                self.assertEqual(json.loads(app.health())["status"], "ok")
                self._age_snapshot(app, 3600)
                with patch.object(
                    DashboardApplication,
                    "_build_snapshot",
                    side_effect=DashboardError("response exceeds size limit"),
                ):
                    app.refresh()
                self.assertEqual(json.loads(app.health())["status"], "degraded")
                app.refresh()
                report = json.loads(app.health())
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["consecutive_refresh_failures"], 0)
                self.assertNotIn("refresh_error", report)
                self.assertEqual(self._request(app, "GET", "/api/health")[0], 200)

    def test_degraded_on_age_alone_when_no_refresh_is_recorded(self) -> None:
        """The secondary net: a refresh that stops running without raising.

        No error is recorded here, so the failure counter says nothing; only
        the snapshot's age reveals that nothing is updating it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            with patch.object(DashboardApplication, "_schedule_refresh", lambda self: None):
                app = DashboardApplication(root, refresh_seconds=2.0)
                self.assertEqual(json.loads(app.health())["status"], "ok")
                self._age_snapshot(app, app.stale_after_seconds + 1)
                report = json.loads(app.health())
                self.assertEqual(report["status"], "degraded")
                self.assertEqual(report["consecutive_refresh_failures"], 0)
                self.assertNotIn("refresh_error", report)
                self.assertIn("no refresh recorded", report["reason"])
                self.assertEqual(self._request(app, "GET", "/api/health")[0], 503)

    def test_stale_threshold_is_generous_relative_to_the_refresh_interval(self) -> None:
        """An idle dashboard holds an old snapshot legitimately; the age net
        must not fire on ordinary lazy refresh."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            with patch.object(DashboardApplication, "_schedule_refresh", lambda self: None):
                app = DashboardApplication(root, refresh_seconds=2.0)
                self.assertEqual(
                    app.stale_after_seconds,
                    max(2.0 * STALE_SNAPSHOT_REFRESH_MULTIPLIER, MIN_STALE_SNAPSHOT_SECONDS),
                )
                self.assertEqual(json.loads(app.health())["status"], "ok")
                self._age_snapshot(app, app.refresh_seconds * 3)
                self.assertEqual(json.loads(app.health())["status"], "ok")
                # A sub-second refresh interval must not make every snapshot
                # look frozen a second later.
                brisk = DashboardApplication(root, refresh_seconds=0.01)
                self.assertEqual(brisk.stale_after_seconds, MIN_STALE_SNAPSHOT_SECONDS)

    def test_unavailable_when_no_snapshot_was_ever_built(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root)
            with patch.object(
                DashboardApplication,
                "_build_snapshot",
                side_effect=DashboardError("response exceeds size limit"),
            ):
                app = DashboardApplication(root, refresh_seconds=60)
                report = json.loads(app.health())
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(self._request(app, "GET", "/api/health")[0], 503)


if __name__ == "__main__":
    unittest.main()
