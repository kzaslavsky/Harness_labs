from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.dashboard_server import (
    DashboardApplication,
    DashboardError,
    _DashboardHandler,
    _apply_cumulative_node_metrics,
    load_audit_root_registry,
)
from harness_labs.plan_graph_audit import PlanGraphAudit
from scripts.dashboard_fixture_run import create_fixture


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
            {"run_id": "graph-2", "created_at": "2026-08-09T01:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-2"}]},
            {"run_id": "graph-3", "created_at": "2026-08-09T02:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-3"}]},
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

    def test_cumulative_reused_try_adds_no_spend_and_wall_busy_are_all_or_unavailable(self) -> None:
        def metrics(tokens: int, wall: int | None, busy: int | None) -> dict:
            totals = {"calls": 1, "input_tokens": tokens - 5, "cached_input_tokens": 0, "output_tokens": 5, "total_tokens": tokens, "duration_ms": 30, "wall_clock_ms": wall, "busy_ms": busy, "peak_input_tokens": tokens - 5, "cost": {"state": "estimated", "usd": 0.1, "reason": "estimate", "sources": ["pricing"], "estimated_records": 1, "long_context_records": 0}}
            return {"protocol": "harness-run-detail-metrics/1", "totals": totals, "quality": {}, "by_phase": [], "by_agent": [], "by_agent_type": [], "by_model": [], "by_effort": [], "by_backend": [], "stages": [], "provenance": {"usage_records": 1, "collection_method": "verified", "peak_context_definition": "peak"}}

        catalog = {"plan_graphs": [
            {"run_id": "graph-1", "created_at": "2026-08-09T00:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-1"}]},
            {"run_id": "graph-2", "created_at": "2026-08-09T01:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "try-2"}]},
            # The newest attempt REUSES the sealed node: its planned run
            # directory never existed, so a reuse contributes no new spend.
            {"run_id": "graph-3", "created_at": "2026-08-09T02:00:00Z", "plan_digest": "plan", "nodes": [{"node_id": "FR-1", "feature_run_id": "graph-3-FR-1", "reused_from_attempt": "graph-2"}]},
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
            with patch("harness_labs.dashboard_server.RunCatalog.snapshot", return_value=refreshed):
                app._snapshot = app._build_snapshot()
            _, refreshed_catalog, refreshed_options = self._request(app, "GET", "/api/catalog")
            self.assertNotEqual(first_catalog, refreshed_catalog)
            self.assertEqual(refreshed_options["etag"], first_etag)
            transport = type("Transport", (), {"headers": {"If-None-Match": first_etag}, "wfile": io.BytesIO(), "send_response": lambda self, value: setattr(self, "status", value), "send_header": lambda self, key, value: setattr(self, key.lower().replace("-", "_"), value), "end_headers": lambda self: None})()
            _DashboardHandler._send(transport, 200, refreshed_catalog, etag=refreshed_options["etag"])
            self.assertEqual(transport.status, 304)

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
            with patch("harness_labs.dashboard_server.RunCatalog.snapshot", return_value=duplicate):
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
                "harness_labs.dashboard_server.build_run_detail",
                return_value={"blob": "x" * (1024 * 1024)},
            ):
                app = DashboardApplication(root, refresh_seconds=60)
                self.assertEqual(self._request(app, "GET", "/api/catalog")[0], 200)
                self.assertEqual(
                    self._request(app, "GET", "/api/feature-runs/run-1")[0],
                    404,
                )


if __name__ == "__main__":
    unittest.main()
