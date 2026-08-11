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

from harness_labs.dashboard_server import DashboardApplication, _DashboardHandler, create_dashboard_server
from harness_labs.run_catalog import RunCatalog
from scripts.dashboard_fixture_run import advance_live_fixture, create_fixture


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
            with patch("harness_labs.dashboard_server.RunCatalog", side_effect=lambda source, **options: RunCatalog(source, process_probe=lambda pid: "fixture-process-token", **options)):
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
                page.evaluate("[...document.querySelectorAll('.react-flow__node')].find((node) => node.innerText.includes('live-child')).click()")
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
