"""Contract and behavior tests for the ``plangraph-metrics-snapshot/1``
builder, offline CLI, best-effort emission hooks, and run-root
self-registration (PlanGraph node DM-03).

Fixtures use real, verified run directories (via ``PlanGraphAudit`` /
``AuditJournal`` / ``PlanGraph.run()``) rather than synthetic dicts, because
the builder under test reads run directories through ``build_run_catalog``
and ``project_run_metrics``, both of which require an
``AuditJournal.verify``-authenticated journal.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Mapping

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.observability import graph_metrics
from harness_labs.observability.plangraph_snapshot import (
    SnapshotSkipped,
    build_snapshot,
    write_snapshot,
)
from harness_labs.observability.run_catalog import build_run_catalog, build_run_detail
from harness_labs.observability.run_metrics import project_run_metrics
from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    persist_registration,
    register_plan_graph,
)
from harness_labs.plangraph.plan_graph_audit import PlanGraphAudit

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO_ROOT / "schemas" / "plangraph-metrics-snapshot.schema.json").read_text(encoding="utf-8"))


class SchemaValidationError(ValueError):
    """Raised when a document violates the closed schema subset used here."""


class ClosedSchemaValidator:
    """Small dependency-free validator, adapted from
    ``tests/test_run_catalog_contracts.py`` with ``"number"`` type support
    (the snapshot schema carries float fields -- token cost, cache savings --
    that the original validator's type table did not recognize)."""

    def __init__(self, schema: dict[str, object]) -> None:
        self.schema = schema

    def validate(self, value: object) -> None:
        self._validate(value, self.schema, "$")

    def _resolve(self, reference: str) -> dict[str, object]:
        target: object = self.schema
        for part in reference.removeprefix("#/").split("/"):
            if not isinstance(target, dict):
                raise SchemaValidationError(f"invalid reference {reference}")
            target = target[part]
        if not isinstance(target, dict):
            raise SchemaValidationError(f"invalid reference target {reference}")
        return target

    def _valid(self, value: object, schema: dict[str, object]) -> bool:
        try:
            self._validate(value, schema, "$")
        except SchemaValidationError:
            return False
        return True

    def _validate(self, value: object, schema: dict[str, object], path: str) -> None:
        if "$ref" in schema:
            self._validate(value, self._resolve(str(schema["$ref"])), path)
            return
        for part in schema.get("allOf", []):
            if not isinstance(part, dict):
                raise SchemaValidationError(f"{path}: invalid allOf schema")
            self._validate(value, part, path)
        condition = schema.get("if")
        if isinstance(condition, dict) and self._valid(value, condition):
            consequence = schema.get("then")
            if isinstance(consequence, dict):
                self._validate(value, consequence, path)
        if "const" in schema and value != schema["const"]:
            raise SchemaValidationError(f"{path}: expected {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise SchemaValidationError(f"{path}: value is outside its enum")
        expected = schema.get("type")
        types = expected if isinstance(expected, list) else [expected]
        if expected is not None and not any(self._matches(value, item) for item in types):
            raise SchemaValidationError(f"{path}: wrong type ({value!r} vs {types})")
        if isinstance(value, dict):
            required = schema.get("required", [])
            for name in required:
                if name not in value:
                    raise SchemaValidationError(f"{path}: missing {name}")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extras = set(value) - set(properties)
                if extras:
                    raise SchemaValidationError(f"{path}: unexpected {sorted(extras)!r}")
            for name, item in properties.items():
                if name in value and isinstance(item, dict):
                    self._validate(value[name], item, f"{path}.{name}")
        if isinstance(value, list):
            item = schema.get("items")
            if isinstance(item, dict):
                for index, child in enumerate(value):
                    self._validate(child, item, f"{path}[{index}]")
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise SchemaValidationError(f"{path}: shorter than minLength")

    @staticmethod
    def _matches(value: object, expected: object) -> bool:
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }.get(expected, False)


# ---------------------------------------------------------------------------
# Fixture construction: real, verified PlanGraph + FeatureRun run directories
# ---------------------------------------------------------------------------

_PLAN_SECTIONS = {"1": "Section one text"}
_ACCEPTANCE_CRITERIA = {"AC-1": "Criterion one text", "AC-2": "Criterion two text"}


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()


def _init_repository(root: Path) -> tuple[Path, str]:
    """A real git repository whose one commit carries both a working file and
    ``plan.md`` as a valid, digest-checkable decomposition (``plan_sections``
    / ``acceptance_criteria`` only -- the builder reads no other key)."""
    repository = root / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.com")
    _git(repository, "config", "user.name", "Tests")
    (repository / "feature.txt").write_text("hello\n", encoding="utf-8")
    (repository / "plan.md").write_text(
        json.dumps({"plan_sections": _PLAN_SECTIONS, "acceptance_criteria": _ACCEPTANCE_CRITERIA}, sort_keys=True),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "init")
    return repository, _git(repository, "rev-parse", "HEAD")


def _decomposition(base_commit: str) -> dict[str, Any]:
    return {
        "plan": "plan.md",
        "base_commit": base_commit,
        "plan_sections": _PLAN_SECTIONS,
        "acceptance_criteria": _ACCEPTANCE_CRITERIA,
        "runs": [
            {"id": "A", "objective": "Implement A.", "plan_sections": ["1"], "criteria": ["AC-1"], "depends_on": [], "allowed_paths": ["feature.txt"], "verification_argv": ["true"]},
            {"id": "B", "objective": "Implement B.", "plan_sections": ["1"], "criteria": ["AC-2"], "depends_on": ["A"], "allowed_paths": ["feature.txt"], "verification_argv": ["true"]},
        ],
    }


def _write_feature_run(
    run_dir: Path, run_id: str, *, plan_graph_id: str, plan_node_id: str, objective: str,
    base_commit: str, criteria_ids: tuple[str, ...], status: str = "succeeded",
    input_tokens: int = 100, output_tokens: int = 20,
) -> None:
    """Author one real, verified FeatureRun audit trail correlated to a node."""
    journal = AuditJournal(run_dir, run_id, actor=AuditActor("controller", "controller"))
    descriptor = {
        "protocol": "harness-run-descriptor/1", "run_kind": "feature_run", "run_id": run_id,
        "created_at": "2026-08-09T00:00:00Z", "objective": objective, "evidence_classification": "production_lifecycle",
        "repository": {"path": str(run_dir), "base_branch": "main", "base_commit": base_commit},
        "approved_plan": None,
        "parent_correlation": {"plan_graph_id": plan_graph_id, "plan_node_id": plan_node_id, "parent_run_id": plan_graph_id},
    }
    raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (journal.run_dir / "descriptor.json").write_bytes(raw)
    journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
    journal.append(
        "backend_transport", status="succeeded", attempt_id=f"implement-{plan_node_id}/attempt-1",
        actor=AuditActor(f"implement-{plan_node_id}/attempt-1", "semantic_worker"), backend_id="codex-exec", duration_ms=1000,
        payload={"model": "claude-sonnet-5", "reasoning": "high", "usage": {"input_tokens": input_tokens, "cached_input_tokens": 0, "output_tokens": output_tokens, "cost_usd": "0.010000"}},
    )
    criteria = {cid: {"id": cid, "status": "satisfied" if status == "succeeded" else "open"} for cid in criteria_ids}
    journal.finalize(status, result={"status": status}, state={"controller": {"criteria": criteria, "tasks": {}, "findings": {}}, "review_fix": {"cycles": 0}})


def _make_launcher(outcomes: Mapping[str, Mapping[str, Any]]):
    """A PlanGraph launcher callable; ``outcomes`` maps node_id -> overrides."""

    def launcher(request):
        plan = outcomes.get(request.plan_node_id, {})
        status = plan.get("status", "succeeded")
        if plan.get("write_journal", True):
            _write_feature_run(
                Path(request.run_dir), request.feature_run_id,
                plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
                objective=request.run.objective, base_commit=request.base_commit,
                criteria_ids=tuple(request.run.criteria), status=status,
                input_tokens=plan.get("input_tokens", 100), output_tokens=plan.get("output_tokens", 20),
            )
        return FeatureRunOutcome(
            status=status,
            candidate_commit=(plan.get("candidate_commit", request.base_commit) if status == "succeeded" else None),
            evidence=None if status == "succeeded" else {"error": plan.get("reason", "policy violation")},
            plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
            feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
        )

    return launcher


def _build_terminal_graph(root: Path, *, graph_attempt_id: str = "attempt-1", outcomes: Mapping[str, Mapping[str, Any]] | None = None):
    repository, base_commit = _init_repository(root)
    registration = register_plan_graph(repository=repository, logical_graph_id=graph_attempt_id, decomposition=_decomposition(base_commit))
    run_root = root / "runs"
    graph = PlanGraph(repository, registration, _make_launcher(outcomes or {}), run_root=run_root, graph_run_id=graph_attempt_id)
    result = graph.run()
    return repository, run_root, registration, result


def _registration_binding(graph_run_id: str) -> dict[str, str]:
    return {"logical_graph_id": graph_run_id, "registration_protocol": "plan-graph-registration/1", "registration_digest": "0" * 64, "graph_attempt_id": graph_run_id}


def _minimal_graph_audit(root: Path, graph_run_id: str, *, nodes: Mapping[str, Mapping[str, Any]] | None = None) -> PlanGraphAudit:
    """A minimal, real plan-graph audit trail without dispatching any node --
    for testing terminal-status gating, where node-level realism is not
    needed."""
    plan_path = root / "plan.md"
    if not plan_path.is_file():
        plan_path.write_text("plan text\n", encoding="utf-8")
    return PlanGraphAudit(
        repository=root, run_root=root, graph_run_id=graph_run_id, plan=str(plan_path),
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(), base_commit="a" * 40,
        registration_binding=_registration_binding(graph_run_id), objective="minimal fixture graph",
        nodes=nodes or {"A": {"status": "queued", "feature_run_id": None, "depends_on": []}},
        functionality_tests=(),
    )


def _module_launcher(request):
    """Referenced by dotted path (``--launcher``) from in-process CLI
    invocations of ``scripts/run_plan_graph.py``; always succeeds with a
    real, verified child audit trail."""
    _write_feature_run(
        Path(request.run_dir), request.feature_run_id,
        plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
        objective=request.run.objective, base_commit=request.base_commit,
        criteria_ids=tuple(request.run.criteria), status="succeeded",
    )
    return FeatureRunOutcome(
        status="succeeded", candidate_commit=request.base_commit, plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id, feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
    )


class _CliMixin:
    """Shared in-process CLI invocation with isolated stdio and env."""

    def _run_main(self, module, argv: list[str], *, env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
        old_argv = sys.argv
        old_environ = dict(os.environ)
        sys.argv = argv
        if env:
            os.environ.update(env)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = module.main()
        finally:
            sys.argv = old_argv
            os.environ.clear()
            os.environ.update(old_environ)
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# AC-DM03-1: schema-valid, structurally identical to the live rollup
# ---------------------------------------------------------------------------

class SnapshotContractTests(unittest.TestCase):
    def test_snapshot_validates_and_matches_live_rollup_for_a_terminal_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run_root, registration, result = _build_terminal_graph(root)
            self.assertEqual(result.status, "succeeded")

            snapshot = build_snapshot(run_root, "attempt-1", repository=repository)
            ClosedSchemaValidator(SCHEMA).validate(snapshot)

            catalog = build_run_catalog(run_root)
            graph = next(item for item in catalog["plan_graphs"] if item["run_id"] == "attempt-1")
            node_details = {
                run["run_id"]: build_run_detail(run_root, run["run_id"])["metrics"]
                for run in catalog["feature_runs"] if run.get("status") != "corrupt"
            }
            own = project_run_metrics(run_root / "attempt-1")
            ledger_path = next((run_root / ".plan-graph-budgets").glob("*.jsonl"))
            ledger = graph_metrics.read_budget_ledger(ledger_path)
            expected_metrics = graph_metrics.compute_graph_metrics(graph, catalog, node_details, own_summary=own["summary"], budget_ledger=ledger)

        # Same shared graph_metrics implementation, same catalog snapshot ->
        # the snapshot's graph_metrics block must be byte-for-byte identical
        # to an independently-recomputed live rollup, not merely similar.
        self.assertEqual(snapshot["graph_metrics"], expected_metrics)
        self.assertEqual(snapshot["identity"]["run_id"], "attempt-1")
        self.assertEqual(snapshot["identity"]["logical_graph_id"], "attempt-1")
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["graph_metrics"]["totals"]["tokens"]["total_tokens"], 240)
        self.assertEqual(snapshot["outcome"]["nodes_succeeded"], 2)
        self.assertFalse(snapshot["data_quality"]["criteria_text_unavailable"])
        self.assertEqual(snapshot["outcome"]["acceptance_criteria"], _ACCEPTANCE_CRITERIA)
        self.assertEqual(len(snapshot["feature_runs"]), 2)
        self.assertEqual({row["node_id"] for row in snapshot["feature_runs"]}, {"A", "B"})

    def test_schema_itself_is_closed(self) -> None:
        self.assertFalse(SCHEMA["additionalProperties"])
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")


# ---------------------------------------------------------------------------
# AC-DM03-2: builder/CLI idempotency, atomicity, terminal-only default,
# no writes inside run directories, --dry-run counts
# ---------------------------------------------------------------------------

class BuilderContractTests(unittest.TestCase):
    def test_refuses_a_non_terminal_graph_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _minimal_graph_audit(root, "live-graph")
            with self.assertRaises(SnapshotSkipped):
                build_snapshot(root, "live-graph")

    def test_interrupted_graph_requires_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = _minimal_graph_audit(root, "interrupted-graph")
            audit.journal.finalize("interrupted", result={"status": "interrupted"}, state=audit.journal.checkpoint_state())
            with self.assertRaises(SnapshotSkipped):
                build_snapshot(root, "interrupted-graph")
            snapshot = build_snapshot(root, "interrupted-graph", include_interrupted=True)
            self.assertEqual(snapshot["status"], "interrupted")

    def test_unknown_graph_is_skipped_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("runs").mkdir()
            with self.assertRaises(SnapshotSkipped):
                build_snapshot(root / "runs", "does-not-exist")


class WriteSnapshotContractTests(unittest.TestCase):
    def test_write_is_idempotent_atomic_and_never_touches_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run_root, registration, result = _build_terminal_graph(root)
            run_dir = run_root / "attempt-1"
            before = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))

            snapshot = build_snapshot(run_root, "attempt-1", repository=repository)
            target, wrote = write_snapshot(run_root, snapshot)
            self.assertTrue(wrote)
            self.assertEqual(target, run_root.resolve() / ".plan-graph-snapshots" / "attempt-1.json")
            self.assertTrue(target.is_file())

            # The write must never touch the run directory: dot-prefixed and
            # a sibling of it, exactly like .plan-graph-budgets.
            after = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))
            self.assertEqual(before, after)
            self.assertFalse(target.is_relative_to(run_dir))

            first_bytes = target.read_bytes()
            first_mtime = target.stat().st_mtime_ns

            # Idempotent: a second call without --force is a no-op.
            target2, wrote2 = write_snapshot(run_root, snapshot)
            self.assertFalse(wrote2)
            self.assertEqual(target2.read_bytes(), first_bytes)
            self.assertEqual(target2.stat().st_mtime_ns, first_mtime)

            # --force overwrites.
            target3, wrote3 = write_snapshot(run_root, snapshot, force=True)
            self.assertTrue(wrote3)

            # No leftover temp files from the atomic rename.
            leftovers = [item for item in (run_root / ".plan-graph-snapshots").iterdir() if item.name.startswith(".tmp-")]
            self.assertEqual(leftovers, [])

    def test_write_refuses_a_symlinked_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run_root, registration, result = _build_terminal_graph(root)
            snapshot = build_snapshot(run_root, "attempt-1", repository=repository)
            directory_path = run_root / ".plan-graph-snapshots"
            directory_path.mkdir(parents=True)
            decoy = root / "decoy.json"
            decoy.write_text("{}", encoding="utf-8")
            (directory_path / "attempt-1.json").symlink_to(decoy)
            with self.assertRaises(SnapshotSkipped):
                write_snapshot(run_root, snapshot)


class OfflineCliContractTests(unittest.TestCase, _CliMixin):
    def test_dry_run_reports_counts_without_writing_then_a_real_run_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run_root, registration, result = _build_terminal_graph(root)
            from scripts import build_plangraph_snapshot

            code, out, err = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(run_root), "--all-completed", "--repository", str(repository), "--dry-run"],
            )
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["reconstructed"], 1)
            self.assertEqual(report["skipped"], 0)
            self.assertEqual(report["failed"], 0)
            self.assertTrue(report["dry_run"])
            self.assertFalse((run_root / ".plan-graph-snapshots").exists(), "--dry-run must not write")

            code2, out2, err2 = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(run_root), "--all-completed", "--repository", str(repository)],
            )
            self.assertEqual(code2, 0, err2)
            report2 = json.loads(out2)
            self.assertEqual(report2["reconstructed"], 1)
            self.assertTrue((run_root / ".plan-graph-snapshots" / "attempt-1.json").is_file())

            # Idempotent CLI re-run: still reconstructed (successfully
            # processed), never failed, and the file is not rewritten.
            before_mtime = (run_root / ".plan-graph-snapshots" / "attempt-1.json").stat().st_mtime_ns
            code3, out3, err3 = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(run_root), "--all-completed", "--repository", str(repository)],
            )
            self.assertEqual(code3, 0, err3)
            self.assertEqual(json.loads(out3)["reconstructed"], 1)
            self.assertEqual((run_root / ".plan-graph-snapshots" / "attempt-1.json").stat().st_mtime_ns, before_mtime)

    def test_single_graph_target_and_include_interrupted_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = _minimal_graph_audit(root, "interrupted-graph")
            audit.journal.finalize("interrupted", result={"status": "interrupted"}, state=audit.journal.checkpoint_state())
            from scripts import build_plangraph_snapshot

            code, out, err = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(root), "--graph", "interrupted-graph", "--dry-run"],
            )
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["skipped"], 1)
            self.assertIn("interrupted", report["skipped_details"][0]["reason"])

            code2, out2, _ = self._run_main(
                build_plangraph_snapshot,
                ["build_plangraph_snapshot.py", "--run-root", str(root), "--graph", "interrupted-graph", "--include-interrupted"],
            )
            self.assertEqual(code2, 0)
            self.assertEqual(json.loads(out2)["reconstructed"], 1)
            self.assertTrue((root / ".plan-graph-snapshots" / "interrupted-graph.json").is_file())


# ---------------------------------------------------------------------------
# AC-DM03-3: runner + recovery-coordinator best-effort emission; failures
# are warnings that never alter run status or journals
# ---------------------------------------------------------------------------

class RunnerEmissionTests(unittest.TestCase, _CliMixin):
    def _register(self, repository: Path, base_commit: str, root: Path, *, automatic_recovery=None):
        registration = register_plan_graph(repository=repository, logical_graph_id="attempt-1", decomposition=_decomposition(base_commit), automatic_recovery=automatic_recovery)
        registration_path = persist_registration(repository=repository, registration_root=root / "registrations", registration=registration)
        return registration, registration_path

    def test_terminal_run_through_the_cli_registers_its_root_and_emits_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_commit = _init_repository(root)
            registration, registration_path = self._register(repository, base_commit, root)
            run_root = root / "runs"
            registry_path = root / "registry.json"
            from scripts import run_plan_graph

            code, out, err = self._run_main(
                run_plan_graph,
                [
                    "run_plan_graph.py", "run", "--repository", str(repository), "--registration", str(registration_path),
                    "--graph-attempt-id", "attempt-1", "--launcher", "tests.test_plangraph_snapshot:_module_launcher",
                    "--run-root", str(run_root),
                ],
                env={"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(registry_path)},
            )
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual(result["status"], "succeeded")

            # Run-root self-registration (AC-DM03-5 wiring exercised together
            # with emission here; AC-DM03-5's own tests cover its contract
            # in isolation below).
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["protocol"], "harness-dashboard-audit-root-registry/1")
            self.assertEqual(registry["audit_roots"], [str(run_root.resolve())])

            # Best-effort snapshot emission after terminal finalization.
            snapshot_file = run_root / ".plan-graph-snapshots" / "attempt-1.json"
            self.assertTrue(snapshot_file.is_file())
            snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
            ClosedSchemaValidator(SCHEMA).validate(snapshot)
            self.assertEqual(snapshot["status"], "succeeded")

    def test_snapshot_emission_failure_is_a_warning_and_never_alters_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_commit = _init_repository(root)
            registration, registration_path = self._register(repository, base_commit, root)
            run_root = root / "runs"
            run_root.mkdir(parents=True)
            # Occupy the snapshot directory's path with a plain file so the
            # write step's mkdir fails.
            (run_root / ".plan-graph-snapshots").write_text("occupied", encoding="utf-8")
            registry_path = root / "registry.json"
            from scripts import run_plan_graph

            code, out, err = self._run_main(
                run_plan_graph,
                [
                    "run_plan_graph.py", "run", "--repository", str(repository), "--registration", str(registration_path),
                    "--graph-attempt-id", "attempt-1", "--launcher", "tests.test_plangraph_snapshot:_module_launcher",
                    "--run-root", str(run_root),
                ],
                env={"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(registry_path)},
            )
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual(result["status"], "succeeded")
            self.assertIn("metrics-snapshot", err)
            self.assertIn("continuing", err)
            # The run's own journal is untouched by the emission failure.
            self.assertTrue((run_root / "attempt-1" / "checkpoint.json").is_file())
            manifest = json.loads((run_root / "attempt-1" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "succeeded")

    def test_registration_failure_is_a_warning_and_the_run_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_commit = _init_repository(root)
            registration, registration_path = self._register(repository, base_commit, root)
            run_root = root / "runs"
            # A registry path whose parent is a plain file: mkdir(parents=True) fails.
            blocker = root / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            bad_registry = blocker / "sub" / "registry.json"
            from scripts import run_plan_graph

            code, out, err = self._run_main(
                run_plan_graph,
                [
                    "run_plan_graph.py", "run", "--repository", str(repository), "--registration", str(registration_path),
                    "--graph-attempt-id", "attempt-1", "--launcher", "tests.test_plangraph_snapshot:_module_launcher",
                    "--run-root", str(run_root),
                ],
                env={"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(bad_registry)},
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["status"], "succeeded")
            self.assertIn("registration failed", err)
            self.assertIn("continuing", err)
            self.assertFalse(bad_registry.exists())


class RecoveryEmissionTests(unittest.TestCase, _CliMixin):
    def test_recovery_finalization_emits_a_snapshot_for_the_escalated_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_commit = _init_repository(root)
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["resume", "extend_budget"], "max_extra_node_launches": 1, "max_structural_decisions": 0}
            registration = register_plan_graph(repository=repository, logical_graph_id="logical", decomposition=_decomposition(base_commit), automatic_recovery=authority)
            registration_path = persist_registration(repository=repository, registration_root=root / "registrations", registration=registration)
            run_root = root / "runs"

            # Node A blocks; no child audit trail is written (mirrors a
            # genuine policy-violation block: the predecessor is terminal,
            # but the runner's own emission hook was not exercised here, so
            # the recovery coordinator's hook is what must produce the file).
            blocked_launcher = _make_launcher({"A": {"status": "blocked", "write_journal": False, "reason": "permission denied"}})
            graph = PlanGraph(repository, registration, blocked_launcher, run_root=run_root, graph_run_id="attempt")
            result = graph.run()
            self.assertEqual(result.status, "blocked")
            self.assertFalse((run_root / ".plan-graph-snapshots").exists())

            escalation = {
                "protocol": "plan-graph-block-escalation/1", "graph_run_id": "attempt", "logical_graph_id": "logical",
                "blocked_node_id": "A", "status_flags": {}, "nodes": [{"node_id": "A", "classification": "product", "reason": "permission denied"}],
                "budget_state": {}, "significance_guidance": _ACCEPTANCE_CRITERIA,
                "resume_directive_template": {"logical_graph_id": "logical", "predecessor_attempt_id": "attempt", "retry_frontier": ["A"]},
            }
            escalation_path = root / "escalation.json"
            escalation_path.write_text(json.dumps(escalation), encoding="utf-8")

            from scripts import plan_graph_recover

            code, out, err = self._run_main(
                plan_graph_recover,
                [
                    "plan_graph_recover.py", str(escalation_path), "--repository", str(repository),
                    "--registration", str(registration_path), "--run-root", str(run_root), "--launcher-command", "echo",
                ],
            )
            # "permission denied" classifies as a policy violation: a
            # human-tier decision the coordinator refuses to make -- no
            # resume subprocess is spawned, so the file below can only have
            # come from the recovery coordinator's own emission hook.
            self.assertEqual(code, 1)
            coordinator_result = json.loads(out.strip())
            self.assertEqual(coordinator_result["status"], "requires_human")

            snapshot_file = run_root / ".plan-graph-snapshots" / "attempt.json"
            self.assertTrue(snapshot_file.is_file())
            snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["status"], "blocked")
            self.assertEqual(snapshot["outcome"]["nodes_blocked"], 1)

    def test_recovery_snapshot_failure_never_masks_the_coordinator_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, base_commit = _init_repository(root)
            authority = {"protocol": "plan-graph-automatic-recovery/1", "allowed_actions": ["resume"], "max_extra_node_launches": 1, "max_structural_decisions": 0}
            registration = register_plan_graph(repository=repository, logical_graph_id="logical", decomposition=_decomposition(base_commit), automatic_recovery=authority)
            registration_path = persist_registration(repository=repository, registration_root=root / "registrations", registration=registration)
            run_root = root / "runs"
            blocked_launcher = _make_launcher({"A": {"status": "blocked", "write_journal": False, "reason": "permission denied"}})
            PlanGraph(repository, registration, blocked_launcher, run_root=run_root, graph_run_id="attempt").run()
            # Occupy the snapshot directory's path so the emission hook fails.
            (run_root / ".plan-graph-snapshots").write_text("occupied", encoding="utf-8")

            escalation = {
                "protocol": "plan-graph-block-escalation/1", "graph_run_id": "attempt", "logical_graph_id": "logical",
                "blocked_node_id": "A", "status_flags": {}, "nodes": [{"node_id": "A", "classification": "product", "reason": "permission denied"}],
                "budget_state": {}, "significance_guidance": _ACCEPTANCE_CRITERIA,
                "resume_directive_template": {"logical_graph_id": "logical", "predecessor_attempt_id": "attempt", "retry_frontier": ["A"]},
            }
            escalation_path = root / "escalation.json"
            escalation_path.write_text(json.dumps(escalation), encoding="utf-8")

            from scripts import plan_graph_recover

            code, out, err = self._run_main(
                plan_graph_recover,
                [
                    "plan_graph_recover.py", str(escalation_path), "--repository", str(repository),
                    "--registration", str(registration_path), "--run-root", str(run_root), "--launcher-command", "echo",
                ],
            )
            self.assertEqual(code, 1)
            # stdout still carries exactly the coordinator's own result --
            # the emission failure is reported on stderr only, never mixed
            # into (or replacing) the coordinator's own JSON output.
            coordinator_result = json.loads(out.strip())
            self.assertEqual(coordinator_result["status"], "requires_human")
            self.assertIn("continuing", err)


# ---------------------------------------------------------------------------
# AC-DM03-4: outcome block (per-node criteria/evidence, graph-level counts,
# git-derived delta, digest-checked criteria text, templated narrative)
# ---------------------------------------------------------------------------

class OutcomeTests(unittest.TestCase):
    def test_outcome_reports_per_node_and_graph_level_facts_and_a_grounded_narrative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcomes = {"B": {"status": "blocked", "write_journal": False, "reason": "assertion failed"}}
            repository, run_root, registration, result = _build_terminal_graph(root, outcomes=outcomes)
            self.assertEqual(result.status, "blocked")
            snapshot = build_snapshot(run_root, "attempt-1", repository=repository)

        outcome = snapshot["outcome"]
        self.assertEqual(outcome["nodes_total"], 2)
        self.assertEqual(outcome["nodes_attempted"], 2)
        self.assertEqual(outcome["nodes_succeeded"], 1)
        self.assertEqual(outcome["nodes_blocked"], 1)
        self.assertEqual(outcome["nodes_failed"], 0)

        rows = {row["node_id"]: row for row in outcome["nodes"]}
        self.assertEqual(rows["A"]["status"], "succeeded")
        self.assertEqual(rows["A"]["criteria_satisfied"], 1)
        self.assertEqual(rows["A"]["criteria_total"], 1)
        self.assertIsNone(rows["A"]["evidence_reason"])
        self.assertEqual(rows["B"]["status"], "blocked")
        self.assertIsNotNone(rows["B"]["evidence_reason"])

        # Delta: base==final commit (no code was actually integrated for the
        # blocked node), so a genuine, verified zero-change diff -- not a
        # degrade.
        delta = outcome["delta"]
        self.assertEqual(delta["state"], "available")
        self.assertEqual(delta["files_changed"], 0)
        self.assertEqual({row["node_id"] for row in delta["nodes"]}, {"A", "B"})

        self.assertEqual(outcome["acceptance_criteria"], _ACCEPTANCE_CRITERIA)

        # The narrative is templated: every fact it states is already present
        # verbatim elsewhere in the document.
        narrative = outcome["narrative"]
        self.assertIn(snapshot["display_name"], narrative)
        self.assertIn(snapshot["status"], narrative)
        self.assertIn(f"{outcome['nodes_succeeded']} of {outcome['nodes_total']}", narrative)
        self.assertIn(f"{outcome['nodes_blocked']} blocked", narrative)

    def test_criteria_text_available_only_when_the_recorded_digest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run_root, registration, result = _build_terminal_graph(root)
            snapshot = build_snapshot(run_root, "attempt-1", repository=repository)
            self.assertFalse(snapshot["data_quality"]["criteria_text_unavailable"])
            self.assertEqual(snapshot["outcome"]["acceptance_criteria"], _ACCEPTANCE_CRITERIA)
            self.assertEqual(snapshot["outcome"]["plan_sections"], _PLAN_SECTIONS)

            # Tamper with plan.md after the digest was recorded: the digest
            # check must now fail closed, never serving stale/mismatched text.
            (repository / "plan.md").write_text(json.dumps({"plan_sections": {}, "acceptance_criteria": {"AC-9": "tampered"}}), encoding="utf-8")
            tampered = build_snapshot(run_root, "attempt-1", repository=repository)
            self.assertTrue(tampered["data_quality"]["criteria_text_unavailable"])
            self.assertIsNone(tampered["outcome"]["acceptance_criteria"])
            self.assertTrue(any("digest" in note for note in tampered["data_quality"]["reconstruction_notes"]))

    def test_delta_and_criteria_text_are_unavailable_without_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run_root, registration, result = _build_terminal_graph(root)
            snapshot = build_snapshot(run_root, "attempt-1", repository=None)

        self.assertEqual(snapshot["outcome"]["delta"]["state"], "unavailable")
        self.assertIn("repository", snapshot["outcome"]["delta"]["reason"])
        self.assertTrue(snapshot["data_quality"]["criteria_text_unavailable"])
        self.assertIsNone(snapshot["outcome"]["acceptance_criteria"])
        # Per-node candidate commits are still reported: they need no git access.
        self.assertTrue(all(row["candidate_commit"] for row in snapshot["outcome"]["delta"]["nodes"]))


# ---------------------------------------------------------------------------
# AC-DM03-5: run-root self-registration (atomic, deduplicated, pruned,
# best-effort)
# ---------------------------------------------------------------------------

class RunRootRegistrationTests(unittest.TestCase, _CliMixin):
    def _run_once(self, root: Path, run_root: Path, registry_path: Path, graph_attempt_id: str) -> tuple[int, str, str]:
        (root / graph_attempt_id).mkdir(parents=True, exist_ok=True)
        repository, base_commit = _init_repository(root / graph_attempt_id)
        registration = register_plan_graph(repository=repository, logical_graph_id=graph_attempt_id, decomposition=_decomposition(base_commit))
        registration_path = persist_registration(repository=repository, registration_root=root / graph_attempt_id / "registrations", registration=registration)
        from scripts import run_plan_graph
        return self._run_main(
            run_plan_graph,
            [
                "run_plan_graph.py", "run", "--repository", str(repository), "--registration", str(registration_path),
                "--graph-attempt-id", graph_attempt_id, "--launcher", "tests.test_plangraph_snapshot:_module_launcher",
                "--run-root", str(run_root),
            ],
            env={"HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY": str(registry_path)},
        )

    def test_registers_atomically_deduplicates_and_prunes_stale_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            stale_root = root / "stale-run-root"  # never created: pruned on sight

            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(json.dumps({"protocol": "harness-dashboard-audit-root-registry/1", "audit_roots": [str(stale_root)]}), encoding="utf-8")

            run_root_one = root / "graph-1" / "runs"
            code, out, err = self._run_once(root, run_root_one, registry_path, "graph-1")
            self.assertEqual(code, 0, err)

            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["protocol"], "harness-dashboard-audit-root-registry/1")
            # The stale entry (directory never existed) was pruned; the new
            # root was added.
            self.assertEqual(registry["audit_roots"], [str(run_root_one.resolve())])

            # A second graph launched against a different run root: both
            # roots are present, most-recent first, no duplicates.
            run_root_two = root / "graph-2" / "runs"
            code2, out2, err2 = self._run_once(root, run_root_two, registry_path, "graph-2")
            self.assertEqual(code2, 0, err2)
            registry2 = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry2["audit_roots"][0], str(run_root_two.resolve()))
            self.assertEqual(set(registry2["audit_roots"]), {str(run_root_one.resolve()), str(run_root_two.resolve())})

            # Re-registering the same run root deduplicates rather than
            # appending a second copy.
            code3, out3, err3 = self._run_once(root, run_root_one, registry_path, "graph-1-again")
            self.assertEqual(code3, 0, err3)
            registry3 = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(registry3["audit_roots"]), sorted({str(run_root_one.resolve()), str(run_root_two.resolve())}))
            self.assertEqual(len(registry3["audit_roots"]), 2)

    def test_registration_cap_matches_the_dashboard_servers_bound(self) -> None:
        from harness_labs.observability.dashboard_server import MAX_AUDIT_ROOTS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            existing_roots = [str(root / f"pre-existing-{index}") for index in range(MAX_AUDIT_ROOTS)]
            for entry in existing_roots:
                Path(entry).mkdir(parents=True)
            registry_path.write_text(json.dumps({"protocol": "harness-dashboard-audit-root-registry/1", "audit_roots": existing_roots}), encoding="utf-8")

            run_root = root / "graph-1" / "runs"
            code, out, err = self._run_once(root, run_root, registry_path, "graph-1")
            self.assertEqual(code, 0, err)
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(registry["audit_roots"]), MAX_AUDIT_ROOTS)
            self.assertEqual(registry["audit_roots"][0], str(run_root.resolve()))


if __name__ == "__main__":
    unittest.main()
