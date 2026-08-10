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
    load_audit_root_registry,
)
from scripts.dashboard_fixture_run import create_fixture


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
