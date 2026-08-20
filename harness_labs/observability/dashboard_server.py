"""Bounded, read-only HTTP surface for the verified run catalog."""
from __future__ import annotations

import hashlib
import json
import os
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
from harness_labs.observability import graph_metrics, plangraph_snapshot
from harness_labs.observability.run_catalog import RunCatalog, build_run_detail, merge_run_catalogs
from harness_labs.observability.run_metrics import TERMINAL_STATUSES

MAX_RUN_DIRECTORIES = 512
MAX_FILES_PER_RUN = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
# The /api/catalog body grows with every graph attempt across all audit
# roots; at 1 MiB the cap was crossed after ~25 attempts of one campaign,
# after which refresh() failed on every cycle and the dashboard silently
# served its last sub-cap snapshot forever (health still "ok").
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# A snapshot is served until a *successful* refresh replaces it, so a refresh
# that keeps failing leaves the dashboard serving frozen data indefinitely.
# Failures are reported immediately; the age threshold is the secondary net for
# a refresh that stops running without raising (a dead scheduler thread), and
# is deliberately generous because refresh is asynchronous and lazy -- an idle
# dashboard legitimately holds an old snapshot until the next request.
STALE_SNAPSHOT_REFRESH_MULTIPLIER = 30
MIN_STALE_SNAPSHOT_SECONDS = 60.0
MAX_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_TEXT = 512
MAX_AUDIT_ROOTS = 16
MAX_ROOT_REGISTRY_BYTES = 64 * 1024
# Named bound (plan DM-04): at most this many snapshot files are discovered
# and listed per audit root by GET /api/snapshots; excess files are reported
# as a truncation diagnostic rather than failing the listing. Per-file size
# reuses MAX_FILE_BYTES.
MAX_SNAPSHOT_FILES = 512
# Named bound (plan DM-04): at most this many directory entries are
# enumerated per audit root while discovering snapshot files, independent of
# and larger than MAX_SNAPSHOT_FILES so a directory containing many
# non-``.json`` entries cannot make the scan itself unbounded.
MAX_SNAPSHOT_DIRECTORY_ENTRIES = 8192
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class DashboardError(RuntimeError):
    """A safe-to-report dashboard configuration or catalog error."""


@dataclass(frozen=True)
class _Snapshot:
    catalog: bytes
    catalog_value: Mapping[str, Any]
    graph_details: Mapping[str, bytes]
    run_details: Mapping[str, bytes]
    # PlanGraph rollup documents (GET /api/plan-graph-metrics/<id>), lazily
    # built and cached here on first request. Every value in the served
    # document is revision-derived (own_summary/node details/budget ledger
    # all come from the current catalog revision's verified files; elapsed
    # time is deliberately never served -- see plan_graph_metrics), so this
    # cache never needs to expire before the enclosing _Snapshot itself is
    # replaced by the next catalog revision.
    graph_metrics: Mapping[str, bytes]
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
        self._graph_metrics_lock = threading.Lock()
        self._refresh_guard = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        self._snapshot: _Snapshot | None = None
        self._last_error: str | None = None
        self._consecutive_refresh_failures = 0

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
                self._consecutive_refresh_failures += 1
                return
            self._snapshot = candidate
            self._last_error = None
            self._consecutive_refresh_failures = 0

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

    @property
    def stale_after_seconds(self) -> float:
        """Age past which a served snapshot is reported as frozen."""

        return max(
            self.refresh_seconds * STALE_SNAPSHOT_REFRESH_MULTIPLIER,
            MIN_STALE_SNAPSHOT_SECONDS,
        )

    def health_report(self) -> dict[str, Any]:
        """Report whether the served snapshot is current, frozen, or absent.

        A snapshot is only replaced by a *successful* refresh, so reporting
        merely that one exists says nothing about whether it is current: the
        /api/catalog size-cap incident kept a snapshot in place while every
        refresh failed, and health reported "ok" throughout. Health therefore
        answers the question that matters -- is the data being served still
        being updated -- and reports "degraded" when it is not.
        """

        try:
            snapshot = self.snapshot()
        except DashboardError as exc:
            return {"status": "unavailable", "reason": _bounded_text(str(exc))}
        age = max(0.0, time.monotonic() - snapshot.created_at)
        failures = self._consecutive_refresh_failures
        error = self._last_error
        report: dict[str, Any] = {
            "status": "ok",
            "catalog_revision": snapshot.revision,
            "snapshot_age_seconds": round(age, 3),
            "stale_after_seconds": round(self.stale_after_seconds, 3),
            "consecutive_refresh_failures": failures,
        }
        if error is not None:
            # Refresh is failing while a previously built snapshot is still
            # being served -- the frozen-but-serving case exactly.
            report["status"] = "degraded"
            report["reason"] = (
                f"serving a snapshot {age:.1f}s old; "
                f"{failures} consecutive refresh failure(s), last: {error}"
            )
            report["refresh_error"] = error
        elif age >= self.stale_after_seconds:
            report["status"] = "degraded"
            report["reason"] = (
                f"serving a snapshot {age:.1f}s old with no refresh recorded "
                f"in the last {self.stale_after_seconds:.0f}s"
            )
        return report

    def health(self) -> bytes:
        return _json_bytes(self.health_report())

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

    def plan_graph_metrics(self, snapshot: _Snapshot, run_id: str) -> bytes | None:
        """Serve the DM-01 rollup for one PlanGraph, cached per catalog revision.

        Built lazily (not inside ``_build_snapshot``) so a plain
        ``/api/catalog`` request never pays for it -- matching the existing
        lazy-detail contract exercised by
        ``test_catalog_does_not_eagerly_project_feature_run_details``. A
        computation failure for this one graph can therefore never affect
        catalog building or any other graph; it degrades to an
        ``error``-flagged document instead of raising.
        """
        cached = snapshot.graph_metrics.get(run_id)
        if cached is not None:
            return cached
        with self._graph_metrics_lock:
            cached = snapshot.graph_metrics.get(run_id)
            if cached is not None:
                return cached
            graphs = {graph["run_id"]: graph for graph in snapshot.catalog_value["plan_graphs"]}
            graph = graphs.get(run_id)
            if graph is None:
                return None
            document = _compute_plan_graph_metrics(snapshot.catalog_value, graph)
            try:
                body = _json_bytes(document)
            except DashboardError:
                return None
            if isinstance(snapshot.graph_metrics, dict):
                snapshot.graph_metrics[run_id] = body
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
        revision = str(catalog_value["revision"])
        # The rollup cache is entirely revision-derived (see
        # plan_graph_metrics), so a refresh that reproduces the same
        # content-derived revision carries the previous snapshot's cache
        # forward instead of discarding and recomputing every graph.
        previous = self._snapshot
        graph_metrics_cache = previous.graph_metrics if previous is not None and previous.revision == revision else {}
        return _Snapshot(catalog, catalog_value, graph_details, {}, graph_metrics_cache, revision, time.monotonic())


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
        elif path.startswith("/api/plan-graph-metrics/"):
            # A distinct prefix from /api/plan-graphs/ (which validates its
            # trailing segment with _RUN_ID, so a /metrics suffix under it
            # would 404): avoids touching the dispatch order above.
            body = self.app.plan_graph_metrics(snapshot, _path_id(path, "/api/plan-graph-metrics/"))
            etag = True
        elif path == "/api/snapshots":
            listing, listing_etag = _list_snapshots(self.app.audit_roots, snapshot.catalog_value)
            try:
                body = _json_bytes(listing)
                etag = f'"{listing_etag}"'
            except DashboardError:
                # The listing itself (not any one entry) exceeded the
                # response cap; degrade to an empty, diagnosed listing
                # rather than a 5xx -- never a handler exception.
                body = _json_bytes({
                    "protocol": _SNAPSHOTS_LISTING_PROTOCOL,
                    "bounds": {"max_snapshot_files_per_root": MAX_SNAPSHOT_FILES, "max_file_bytes": MAX_FILE_BYTES},
                    "snapshots": [],
                    "diagnostics": [{"code": "listing_too_large", "message": "snapshot listing exceeds the response size limit", "run_id": None, "source_root": None}],
                })
                etag = True
        elif path.startswith("/api/snapshots/"):
            document = _find_snapshot_document(self.app.audit_roots, _path_id(path, "/api/snapshots/"))
            if document is None:
                body = None
            else:
                try:
                    body = _json_bytes(document)
                except DashboardError:
                    body = None
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
    # Dot-prefixed directories (.plan-graph-budgets, .plan-graph-locks,
    # .plan-graph-snapshots) are infrastructure beside run directories, not
    # runs -- run_catalog.build_run_catalog already skips them the same way.
    # Excluding them here too means they are neither walked for file-count
    # / oversize checks nor counted toward MAX_RUN_DIRECTORIES.
    entries = [entry for entry in root.iterdir() if not entry.name.startswith(".")]
    if len(entries) > MAX_RUN_DIRECTORIES:
        raise DashboardError("audit root exceeds run directory limit")
    excluded: dict[str, str] = {}
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        violation = _scan_run_tree(entry)
        if violation is not None:
            excluded[entry.name] = violation
    return excluded


# pytest owns ``<run>/verification-tmp`` as the verification stages' basetemp
# (harness_labs.featurerun.feature_run._verification_basetemp) and plants its
# own ``<name>current -> <name>N`` bookkeeping symlinks inside it. Nothing ever
# reads that tree: run_catalog reads a fixed set of files at the run root, and
# resolves journal-referenced artifacts under its own containment guards
# (resolve(strict=True) plus a run_dir-parent check). Walking the scratch tree
# therefore only lets pytest's own symlinks disqualify an otherwise sound run
# as "evidence unavailable", so prune it from the scan the way dot-prefixed
# infrastructure directories are pruned at the audit root.
VERIFICATION_SCRATCH_DIRNAME = "verification-tmp"


def _scan_run_tree(run: Path) -> str | None:
    """Return the bounding violation found in one run directory, else ``None``.

    Traverses explicitly (rather than via ``rglob``) so the scratch directory
    can be pruned before its contents are counted or symlink-checked.
    """

    count = 0
    stack: list[tuple[Path, bool]] = [(run, True)]
    while stack:
        directory, at_run_root = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError:
            # Parity with the previous rglob-based scan, which silently
            # skipped directories it could not read.
            continue
        for child in children:
            if at_run_root and child.name == VERIFICATION_SCRATCH_DIRNAME and not child.is_symlink():
                continue
            count += 1
            if count > MAX_FILES_PER_RUN:
                return "run exceeds file-count limit"
            if child.is_symlink():
                return "run contains a symlink"
            if child.is_dir(follow_symlinks=False):
                stack.append((Path(child.path), False))
            elif child.is_file(follow_symlinks=False) and child.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                return "run contains an oversized file"
    return None


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


# ---------------------------------------------------------------------------
# GET /api/plan-graph-metrics/<id> (DM-04): PlanGraph rollup over the shared
# graph_metrics implementation. See DashboardApplication.plan_graph_metrics
# for the per-revision cache; everything below is a pure read.
# ---------------------------------------------------------------------------


def _compute_plan_graph_metrics(catalog: Mapping[str, Any], graph: Mapping[str, Any]) -> dict[str, Any]:
    """Compute one graph's DM-01 rollup, degrading to an error document on
    any failure so one broken graph can never surface as a handler
    exception or otherwise affect any other graph's metrics."""
    run_id = graph.get("run_id")
    source_root = graph.get("source_root")
    try:
        node_details = _collect_node_details(catalog, graph)
        own_summary = _graph_own_summary(source_root, run_id)
        budget_ledger = _graph_budget_ledger(source_root, run_id)
        return graph_metrics.compute_graph_metrics(
            graph, catalog, node_details, own_summary=own_summary, budget_ledger=budget_ledger,
        )
    except Exception as exc:  # noqa: BLE001 - a single graph's failure must degrade, never propagate
        return {
            "protocol": graph_metrics.PROTOCOL,
            "run_id": run_id,
            "status": graph.get("status"),
            "error": {"state": "unavailable", "reason": _bounded_text(f"metrics computation failed: {exc}")},
        }


def _collect_node_details(catalog: Mapping[str, Any], graph: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each feature run's own (single-try, unmerged) detail-metrics document,
    keyed by run_id, exactly the shape ``compute_graph_metrics`` expects
    (mirrors ``plangraph_snapshot._collect_node_details``, adapted for a
    merged multi-root catalog where each run names its own root).

    Scoped to this one graph's attempt-scoped tries and their lineage
    histories -- the exact run_ids ``compute_graph_metrics`` needs for this
    graph's node loop -- rather than every feature run in the whole merged
    catalog, so a metrics request for one graph does no work proportional to
    unrelated graphs.
    """
    needed: set[str] = set()
    for node in graph.get("nodes", []) or []:
        if not isinstance(node, Mapping):
            continue
        needed.update(graph_metrics._attempt_scoped_history(graph, node, catalog))
        feature_run_id = node.get("feature_run_id")
        if isinstance(feature_run_id, str):
            needed.update(graph_metrics.node_history_run_ids(catalog, feature_run_id))
    # The campaign union also counts runs of nodes retired from this
    # attempt's checkpoint: every run belonging to a graph on the recorded
    # predecessor chain is needed, whether or not its node id survives here.
    graph_run_id = graph.get("run_id")
    if isinstance(graph_run_id, str):
        chain = {*graph_metrics.attempt_ancestors(catalog, graph_run_id), graph_run_id}
        graphs_by_id = {
            record.get("run_id"): record
            for record in catalog.get("plan_graphs", []) or []
            if isinstance(record, Mapping) and isinstance(record.get("run_id"), str)
        }
        for run in catalog.get("feature_runs", []) or []:
            if not isinstance(run, Mapping) or not isinstance(run.get("run_id"), str):
                continue
            correlation = run.get("correlation")
            if isinstance(correlation, Mapping) and correlation.get("plan_graph_id") in chain:
                needed.add(run["run_id"])
        for chain_graph_id in chain:
            record = graphs_by_id.get(chain_graph_id)
            for chain_node in (record.get("nodes", []) if isinstance(record, Mapping) else []):
                planned = chain_node.get("feature_run_id") if isinstance(chain_node, Mapping) else None
                if isinstance(planned, str):
                    needed.add(planned)
    runs = {
        run["run_id"]: run
        for run in catalog.get("feature_runs", []) or []
        if isinstance(run, Mapping) and isinstance(run.get("run_id"), str)
    }
    details: dict[str, dict[str, Any]] = {}
    for run_id in needed:
        run = runs.get(run_id)
        if run is None or run.get("status") == "corrupt":
            continue
        source_root = run.get("source_root")
        if not isinstance(source_root, str):
            continue
        try:
            detail = build_run_detail(Path(source_root), run_id)
        except (AuditError, OSError, ValueError, json.JSONDecodeError):
            continue
        details[run_id] = detail["metrics"]
    return details


def _graph_own_summary(source_root: Any, run_id: Any) -> Mapping[str, Any] | None:
    """The graph's own verified ``summary.json`` contents, or ``None`` when
    unavailable (live graph, missing summary, or unreadable run)."""
    if not isinstance(source_root, str) or not isinstance(run_id, str):
        return None
    try:
        detail = build_run_detail(Path(source_root), run_id)
    except (AuditError, OSError, ValueError, json.JSONDecodeError):
        return None
    usage = detail.get("usage")
    return usage if isinstance(usage, Mapping) else None


def _graph_budget_ledger(source_root: Any, run_id: Any) -> Mapping[str, Any] | None:
    """The retry-budget ledger for this graph attempt, via the same lookup
    plangraph_snapshot's DM-03 builder uses, so live and snapshot retry
    counters can never diverge."""
    if not isinstance(source_root, str) or not isinstance(run_id, str):
        return None
    return plangraph_snapshot._read_budget_ledger(Path(source_root), run_id)


# ---------------------------------------------------------------------------
# GET /api/snapshots, GET /api/snapshots/<id> (DM-04): per-request, bounded
# discovery of .plan-graph-snapshots/ files written by DM-03. Never cached
# across requests -- unlike the catalog, a snapshot file is invisible to the
# catalog revision (dot-dir skip), so a snapshot written while the server is
# running must be visible without waiting for a catalog refresh.
# ---------------------------------------------------------------------------

_SNAPSHOTS_LISTING_PROTOCOL = "harness-dashboard-snapshots-listing/1"


def _list_snapshots(audit_roots: tuple[Path, ...], catalog_value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Bounded per-request listing: headline metrics for every discovered,
    well-formed snapshot, plus ``snapshot_missing`` stubs for terminal
    catalog graphs with none. Named bounds: at most MAX_SNAPSHOT_FILES
    snapshot files per audit root; each file capped at MAX_FILE_BYTES.
    Symlinked, oversize, or malformed files degrade to a per-entry
    diagnostic; they never fail the listing or raise."""
    entries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    etag_material: list[list[Any]] = []
    seen_ids: set[str] = set()
    for root in audit_roots:
        directory = root / plangraph_snapshot.SNAPSHOT_DIRNAME
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            json_children = []
            truncated_entries = False
            with os.scandir(directory) as it:
                for scanned, entry in enumerate(it, start=1):
                    if scanned > MAX_SNAPSHOT_DIRECTORY_ENTRIES:
                        truncated_entries = True
                        break
                    if entry.name.endswith(".json"):
                        json_children.append(entry)
        except OSError as exc:
            diagnostics.append(_snapshot_diagnostic("snapshot_root_unreadable", str(exc), None, root))
            continue
        if truncated_entries:
            diagnostics.append(_snapshot_diagnostic(
                "snapshot_directory_truncated",
                f"snapshot directory contains more than {MAX_SNAPSHOT_DIRECTORY_ENTRIES} entries; enumeration was stopped early",
                None, root,
            ))
        children = sorted(json_children, key=lambda entry: entry.name)
        if len(children) > MAX_SNAPSHOT_FILES:
            diagnostics.append(_snapshot_diagnostic(
                "snapshot_count_truncated",
                f"{len(children)} snapshot files exceed the {MAX_SNAPSHOT_FILES} per-root limit; only the first {MAX_SNAPSHOT_FILES} (by name) were listed",
                None, root,
            ))
            children = children[:MAX_SNAPSHOT_FILES]
        for child in children:
            snapshot_id = child.name[: -len(".json")]
            try:
                stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                diagnostics.append(_snapshot_diagnostic("snapshot_unreadable", str(exc), snapshot_id if _RUN_ID.fullmatch(snapshot_id) else None, root))
                continue
            etag_material.append([str(Path(child.path)), stat.st_size, int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))])
            if not _RUN_ID.fullmatch(snapshot_id):
                diagnostics.append(_snapshot_diagnostic("snapshot_invalid_name", "snapshot filename is not a safe id", None, root))
                continue
            if child.is_symlink():
                diagnostics.append(_snapshot_diagnostic("snapshot_symlink_rejected", "snapshot file must not be a symlink", snapshot_id, root))
                continue
            document, reason = _read_snapshot_file(Path(child.path))
            if document is None:
                diagnostics.append(_snapshot_diagnostic("snapshot_malformed", reason or "snapshot file could not be read", snapshot_id, root))
                continue
            # Only a healthy, served entry suppresses the terminal-graph
            # snapshot_missing stub below -- a symlinked or malformed file
            # must still surface the stub alongside its diagnostic, never
            # neither.
            seen_ids.add(snapshot_id)
            entries.append(_snapshot_headline(document, str(root)))
    for graph in catalog_value.get("plan_graphs", []) or []:
        if not isinstance(graph, Mapping) or graph.get("status") not in TERMINAL_STATUSES:
            continue
        graph_id = graph.get("graph_attempt_id") or graph.get("run_id")
        if not isinstance(graph_id, str) or graph_id in seen_ids:
            continue
        # DM-03 never writes a snapshot for an "interrupted" graph unless
        # captured with --include-interrupted: that absence is by design,
        # not an emission hole, so it gets a distinct, non-alarming reason.
        reason = (
            "interrupted graph attempts do not receive a metrics snapshot unless captured with --include-interrupted"
            if graph.get("status") == "interrupted"
            else "no metrics snapshot has been written for this terminal graph attempt"
        )
        entries.append({
            "run_id": graph.get("run_id"),
            "logical_graph_id": graph.get("logical_graph_id"),
            "graph_attempt_id": graph.get("graph_attempt_id"),
            "display_name": graph.get("display_name") or graph.get("run_id"),
            "status": graph.get("status"),
            "finished_at": None, "wall_clock_ms": None, "tokens": None, "cost": None, "completeness": None,
            "snapshot_missing": True,
            "reason": reason,
            "source_root": None,
        })
    populated = sorted((entry for entry in entries if entry.get("finished_at")), key=lambda entry: entry["finished_at"], reverse=True)
    unpopulated = [entry for entry in entries if not entry.get("finished_at")]
    document = {
        "protocol": _SNAPSHOTS_LISTING_PROTOCOL,
        "bounds": {"max_snapshot_files_per_root": MAX_SNAPSHOT_FILES, "max_file_bytes": MAX_FILE_BYTES},
        "snapshots": populated + unpopulated,
        "diagnostics": diagnostics[:MAX_DIAGNOSTICS],
    }
    etag = hashlib.sha256(json.dumps(
        {"files": sorted(etag_material), "revision": catalog_value.get("revision")},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return document, etag


def _snapshot_diagnostic(code: str, message: str, run_id: str | None, root: Path) -> dict[str, Any]:
    return {"code": code, "message": _bounded_text(message), "run_id": run_id, "source_root": str(root)}


def _snapshot_headline(document: Mapping[str, Any], source_root: str) -> dict[str, Any]:
    identity = document.get("identity") if isinstance(document.get("identity"), Mapping) else {}
    metrics = document.get("graph_metrics") if isinstance(document.get("graph_metrics"), Mapping) else {}
    totals = metrics.get("totals") if isinstance(metrics.get("totals"), Mapping) else {}
    timing = document.get("timing") if isinstance(document.get("timing"), Mapping) else {}
    data_quality = document.get("data_quality") if isinstance(document.get("data_quality"), Mapping) else {}
    finished_at = timing.get("finished_at")
    return {
        "run_id": identity.get("run_id"),
        "logical_graph_id": identity.get("logical_graph_id"),
        "graph_attempt_id": identity.get("graph_attempt_id"),
        "display_name": document.get("display_name"),
        "status": document.get("status"),
        # Sorted on below (populated entries, by finished_at); a document
        # with a non-string value here must degrade to unavailable rather
        # than raise a TypeError sorting alongside a well-formed string.
        "finished_at": finished_at if isinstance(finished_at, str) else None,
        "wall_clock_ms": timing.get("wall_clock_ms"),
        "tokens": totals.get("tokens"),
        "cost": totals.get("cost"),
        "completeness": data_quality.get("completeness"),
        "snapshot_missing": False,
        "reason": None,
        "source_root": source_root,
    }


def _read_snapshot_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if path.is_symlink():
        return None, "snapshot file must not be a symlink"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"snapshot file could not be read: {exc}"
    if size > MAX_FILE_BYTES:
        return None, "snapshot file exceeds size limit"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"snapshot file could not be read: {exc}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, "snapshot file is not valid JSON"
    if not isinstance(value, Mapping) or value.get("protocol") != plangraph_snapshot.PROTOCOL:
        return None, "snapshot file does not match the expected protocol"
    return dict(value), None


def _find_snapshot_document(audit_roots: tuple[Path, ...], snapshot_id: str) -> dict[str, Any] | None:
    if not snapshot_id:
        return None
    for root in audit_roots:
        directory = root / plangraph_snapshot.SNAPSHOT_DIRNAME
        if directory.is_symlink() or not directory.is_dir():
            continue
        path = directory / f"{snapshot_id}.json"
        if not path.is_file() and not path.is_symlink():
            continue
        document, _ = _read_snapshot_file(path)
        if document is not None:
            return document
    return None


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
