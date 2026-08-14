"""Bounded, read-only HTTP surface for the verified run catalog."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from harness_labs.core.audit import AuditError
from harness_labs.observability import graph_metrics
from harness_labs.observability.run_catalog import RunCatalog, build_run_detail, merge_run_catalogs

MAX_RUN_DIRECTORIES = 512
MAX_FILES_PER_RUN = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_TEXT = 512
MAX_AUDIT_ROOTS = 16
MAX_ROOT_REGISTRY_BYTES = 64 * 1024
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class DashboardError(RuntimeError):
    """A safe-to-report dashboard configuration or catalog error."""


@dataclass(frozen=True)
class _Snapshot:
    catalog: bytes
    catalog_value: Mapping[str, Any]
    graph_details: Mapping[str, bytes]
    run_details: Mapping[str, bytes]
    revision: str
    created_at: float


class DashboardApplication:
    """Owns immutable catalog bytes which are atomically replaced on refresh."""

    def __init__(
        self,
        audit_root: Path | Iterable[Path],
        *,
        assets_root: Path | None = None,
        refresh_seconds: float = 2.0,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        self.audit_roots = _contained_roots(audit_root)
        # Compatibility for callers that inspect the former single-root field.
        self.audit_root = self.audit_roots[0]
        self.assets_root = _contained_assets(assets_root) if assets_root is not None else None
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._detail_lock = threading.Lock()
        self._refresh_guard = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        self._snapshot: _Snapshot | None = None
        self._last_error: str | None = None

    def snapshot(self) -> _Snapshot:
        current = self._snapshot
        if current is None:
            self.refresh()
        elif time.monotonic() - current.created_at >= self.refresh_seconds:
            self._schedule_refresh()
        current = self._snapshot
        if current is None:
            raise DashboardError(self._last_error or "catalog is unavailable")
        return current

    def refresh(self) -> None:
        with self._lock:
            current = self._snapshot
            if current is not None and time.monotonic() - current.created_at < self.refresh_seconds:
                return
            try:
                candidate = self._build_snapshot()
            except (AuditError, DashboardError, OSError, ValueError, json.JSONDecodeError) as exc:
                self._last_error = _bounded_text(str(exc))
                return
            self._snapshot = candidate
            self._last_error = None

    def _schedule_refresh(self) -> None:
        with self._refresh_guard:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            self._refresh_thread = threading.Thread(target=self._background_refresh, name="dashboard-catalog-refresh", daemon=True)
            self._refresh_thread.start()

    def _background_refresh(self) -> None:
        try:
            self.refresh()
        finally:
            with self._refresh_guard:
                if self._refresh_thread is threading.current_thread():
                    self._refresh_thread = None

    def health(self) -> bytes:
        try:
            snapshot = self.snapshot()
        except DashboardError as exc:
            return _json_bytes({"status": "unavailable", "reason": _bounded_text(str(exc))})
        return _json_bytes({"status": "ok", "catalog_revision": snapshot.revision})

    def asset(self, request_path: str) -> tuple[bytes, str] | None:
        if self.assets_root is None:
            return None
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        if not relative or Path(relative).is_absolute() or "\\" in relative:
            return None
        target = self.assets_root.joinpath(*Path(relative).parts)
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            return None
        if resolved.is_symlink() or resolved.parent != self.assets_root and self.assets_root not in resolved.parents:
            return None
        if not resolved.is_file() or resolved.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        media = "text/html; charset=utf-8" if resolved.suffix == ".html" else "application/javascript; charset=utf-8" if resolved.suffix == ".js" else "text/css; charset=utf-8" if resolved.suffix == ".css" else "application/octet-stream"
        return resolved.read_bytes(), media

    def feature_run_detail(self, snapshot: _Snapshot, run_id: str) -> bytes | None:
        cached = snapshot.run_details.get(run_id)
        if cached is not None:
            return cached
        with self._detail_lock:
            cached = snapshot.run_details.get(run_id)
            if cached is not None:
                return cached
            runs = {run["run_id"]: run for run in snapshot.catalog_value["feature_runs"]}
            target = runs.get(run_id)
            if target is None or target.get("status") == "corrupt":
                return None
            details: dict[str, dict[str, Any]] = {}
            for history_id in graph_metrics.node_history_run_ids(snapshot.catalog_value, run_id):
                run = runs.get(history_id)
                if run is None or run.get("status") == "corrupt":
                    continue
                try:
                    details[history_id] = build_run_detail(Path(run["source_root"]), history_id)
                except (AuditError, DashboardError, OSError, ValueError, json.JSONDecodeError):
                    continue
            _apply_cumulative_node_metrics(snapshot.catalog_value, details)
            detail = details.get(run_id)
            if detail is None:
                return None
            try:
                body = _json_bytes(detail)
            except DashboardError:
                return None
            if isinstance(snapshot.run_details, dict):
                snapshot.run_details[run_id] = body
            return body

    def _build_snapshot(self) -> _Snapshot:
        source_catalogs = []
        for root in self.audit_roots:
            excluded_runs = _validate_audit_tree(root)
            source_catalogs.append((root, RunCatalog(root, excluded_runs=excluded_runs).snapshot()))
        catalog_value = merge_run_catalogs(source_catalogs)
        catalog_value = _cap_diagnostics(catalog_value)
        _ensure_unique_ids(catalog_value)
        graph_details: dict[str, bytes] = {}
        for graph in catalog_value["plan_graphs"]:
            graph_details[graph["run_id"]] = _json_bytes(graph)
        catalog = _json_bytes(catalog_value)
        return _Snapshot(catalog, catalog_value, graph_details, {}, str(catalog_value["revision"]), time.monotonic())


def create_dashboard_server(application: DashboardApplication, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """Create a server; callers retain lifecycle control for tests and scripts."""
    class Handler(_DashboardHandler):
        app = application
    return ThreadingHTTPServer((host, port), Handler)


class _DashboardHandler(BaseHTTPRequestHandler):
    app: DashboardApplication
    server_version = "HarnessDashboard/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._send(HTTPStatus.NOT_FOUND, _json_bytes({"error": "not found"}))
            return
        path = parsed.path
        if path == "/api/health":
            body = self.app.health()
            self._send(HTTPStatus.OK if b'"status":"ok"' in body else HTTPStatus.SERVICE_UNAVAILABLE, body)
            return
        if not path.startswith("/api/"):
            asset = self.app.asset(path)
            if asset is None:
                self._send(HTTPStatus.NOT_FOUND, _json_bytes({"error": "not found"}))
            else:
                self._send(HTTPStatus.OK, asset[0], content_type=asset[1], etag=False)
            return
        try:
            snapshot = self.app.snapshot()
        except DashboardError as exc:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, _json_bytes({"error": "catalog unavailable", "reason": _bounded_text(str(exc))}))
            return
        body: bytes | None
        if path == "/api/catalog":
            body = snapshot.catalog
            etag: bool | str = f'"{snapshot.revision}"'
        elif path == "/api/plan-graphs":
            body = _json_bytes(snapshot.catalog_value["plan_graphs"])
            etag = True
        elif path.startswith("/api/plan-graphs/"):
            body = snapshot.graph_details.get(_path_id(path, "/api/plan-graphs/"))
            etag = True
        elif path.startswith("/api/feature-runs/"):
            body = self.app.feature_run_detail(snapshot, _path_id(path, "/api/feature-runs/"))
            etag = True
        else:
            body = None
            etag = True
        if body is None:
            self._send(HTTPStatus.NOT_FOUND, _json_bytes({"error": "not found"}))
        else:
            self._send(HTTPStatus.OK, body, etag=etag)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", etag=False)

    def do_POST(self) -> None:  # noqa: N802
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", etag=False)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _send(self, status: HTTPStatus, body: bytes, *, content_type: str = "application/json; charset=utf-8", etag: bool | str = True) -> None:
        digest = etag if isinstance(etag, str) else '"' + hashlib.sha256(body).hexdigest() + '"'
        if etag and status == HTTPStatus.OK and self.headers.get("If-None-Match") == digest:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", digest)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", digest)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _contained_root(path: Path) -> Path:
    supplied = Path(path)
    if supplied.is_symlink():
        raise DashboardError("audit root must not be a symlink")
    return supplied.resolve()


def _contained_roots(paths: Path | Iterable[Path]) -> tuple[Path, ...]:
    supplied = [paths] if isinstance(paths, (str, Path)) else list(paths)
    if not supplied:
        raise DashboardError("at least one audit root is required")
    if len(supplied) > MAX_AUDIT_ROOTS:
        raise DashboardError("audit root count exceeds limit")
    roots = tuple(_contained_root(Path(path)) for path in supplied)
    if len(set(roots)) != len(roots):
        raise DashboardError("configured audit roots must be unique")
    return roots


def load_audit_root_registry(path: Path) -> tuple[Path, ...]:
    """Read one closed, bounded registry of explicit audit roots."""
    supplied = Path(path)
    if supplied.is_symlink() or not supplied.is_file():
        raise DashboardError("audit root registry must be a file and not a symlink")
    if supplied.stat().st_size > MAX_ROOT_REGISTRY_BYTES:
        raise DashboardError("audit root registry exceeds size limit")
    try:
        value = json.loads(supplied.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DashboardError("audit root registry is not valid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"protocol", "audit_roots"}
        or value.get("protocol") != "harness-dashboard-audit-root-registry/1"
        or not isinstance(value.get("audit_roots"), list)
        or not value["audit_roots"]
        or not all(isinstance(item, str) and item for item in value["audit_roots"])
    ):
        raise DashboardError("audit root registry is invalid")
    if len(value["audit_roots"]) > MAX_AUDIT_ROOTS:
        raise DashboardError("audit root registry exceeds root-count limit")
    base = supplied.resolve().parent
    roots = []
    for item in value["audit_roots"]:
        candidate = Path(item).expanduser()
        roots.append(candidate if candidate.is_absolute() else base / candidate)
    return tuple(roots)


def _contained_assets(path: Path) -> Path:
    supplied = Path(path)
    if supplied.is_symlink() or not supplied.is_dir():
        raise DashboardError("assets root must be a directory and not a symlink")
    return supplied.resolve()


def _validate_audit_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    entries = list(root.iterdir())
    if len(entries) > MAX_RUN_DIRECTORIES:
        raise DashboardError("audit root exceeds run directory limit")
    excluded: dict[str, str] = {}
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        count = 0
        for child in entry.rglob("*"):
            count += 1
            if count > MAX_FILES_PER_RUN:
                excluded[entry.name] = "run exceeds file-count limit"
                break
            if child.is_symlink():
                excluded[entry.name] = "run contains a symlink"
                break
            if child.is_file() and child.stat().st_size > MAX_FILE_BYTES:
                excluded[entry.name] = "run contains an oversized file"
                break
    return excluded


def _cap_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    diagnostics = value.get("diagnostics", [])
    result["diagnostics"] = [
        {"code": _bounded_text(str(item.get("code", "diagnostic"))), "message": _bounded_text(str(item.get("message", "invalid run"))), "run_id": item.get("run_id") if isinstance(item.get("run_id"), str) else None, "source_root": item.get("source_root") if isinstance(item.get("source_root"), str) else None}
        for item in diagnostics[:MAX_DIAGNOSTICS]
        if isinstance(item, Mapping)
    ]
    return result


def _apply_cumulative_node_metrics(catalog: Mapping[str, Any], details: dict[str, dict[str, Any]]) -> None:
    """Thin delegating shim: the merge implementation lives in ``graph_metrics``.

    Kept as a module-level name (rather than inlining the call at the call
    site) because it is part of this module's existing, tested surface.
    """
    graph_metrics.apply_cumulative_node_metrics(catalog, details)


def _ensure_unique_ids(catalog: Mapping[str, Any]) -> None:
    ids: set[str] = set()
    for family in ("plan_graphs", "feature_runs"):
        for record in catalog.get(family, []):
            run_id = record.get("run_id") if isinstance(record, Mapping) else None
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in ids:
                raise DashboardError("catalog contains duplicate or invalid run IDs")
            ids.add(run_id)


def _path_id(path: str, prefix: str) -> str:
    encoded = path.removeprefix(prefix)
    value = unquote(encoded)
    if "/" in encoded or not _RUN_ID.fullmatch(value):
        return ""
    return value


def _bounded_text(value: str) -> str:
    return value[:MAX_DIAGNOSTIC_TEXT]


def _json_bytes(value: Any) -> bytes:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        raise DashboardError("response exceeds size limit")
    return body
