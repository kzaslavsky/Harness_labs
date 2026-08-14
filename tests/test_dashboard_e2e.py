"""End-to-end certification of the read-only dashboard discovery walk."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from harness_labs.observability.dashboard_server import DashboardApplication, _DashboardHandler, create_dashboard_server
from harness_labs.observability.plangraph_snapshot import build_snapshot, write_snapshot
from harness_labs.observability.run_catalog import RunCatalog
from scripts.dashboard_fixture_run import _feature, _graph, _lease, advance_live_fixture, create_fixture


def _get(app: DashboardApplication, path: str) -> bytes:
    """Walk the production request handler without requiring a network socket."""
    handler = object.__new__(_DashboardHandler)
    handler.app = app
    handler.path = path
    handler.headers = {}
    sent: list[tuple[int, bytes]] = []
    handler._send = lambda status, body, **kwargs: sent.append((int(status), body))
    handler.do_GET()
    status, body = sent[0]
    if status != 200:
        raise AssertionError(f"GET {path} returned {status}")
    return body


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _chrome_binary() -> str | None:
    """Return an explicitly configured or conventional local Chrome binary."""
    configured = os.environ.get("DASHBOARD_E2E_CHROME")
    candidates = [configured] if configured else []
    candidates.extend((
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ))
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


class _ChromePage:
    """Small, dependency-free CDP client for this browser certification only."""

    def __init__(self, chrome: str) -> None:
        self._profile = tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._port = probe.getsockname()[1]
        self._process = subprocess.Popen(
            [chrome, "--headless=new", f"--remote-debugging-port={self._port}",
             f"--user-data-dir={self._profile.name}", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/list", timeout=1).read())
                page = next(target for target in targets if target.get("type") == "page")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.close()
                    raise AssertionError("Chrome DevTools did not start")
                time.sleep(0.1)
        self._socket = self._connect(page["webSocketDebuggerUrl"])
        self._message_id = 0

    @staticmethod
    def _connect(url: str) -> socket.socket:
        from base64 import b64encode
        from hashlib import sha1
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        connection = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        key = b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        connection.sendall(request)
        response = connection.recv(4096).decode("iso-8859-1")
        expected = b64encode(sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if " 101 " not in response or f"Sec-WebSocket-Accept: {expected}" not in response:
            connection.close()
            raise AssertionError("Chrome DevTools rejected WebSocket connection")
        return connection

    def _send(self, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        mask = os.urandom(4)
        length = len(raw)
        header = bytes([0x81, 0x80 | length]) if length < 126 else bytes([0x81, 0x80 | 126]) + length.to_bytes(2, "big")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(raw))
        self._socket.sendall(header + mask + masked)

    def _receive(self) -> dict[str, object]:
        header = self._socket.recv(2)
        if len(header) != 2:
            raise AssertionError("Chrome DevTools closed the WebSocket")
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._socket.recv(2), "big")
        elif length == 127:
            length = int.from_bytes(self._socket.recv(8), "big")
        payload = bytearray()
        while len(payload) < length:
            payload.extend(self._socket.recv(length - len(payload)))
        return json.loads(payload)

    def command(self, method: str, **params: object) -> dict[str, object]:
        self._message_id += 1
        request_id = self._message_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            response = self._receive()
            if response.get("id") == request_id:
                return response

    def evaluate(self, expression: str) -> object:
        response = self.command("Runtime.evaluate", expression=expression, returnByValue=True)
        result = response.get("result", {}).get("result", {})
        if "exceptionDetails" in response.get("result", {}):
            raise AssertionError(f"browser evaluation failed: {response}")
        return result.get("value")

    def wait_for(self, expression: str, *, timeout: float = 10) -> object:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"browser condition timed out: {expression}")

    def close(self) -> None:
        if hasattr(self, "_socket"):
            self._socket.close()
        if hasattr(self, "_process"):
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._profile.cleanup()


class DashboardEndToEndTests(unittest.TestCase):
    def test_discovery_inspection_polling_and_legacy_import_are_certified(self) -> None:
        chrome = _chrome_binary()
        if chrome is None:
            self.skipTest("set DASHBOARD_E2E_CHROME to a Chrome binary to run UI certification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "audit-root"
            second_root = Path(temporary) / "second-audit-root"
            create_fixture(root)
            second_root.mkdir()
            shutil.move(str(root / "completed-child"), str(second_root / "completed-child"))
            before = (_tree_digest(root), _tree_digest(second_root))
            assets = Path("dashboard/plan-graph/dist").resolve()
            with patch("harness_labs.observability.dashboard_server.RunCatalog", side_effect=lambda source, **options: RunCatalog(source, process_probe=lambda pid: "fixture-process-token", **options)):
                app = DashboardApplication([root, second_root], assets_root=assets, refresh_seconds=0.001)
                server = create_dashboard_server(app, port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                page = _ChromePage(chrome)
                host, port = server.server_address[:2]
                self.addCleanup(server.server_close)
                self.addCleanup(lambda: thread.join(timeout=5))
                self.addCleanup(server.shutdown)
                self.addCleanup(page.close)
                page.command("Page.navigate", url=f"http://{host}:{port}/")
                page.wait_for("document.querySelectorAll('.runs button').length > 0")
                catalog_bytes = _get(app, "/api/catalog")
                catalog = json.loads(catalog_bytes)
                self.assertEqual(len(catalog["source_roots"]), 2)
                records = {item["run_id"]: item for item in catalog["feature_runs"]}
                self.assertEqual(records["completed-child"]["liveness"]["state"], "terminal")
                self.assertEqual(records["live-child"]["liveness"]["state"], "live")
                self.assertEqual(records["stale-child"]["liveness"]["state"], "stale")
                self.assertEqual(records["legacy-child"]["kind"], "legacy_feature_run")
                self.assertIn("legacy-child", [item["run_id"] for item in catalog["ungrouped_feature_runs"]])
                self.assertIn("malformed-run", [item["run_id"] for item in catalog["diagnostics"]])
                graph = next(item for item in catalog["plan_graphs"] if item["run_id"] == "active-graph")
                self.assertEqual({node["feature_run_id"] for node in graph["nodes"]}, {"live-child", "stale-child", "planned-child"})
                detail = json.loads(_get(app, "/api/feature-runs/live-child"))
                for family in ("lifecycle", "criteria", "tasks", "findings", "evidence_metadata", "git_custody", "usage"):
                    self.assertIn(family, detail["availability"])
                # A planned node without a verified child still has inspectable
                # graph detail and an explicit metrics-availability explanation.
                page.evaluate("[...document.querySelectorAll('.react-flow__node')].find((node) => node.innerText.includes('planned')).click()")
                page.wait_for("document.querySelector('aside[aria-label=\\\"active-graph:planned PlanGraph node details\\\"]') !== null")
                page.wait_for("document.querySelector('.inspector').innerText.includes('Verified FeatureRun metrics are unavailable')")
                # A correlated node opens its verified run directly on metrics.
                page.evaluate("[...document.querySelectorAll('.react-flow__node')].find((node) => node.querySelector('strong')?.innerText === 'live').click()")
                page.wait_for("document.querySelector('aside[aria-label=\\\"live-child FeatureRun details\\\"]') !== null")
                page.wait_for("document.querySelector('.inspector').innerText.includes('Dashboard fixture live-child')")
                self.assertEqual(
                    page.evaluate("[...document.querySelectorAll('.detail-tabs button')].map((button) => button.innerText)"),
                    ["Overview", "Activity", "Metrics", "Evidence", "Git Custody"],
                )
                page.wait_for("document.querySelector('.inspector').innerText.includes('Total tokens')")
                self.assertEqual(page.evaluate("document.querySelectorAll('.inspector pre').length"), 0)
                self.assertEqual(before, (_tree_digest(root), _tree_digest(second_root)), "dashboard reads must not mutate audit roots")
                advance_live_fixture(root)
                # The two-second UI polling interval must retain the selection and
                # show the terminal state after the catalog refresh.
                page.wait_for("document.querySelector('aside[aria-label=\\\"live-child FeatureRun details\\\"]') && document.querySelector('.inspector').innerText.includes('Succeeded')", timeout=6)

    def test_in_flight_strip_and_graph_totals_are_certified(self) -> None:
        """AC-DM05-1, AC-DM05-2: the in-flight strip lists every live PlanGraph
        and switches selection without a reload, and the GraphTotals panel
        renders DM-01 headline metrics with explicit tri-state labelling."""
        chrome = _chrome_binary()
        if chrome is None:
            self.skipTest("set DASHBOARD_E2E_CHROME to a Chrome binary to run UI certification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_fixture(root)
            # A second, independent live PlanGraph (distinct topology from
            # "active-graph") so the strip has more than one entry to switch
            # between -- this is what AC-DM05-1 actually certifies.
            second_parent = {"plan_graph_id": "second-active-graph", "plan_node_id": "solo", "parent_run_id": "second-active-graph"}
            second_live = _feature(root, "second-live-child", parent=second_parent)
            _lease(second_live, "second-live-child", stale=False)
            _graph(root, "second-active-graph", root / "approved-plan.md", {"solo": {"status": "running", "feature_run_id": "second-live-child"}}, terminal=False)
            assets = Path("dashboard/plan-graph/dist").resolve()
            with patch("harness_labs.observability.dashboard_server.RunCatalog", side_effect=lambda source, **options: RunCatalog(source, process_probe=lambda pid: "fixture-process-token", **options)):
                app = DashboardApplication(root, assets_root=assets, refresh_seconds=0.001)
                server = create_dashboard_server(app, port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                page = _ChromePage(chrome)
                host, port = server.server_address[:2]
                self.addCleanup(server.server_close)
                self.addCleanup(lambda: thread.join(timeout=5))
                self.addCleanup(server.shutdown)
                self.addCleanup(page.close)
                page.command("Page.navigate", url=f"http://{host}:{port}/")
                page.wait_for("document.querySelectorAll('.in-flight-list button').length === 2")
                names = page.evaluate("[...document.querySelectorAll('.in-flight-list button strong')].map((node) => node.innerText)")
                self.assertEqual(len(set(names)), 2, "in-flight display names must be unique")
                # Every strip entry reports a non-empty elapsed label derived
                # client-side from started_at, never a raw zero/blank.
                elapsed_labels = page.evaluate("[...document.querySelectorAll('.in-flight-list button span')].map((node) => node.innerText)")
                self.assertTrue(all(label for label in elapsed_labels))
                # The GraphTotals panel renders for the initially selected
                # live graph with explicit tri-state labelling (this fixture
                # records no backend usage, so tokens must say so, not "0").
                page.wait_for("document.querySelector('.graph-totals') !== null")
                page.wait_for("document.querySelector('.graph-totals').innerText.includes('Total tokens')")
                self.assertIn("Unavailable", page.evaluate("document.querySelector('.graph-totals').innerText"))
                # active-graph (3 nodes) and second-active-graph (1 node) have
                # different topologies, so the node count is a reliable,
                # order-independent signal that a strip click actually
                # switched the canvas's selection in place -- no navigation,
                # no full reload.
                initial_count = page.evaluate("document.querySelectorAll('.react-flow__node').length")
                self.assertIn(initial_count, (1, 3))
                active_index = page.evaluate("[...document.querySelectorAll('.in-flight-list button')].findIndex((button) => button.classList.contains('active'))")
                other_index = 1 - active_index
                other_name = names[other_index]
                page.evaluate(f"document.querySelectorAll('.in-flight-list button')[{other_index}].click()")
                page.wait_for(f"document.querySelectorAll('.react-flow__node').length === {4 - initial_count}")
                active_name = page.evaluate("document.querySelector('.in-flight-list button.active strong').innerText")
                self.assertEqual(active_name, other_name)
                page.wait_for("document.querySelector('.graph-totals').innerText.includes('Logical nodes')")
                # A single-try node inspector reports non-cumulative retry
                # labelling ("Retries", not "Cumulative retries") -- the
                # cumulative merge itself is certified by test_graph_metrics.py
                # and test_dashboard_api.py; this pins only the UI wiring.
                page.evaluate("[...document.querySelectorAll('.react-flow__node')].find((node) => !node.innerText.includes('correlation unavailable')).click()")
                page.wait_for("document.querySelector('.inspector').innerText.includes('Retries')")
                self.assertNotIn("Cumulative retries", page.evaluate("document.querySelector('.inspector').innerText"))

    def test_completed_viewer_and_comparison_table_are_certified(self) -> None:
        """AC-DM06-1, AC-DM06-2: the completed viewer lists every snapshot
        and snapshot_missing stub, renders a selected snapshot's graph
        totals / per-node metrics / outcome summary through the same
        components as the live view, and the comparison table groups by
        logical graph with an expandable per-attempt toggle, a sortable
        metric-complete filter with hidden-row count, and per-column sort
        indicators defaulting to finished_at descending."""
        chrome = _chrome_binary()
        if chrome is None:
            self.skipTest("set DASHBOARD_E2E_CHROME to a Chrome binary to run UI certification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_fixture(root)
            document = build_snapshot(root, "completed-graph")
            write_snapshot(root, document)
            # A second terminal graph with no snapshot file on disk: the
            # listing must flag it `snapshot_missing` (AC-DM06-1) instead of
            # silently omitting it, and the comparison table must still be
            # able to represent it (as an entirely degraded row).
            second_parent = {"plan_graph_id": "second-completed-graph", "plan_node_id": "done", "parent_run_id": "second-completed-graph"}
            _feature(root, "second-completed-child", parent=second_parent, terminal=True)
            _graph(root, "second-completed-graph", root / "approved-plan.md", {"done": {"status": "queued", "feature_run_id": "second-completed-child"}}, terminal=True)
            assets = Path("dashboard/plan-graph/dist").resolve()
            with patch("harness_labs.observability.dashboard_server.RunCatalog", side_effect=lambda source, **options: RunCatalog(source, process_probe=lambda pid: "fixture-process-token", **options)):
                app = DashboardApplication(root, assets_root=assets, refresh_seconds=0.001)
                server = create_dashboard_server(app, port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                page = _ChromePage(chrome)
                host, port = server.server_address[:2]
                self.addCleanup(server.server_close)
                self.addCleanup(lambda: thread.join(timeout=5))
                self.addCleanup(server.shutdown)
                self.addCleanup(page.close)
                page.command("Page.navigate", url=f"http://{host}:{port}/")
                page.wait_for("document.querySelectorAll('.runs button').length > 0")
                # The Live/Completed toggle switches views without a reload.
                page.evaluate("document.querySelector('.view-toggle button:nth-child(2)').click()")
                page.wait_for("document.querySelectorAll('.snapshot-list button').length === 2")
                names = page.evaluate("[...document.querySelectorAll('.snapshot-list button strong')].map((node) => node.innerText)")
                self.assertEqual(len(names), 2)
                missing_marker = page.evaluate("[...document.querySelectorAll('.snapshot-list button')].some((button) => button.classList.contains('snapshot-missing') && button.innerText.includes('Snapshot missing'))")
                self.assertTrue(missing_marker, "a terminal graph with no snapshot file must be flagged snapshot_missing")

                # The left rail itself carries the outcome narrative for each
                # populated entry (plan:332-334), not only the detail pane
                # for whichever snapshot happens to be selected.
                page.wait_for(
                    "[...document.querySelectorAll('.snapshot-list button')]"
                    ".find((button) => !button.classList.contains('snapshot-missing'))"
                    ".innerText.includes('" + document["outcome"]["narrative"][:30].replace("'", "\\'") + "')"
                )

                # Selecting the populated snapshot renders GraphTotals and
                # NodeMetricsTable -- the exact same standalone components
                # (`.graph-totals`, `.metric-section`) the live view uses --
                # plus an outcome summary, all sourced from the snapshot
                # document alone (AC-DM06-1).
                page.evaluate("[...document.querySelectorAll('.snapshot-list button')].find((button) => !button.classList.contains('snapshot-missing')).click()")
                page.wait_for("document.querySelector('.completed-detail-header h2') !== null")
                self.assertEqual(page.evaluate("document.querySelector('.completed-detail-header h2').innerText"), document["display_name"])
                page.wait_for("document.querySelector('.graph-totals') !== null")
                page.wait_for("document.querySelector('.graph-totals').innerText.includes('Total tokens')")
                page.wait_for("document.querySelector('.metric-section') !== null")
                page.wait_for("document.querySelector('.outcome-summary') !== null")
                self.assertIn(document["outcome"]["narrative"], page.evaluate("document.querySelector('.outcome-summary').innerText"))

                # Selecting the snapshot_missing stub explains the gap
                # instead of rendering an empty or broken detail pane.
                page.evaluate("[...document.querySelectorAll('.snapshot-list button')].find((button) => button.classList.contains('snapshot-missing')).click()")
                page.wait_for("document.querySelector('.completed-browse').innerText.includes('No metrics snapshot has been written')")

                # Compare mode: the default-on metrics-complete filter hides
                # both entries here (the fixture records no token usage, so
                # `completed-graph`'s own completeness grade is "partial",
                # and the stub has none at all) and reports the hidden
                # count -- the table must say so rather than show 0 rows
                # silently (AC-DM06-2).
                page.evaluate("[...document.querySelectorAll('.completed-toolbar button')].find((button) => button.innerText === 'Compare').click()")
                page.wait_for("document.querySelector('.comparison-table') !== null")
                page.wait_for("document.querySelector('.hidden-count').innerText.includes('2 of 2 snapshot')")
                self.assertEqual(page.evaluate("document.querySelectorAll('.comparison-group-row').length"), 0)

                # Turning the filter off reveals both logical graphs as two
                # distinct grouped rows (they have different topologies /
                # logical_graph_id, so no merge is expected), and the sort
                # indicator on the default column confirms finished_at
                # descending is the initial sort.
                page.evaluate("document.querySelector('.metrics-complete-toggle input').click()")
                page.wait_for("document.querySelectorAll('.comparison-group-row').length === 2")
                self.assertEqual(page.evaluate("document.querySelector('.hidden-count').innerText"), "0 of 2 snapshots hidden")
                default_sort = page.evaluate("[...document.querySelectorAll('.sort-button')].find((button) => button.innerText.startsWith('Finished')).innerText")
                self.assertIn("▼", default_sort, "the default comparison sort is finished_at descending")

                # Every attempt's group has exactly one attempt in this
                # fixture; expanding it reveals the attempt row nested
                # beneath the group summary row (the expandable per-attempt
                # child-row requirement).
                page.evaluate("document.querySelector('.expand-toggle').click()")
                page.wait_for("document.querySelectorAll('.comparison-attempt-row').length === 1")

                # The per-attempt toggle switches to one row per attempt
                # (still two rows total here, since each logical graph has
                # exactly one attempt) without a page reload.
                page.evaluate("[...document.querySelectorAll('.comparison-grouping button')].find((button) => button.innerText === 'Per attempt').click()")
                page.wait_for("document.querySelectorAll('.comparison-table tbody tr').length === 2")
                self.assertEqual(page.evaluate("document.querySelectorAll('.comparison-group-row').length"), 0)

                # Clicking a metric column header sorts by it and flips
                # direction on a second click, ascending and descending
                # both remaining reachable (AC-DM06-2).
                page.evaluate("[...document.querySelectorAll('.sort-button')].find((button) => button.innerText.startsWith('Wall time')).click()")
                page.wait_for("[...document.querySelectorAll('.sort-button')].find((button) => button.innerText.startsWith('Wall time')).innerText.includes('▼')")
                page.evaluate("[...document.querySelectorAll('.sort-button')].find((button) => button.innerText.startsWith('Wall time')).click()")
                page.wait_for("[...document.querySelectorAll('.sort-button')].find((button) => button.innerText.startsWith('Wall time')).innerText.includes('▲')")

    def test_comparison_table_expanded_attempt_rows_follow_active_sort(self) -> None:
        """AC-DM06-2 / review-fix: a grouped comparison row's expanded
        per-attempt child rows must re-sort with the table's active column
        and direction, not stay pinned to the group's finished_at order
        (dashboard/plan-graph/src/components/ComparisonTable.jsx:
        expanded-attempt-row-sort)."""
        chrome = _chrome_binary()
        if chrome is None:
            self.skipTest("set DASHBOARD_E2E_CHROME to a Chrome binary to run UI certification")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_fixture(root)
            first = build_snapshot(root, "completed-graph")
            first["data_quality"]["completeness"] = "complete"
            first["timing"]["finished_at"] = "2026-08-10T00:00:00Z"
            first["timing"]["wall_clock_ms"] = {"state": "available", "value": 10_000, "reason": None}
            first["graph_metrics"]["timing"]["wall_clock_ms"] = {"state": "available", "value": 10_000, "reason": None}
            write_snapshot(root, first)
            # A second attempt of the *same* logical graph: an older finish
            # time (so it sorts second under the default finished_at
            # descending order) but a much longer wall time (so it must sort
            # first once the active sort column is switched to Wall time).
            second = json.loads(json.dumps(first))
            second["identity"]["run_id"] = "completed-graph-2"
            second["identity"]["graph_attempt_id"] = "completed-graph-2"
            second["display_name"] = "Completed Graph (attempt 2)"
            second["timing"]["finished_at"] = "2026-08-09T00:00:00Z"
            second["timing"]["wall_clock_ms"] = {"state": "available", "value": 90_000, "reason": None}
            second["graph_metrics"]["timing"]["wall_clock_ms"] = {"state": "available", "value": 90_000, "reason": None}
            write_snapshot(root, second)
            assets = Path("dashboard/plan-graph/dist").resolve()
            with patch("harness_labs.observability.dashboard_server.RunCatalog", side_effect=lambda source, **options: RunCatalog(source, process_probe=lambda pid: "fixture-process-token", **options)):
                app = DashboardApplication(root, assets_root=assets, refresh_seconds=0.001)
                server = create_dashboard_server(app, port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                page = _ChromePage(chrome)
                host, port = server.server_address[:2]
                self.addCleanup(server.server_close)
                self.addCleanup(lambda: thread.join(timeout=5))
                self.addCleanup(server.shutdown)
                self.addCleanup(page.close)
                page.command("Page.navigate", url=f"http://{host}:{port}/")
                page.wait_for("document.querySelectorAll('.runs button').length > 0")
                page.evaluate("document.querySelector('.view-toggle button:nth-child(2)').click()")
                page.wait_for("document.querySelectorAll('.snapshot-list button').length === 2")
                page.evaluate("[...document.querySelectorAll('.completed-toolbar button')].find((button) => button.innerText === 'Compare').click()")
                page.wait_for("document.querySelector('.comparison-table') !== null")
                # Both attempts share one logical_graph_id, so this is a
                # single grouped row with two attempts underneath it.
                page.wait_for("document.querySelectorAll('.comparison-group-row').length === 1")
                page.evaluate("document.querySelector('.expand-toggle').click()")
                page.wait_for("document.querySelectorAll('.comparison-attempt-row').length === 2")

                default_order = page.evaluate("[...document.querySelectorAll('.comparison-attempt-row td:nth-child(2)')].map((cell) => cell.innerText)")
                self.assertEqual(default_order, [first["display_name"], second["display_name"]], "default sort (finished_at descending) puts the newer attempt first")

                # Sorting by Wall time must reorder the expanded child rows
                # too: attempt 2 (90s) now outranks attempt 1 (10s), instead
                # of staying pinned to finished_at order.
                page.evaluate("[...document.querySelectorAll('.sort-button')].find((button) => button.innerText.startsWith('Wall time')).click()")
                page.wait_for(
                    "[...document.querySelectorAll('.comparison-attempt-row td:nth-child(2)')][0]?.innerText === "
                    + json.dumps(second["display_name"])
                )
                reordered = page.evaluate("[...document.querySelectorAll('.comparison-attempt-row td:nth-child(2)')].map((cell) => cell.innerText)")
                self.assertEqual(reordered, [second["display_name"], first["display_name"]], "expanded attempt rows must follow the active sort column, not a fixed finished_at order")

    def test_operator_legacy_graph_import_is_explicitly_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "approved-plan.md"
            plan.write_text("Import legacy graph AC-import\n", encoding="utf-8")
            decomposition = root / "decomposition.json"
            decomposition.write_text(json.dumps({
                "plan": str(plan), "base_commit": "a" * 40,
                "plan_sections": {"FR-import": "Import legacy graph AC-import"},
                "acceptance_criteria": {"AC-import": "import"},
                "runs": [{"id": "import-node", "objective": "Import legacy graph", "plan_sections": ["FR-import"], "criteria": ["AC-import"], "depends_on": []}],
            }), encoding="utf-8")
            state = root / "legacy-state.json"
            state.write_text(json.dumps({"completed": {"import-node": "b" * 40}}), encoding="utf-8")
            result = subprocess.run([
                sys.executable, "scripts/import_plan_graph_state.py", str(decomposition), str(state),
                "--run-root", str(root / "runs"), "--graph-run-id", "imported-graph",
            ], check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("incompatible", result.stderr)
            self.assertFalse((root / "runs" / "imported-graph").exists())


if __name__ == "__main__":
    unittest.main()
