"""The ephemeral ``liveness.json`` lease a running controller owns.

``harness_labs/observability/run_catalog.py`` has always read this file, and
until now nothing outside ``scripts/dashboard_fixture_run.py`` ever wrote one:
every real non-terminal run projected as ``liveness_unavailable``, so the
dashboard could not say whether anything was actually alive.  The repair is on
this side rather than in the catalog because ``harness-controller-liveness/1``
is not a new convention -- it has a schema (``schemas/controller-liveness.
schema.json``) and a specification (``docs/development/live-plangraph-dashboard
-plan.md`` section 3, "the owning controller writes it atomically with mode
0600, refreshes it on a short heartbeat independent of phase duration, and
stops refreshing on exit").  It was simply never implemented.

The markers production *does* write cannot answer the catalog's question.
``plan-graph-admission-liveness.json`` carries only ``{protocol, pid,
process_start_token}``: no run identity, no controller kind, and above all no
heartbeat, so it can never distinguish a controller that is working from one
that is wedged, and it is written once at admission and never refreshed.
``plan-graph-liveness.json`` is a *child*-authored marker in the child's own
protocol, and nothing in the repository writes one either, so teaching the
catalog to read it would have changed nothing for a real run.  Teaching the
catalog to read those would also have meant giving one filename two protocols:
``PlanGraphAudit._child_liveness`` already accepts ``liveness.json`` as an
alias for ``plan-graph-liveness.json`` and validates it as
``harness-plan-graph-parallel-liveness/1``.  A lease written here is simply
not that protocol, so ``_child_liveness`` reads it, rejects it, and returns
``None`` -- the same "no marker" answer it gives today.

Death is judged the way ``PlanGraphAudit.reclaim_orphaned_successor_attempt``
and ``_liveness_disposition`` judge it, and for the same reason: a bare pid is
not an identity.  The lease records the pid *and* the host's process-start
token for that pid, and ``run_catalog._liveness`` reports ``live`` only when
the probe returns that same token.  A controller that was killed leaves its
last lease behind; the pid is either gone (token ``None``) or has been recycled
by an unrelated process (a different token), and either way the run reads
``stale``, never ``live``.  The heartbeat is the second, independent guard: a
lease that stops being refreshed ages out of the catalog's freshness window on
its own.
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from uuid import uuid4


LIVENESS_PROTOCOL = "harness-controller-liveness/1"
LIVENESS_FILENAME = "liveness.json"
CONTROLLER_KINDS = frozenset({"plan_graph", "feature_run"})
#: Well inside ``build_run_catalog``'s 30-second default freshness window, so a
#: single missed beat does not make a working controller look stale.
DEFAULT_HEARTBEAT_SECONDS = 5.0


def process_start_token(pid: int) -> str | None:
    """Return an immutable process-start token when the host can observe one.

    Mirrors ``plan_graph._local_process_start_token`` and
    ``plan_graph_audit._process_start_token`` rather than importing either:
    ``core`` sits below both and must not import back up from them.
    """

    try:
        return str(os.stat(f"/proc/{pid}").st_ctime_ns)
    except OSError:
        pass
    try:
        observed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return observed.stdout.strip() or None


class _Heartbeat:
    """One daemon thread that refreshes every lease this process owns.

    Deliberately not a thread per lease: a PlanGraph controller and each of
    its in-process children hold leases at once, and a thread apiece would
    scale the cost of observability with the width of the graph.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._leases: list["ControllerLivenessLease"] = []
        self._thread: threading.Thread | None = None
        self._interval = DEFAULT_HEARTBEAT_SECONDS

    def add(self, lease: "ControllerLivenessLease") -> None:
        with self._mutex:
            self._leases.append(lease)
            if lease.interval_seconds <= 0:
                # Registered for shutdown only; the owner beats it by hand.
                return
            self._interval = min(self._interval, lease.interval_seconds)
            if self._thread is None:
                # One atexit hook for the process, not one per run: a
                # controller that supervises many runs would otherwise
                # accumulate a callback apiece.
                atexit.register(self._shutdown)
                self._thread = threading.Thread(
                    target=self._loop, name="controller-liveness", daemon=True
                )
                self._thread.start()

    def discard(self, lease: "ControllerLivenessLease") -> None:
        with self._mutex:
            if lease in self._leases:
                self._leases.remove(lease)

    def _shutdown(self) -> None:
        with self._mutex:
            leases = tuple(self._leases)
        for lease in leases:
            lease.stop()

    def _loop(self) -> None:
        while True:
            with self._mutex:
                interval, leases = self._interval, tuple(self._leases)
            sleep(interval)
            for lease in leases:
                lease.beat()


_HEARTBEAT = _Heartbeat()


class ControllerLivenessLease:
    """Write and refresh one run's lease for as long as this process owns it."""

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        controller_kind: str,
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        write: bool = True,
    ) -> None:
        if controller_kind not in CONTROLLER_KINDS:
            raise ValueError("controller_kind must be plan_graph or feature_run")
        if not run_id or "/" in run_id:
            raise ValueError("run_id must be a non-empty path-safe name")
        self.path = Path(run_dir) / LIVENESS_FILENAME
        self.run_id = run_id
        self.controller_kind = controller_kind
        self.interval_seconds = interval_seconds
        self.instance_id = str(uuid4())
        self.pid = os.getpid()
        # Sampled once: the token identifies *this* process, and re-reading it
        # each beat would turn a probe that briefly fails into a lease that
        # claims a different identity than the one it started with.
        self.process_start_token = process_start_token(self.pid) or ""
        self.hostname = socket.gethostname()
        self._sequence = 0
        self._mutex = threading.Lock()
        self._stopped = threading.Event()
        self._enabled = bool(write) and bool(self.process_start_token)
        if not self._enabled:
            # A host that cannot report a process-start token cannot support
            # the identity rule this lease depends on.  Writing a lease with an
            # empty token would fail the catalog's own validation and report as
            # an invalid lease; writing nothing reports "no liveness lease",
            # which is the truthful answer.
            return
        self.beat()
        # Registering also arranges the process-exit sweep: a controller that
        # exits without finalizing -- an unhandled error, a SIGTERM the
        # interpreter catches -- stops claiming to be alive.  A hard kill
        # leaves the lease behind, which is what the identity check is for.
        _HEARTBEAT.add(self)

    def beat(self) -> None:
        """Write the next heartbeat.  Never raises into the controller."""

        if not self._enabled or self._stopped.is_set():
            return
        with self._mutex:
            self._sequence += 1
            payload = {
                "protocol": LIVENESS_PROTOCOL,
                "run_id": self.run_id,
                "controller_instance_id": self.instance_id,
                "hostname": self.hostname,
                "pid": self.pid,
                "process_start_token": self.process_start_token,
                "heartbeat_sequence": self._sequence,
                "heartbeat_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "controller_kind": self.controller_kind,
            }
            try:
                _atomic_write(
                    self.path,
                    (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                        "utf-8"
                    ),
                )
            except OSError:
                # The lease is operational state, never audit evidence.  A run
                # must not fail because its dashboard hint could not be
                # written; a missing lease reads as "no liveness lease".
                pass

    def stop(self) -> None:
        """Stop refreshing and remove the lease.  Idempotent."""

        if self._stopped.is_set():
            return
        self._stopped.set()
        _HEARTBEAT.discard(self)
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ControllerLivenessLease":
        return self

    def __exit__(self, *_exception: object) -> None:
        self.stop()


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


__all__ = [
    "CONTROLLER_KINDS",
    "ControllerLivenessLease",
    "DEFAULT_HEARTBEAT_SECONDS",
    "LIVENESS_FILENAME",
    "LIVENESS_PROTOCOL",
    "process_start_token",
]
