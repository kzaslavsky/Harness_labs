from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness_labs.audit import AuditActor, AuditJournal
from harness_labs.plan_graph_audit import PlanGraphAudit
from harness_labs.run_catalog import _detail_metrics, _graph_execution, _snapshot, build_run_catalog, build_run_detail


def _registration_binding(graph_run_id: str) -> dict[str, str]:
    return {"logical_graph_id": graph_run_id, "registration_protocol": "plan-graph-registration/1",
            "registration_digest": "0" * 64, "graph_attempt_id": graph_run_id}


class RunCatalogTests(unittest.TestCase):
    def _run(self, root: Path, run_id: str, *, terminal: bool = False) -> Path:
        journal = AuditJournal(root / run_id, run_id, actor=AuditActor("a", "r"))
        descriptor = {"protocol": "harness-run-descriptor/1", "run_kind": "feature_run", "run_id": run_id, "created_at": "2026-08-09T00:00:00Z", "objective": "test", "evidence_classification": "production_lifecycle", "repository": {"path": "/repo", "base_branch": "main", "base_commit": "a" * 40}, "approved_plan": None, "parent_correlation": None}
        raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (journal.run_dir / "descriptor.json").write_bytes(raw)
        journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
        journal.checkpoint("running", journal.checkpoint_state())
        if terminal:
            journal.finalize("succeeded", result={"status": "succeeded"})
        return journal.run_dir

    def test_terminal_legacy_and_corrupt_peers_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "done", terminal=True)
            (self._run(root, "old") / "descriptor.json").unlink()
            (root / "bad").mkdir()
            snapshot = build_run_catalog(root)
        records = {record["run_id"]: record for record in snapshot["feature_runs"]}
        self.assertEqual(records["done"]["liveness"]["state"], "terminal")
        self.assertEqual(records["old"]["kind"], "legacy_feature_run")
        self.assertEqual(snapshot["diagnostics"][0]["run_id"], "bad")

    def test_live_requires_fresh_same_process_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "active")
            now = datetime(2026, 8, 9, tzinfo=timezone.utc)
            lease = {"protocol": "harness-controller-liveness/1", "run_id": "active", "controller_instance_id": "instance", "hostname": socket.gethostname(), "pid": 7, "process_start_token": "token", "heartbeat_sequence": 1, "heartbeat_at": now.isoformat(), "controller_kind": "feature_run"}
            (run / "liveness.json").write_text(json.dumps(lease))
            live = build_run_catalog(root, clock=lambda: now, process_probe=lambda pid: "token")
            stale = build_run_catalog(root, clock=lambda: now + timedelta(seconds=31), process_probe=lambda pid: "token")
        self.assertEqual(live["feature_runs"][0]["liveness"]["state"], "live")
        self.assertEqual(stale["feature_runs"][0]["liveness"]["state"], "stale")

    def test_detail_exposes_inspector_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "detail")
            journal = AuditJournal.open_existing(run, actor=AuditActor("a", "r"))
            journal.checkpoint("running", {"controller": {"criteria": ["AC-04"], "tasks": ["task"], "findings": ["finding"], "decisions": ["decision"]}})
            detail = build_run_detail(root, "detail")
        self.assertEqual(detail["criteria"], ["AC-04"])
        self.assertEqual(detail["tasks"], ["task"])
        self.assertIn("git_custody", detail)

    def test_detail_preserves_recorded_empty_evidence_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "empty")
            journal = AuditJournal.open_existing(run, actor=AuditActor("a", "r"))
            journal.checkpoint("running", {"controller": {"criteria": [], "tasks": [], "findings": [], "decisions": []}})
            detail = build_run_detail(root, "empty")
        self.assertEqual(detail["availability"]["criteria"]["state"], "available")
        self.assertEqual(detail["availability"]["findings"]["state"], "available")

    def test_detail_projects_reconciled_usage_breakdowns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "metrics")
            journal = AuditJournal.open_existing(run, actor=AuditActor("controller", "controller"))
            usage = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 25, "cost_usd": "0.125000", "model": "gpt-test"}
            journal.append(
                "backend_transport", status="succeeded", attempt_id="implement-metrics/attempt-1",
                actor=AuditActor("implement-metrics/attempt-1", "semantic_worker"), backend_id="codex-exec", duration_ms=2_000,
                payload={"model": "gpt-test", "reasoning": "high", "usage": usage},
            )
            journal.checkpoint("running", {"controller": {"criteria": {"AC-1": {"status": "satisfied"}}, "findings": {}}, "review_fix": {"cycles": 1}})
            detail = build_run_detail(root, "metrics")
        projected = detail["metrics"]
        self.assertEqual(projected["totals"]["total_tokens"], 125)
        self.assertEqual(projected["totals"]["cached_input_tokens"], 40)
        self.assertEqual(projected["totals"]["cost"]["usd"], 0.125)
        self.assertEqual(projected["by_phase"][0]["label"], "implement")
        self.assertEqual(projected["by_agent"][0]["peak_input_tokens"], 100)
        self.assertEqual(projected["by_model"][0]["label"], "gpt-test")
        self.assertEqual(projected["by_effort"][0]["label"], "high")
        self.assertEqual(projected["quality"]["criteria_satisfied"], 1)

    def test_detail_projects_verified_codex_cumulative_token_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "codex-usage")
            journal = AuditJournal.open_existing(run, actor=AuditActor("controller", "controller"))
            journal.append("backend_process_started", status="started", backend_id="codex-app-server", payload={"model": "gpt-5.6-terra", "reasoning": "medium"})
            artifact = journal.write_artifact("codex-app-server-inbound", {
                "method": "thread/tokenUsage/updated",
                "params": {"tokenUsage": {
                    "total": {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 25, "totalTokens": 125},
                    "last": {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 25, "totalTokens": 125},
                }},
            }, media_type="application/x-ndjson")
            journal.append("transport_message", status="received", backend_id="codex-app-server", artifacts=(artifact,), payload={"direction": "inbound", "method": "thread/tokenUsage/updated", "request_id": None})
            journal.checkpoint("running", {"controller": {"criteria": {}, "tasks": {}, "findings": {}}})
            detail = build_run_detail(root, "codex-usage")
        projected = detail["metrics"]
        self.assertEqual(projected["totals"]["total_tokens"], 125)
        self.assertEqual(projected["totals"]["cached_input_tokens"], 40)
        self.assertEqual(projected["totals"]["calls"], 1)
        self.assertEqual(projected["totals"]["peak_input_tokens"], 100)
        self.assertEqual(projected["by_model"][0]["label"], "gpt-5.6-terra")
        self.assertEqual(projected["provenance"]["collection_method"], "verified cumulative Codex token-usage notifications")

    def test_detail_infers_api_equivalent_cost_with_long_context_pricing(self) -> None:
        metrics = _detail_metrics({
            "events": [{
                "event_type": "backend_transport", "attempt_id": "implement-cost/attempt-1",
                "actor": {"id": "worker", "role": "semantic_worker"}, "backend_id": "codex-exec", "duration_ms": 1,
                "payload": {"model": "gpt-5.6-terra", "reasoning": "medium", "usage": {
                    "input_tokens": 300_000, "cached_input_tokens": 100_000, "output_tokens": 20_000, "cost_usd": None,
                }},
            }],
            "checkpoint": {"state": {}}, "summary": None,
        })
        cost = metrics["totals"]["cost"]
        self.assertEqual(cost["state"], "estimated")
        self.assertEqual(cost["usd"], 1.5)
        self.assertEqual(cost["long_context_records"], 1)
        self.assertIn("gpt-5.6-terra", cost["sources"][0])

    def test_detail_projects_recorded_execution_stages_without_usage(self) -> None:
        metrics = _detail_metrics({
            "events": [{
                "event_type": "controller_event", "status": "succeeded", "monotonic_ns": 1_000_000,
                "payload": {"controller_event": {"event_type": "coordinator.session_started", "payload": {"session_id": "session-1", "segment_id": "build", "attempt": 1, "starting_phase": "implement", "backend_id": "coordinator"}}},
            }, {
                "event_type": "controller_event", "status": "succeeded", "monotonic_ns": 6_000_000,
                "payload": {"controller_event": {"event_type": "coordinator.session_ended", "payload": {"session_id": "session-1", "segment_id": "build", "attempt": 1, "ending_phase": "implement", "result_status": "succeeded", "backend_id": "coordinator"}}},
            }, {
                "event_type": "deterministic_verification_completed", "status": "succeeded", "payload": {"stage": "post_implementation", "attempt": 1, "duration_ms": 20, "exit_code": 0, "timed_out": False},
            }],
            "checkpoint": {"state": {"controller": {"tasks": {"implement-one": {"attempt_id": "implement-one/attempt-1", "role": "semantic_worker", "status": "succeeded"}}}}},
            "summary": None,
        })
        self.assertEqual([stage["phase"] for stage in metrics["stages"]], ["implement", "verify", "implement"])
        self.assertEqual(metrics["stages"][0]["duration_ms"], 5)
        self.assertEqual(metrics["stages"][1]["model"], "not applicable")

    def test_invalid_descriptor_and_unmatched_correlation_are_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = self._run(root, "child")
            descriptor = json.loads((child / "descriptor.json").read_text())
            descriptor["repository"]["base_commit"] = "A" * 40
            raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            (child / "descriptor.json").write_bytes(raw)
            snapshot = build_run_catalog(root)
        self.assertEqual(snapshot["feature_runs"][0]["status"], "corrupt")

    def test_ungrouped_requires_the_full_graph_node_child_correlation(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {"run_id": "graph", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "nodes": [{"node_id": "node", "status": "running", "feature_run_id": "child", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}}]},
            {"run_id": "child", "kind": "feature_run", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "node", "parent_run_id": "other-parent"}},
        ]
        snapshot = _snapshot(Path("/runs"), now, [], records)
        self.assertEqual([record["run_id"] for record in snapshot["ungrouped_feature_runs"]], ["child"])

    def test_legacy_graph_node_recovers_unique_child_by_audited_merge_commit(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {"run_id": "graph", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "available", "reason": None}, "nodes": [{"node_id": "node", "status": "succeeded", "feature_run_id": "legacy-reservation", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}, "_candidate_commit": "b" * 40}]},
            {"run_id": "timestamped-child", "kind": "legacy_feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "partial", "reason": "descriptor absent"}, "correlation": None, "_integration_merge_commits": ("b" * 40,)},
        ]

        snapshot = _snapshot(Path("/runs"), now, [], records)

        node = snapshot["plan_graphs"][0]["nodes"][0]
        self.assertEqual(node["feature_run_id"], "timestamped-child")
        self.assertEqual(node["evidence"]["state"], "partial")
        self.assertNotIn("_candidate_commit", node)
        self.assertNotIn("_integration_merge_commits", snapshot["feature_runs"][0])

    def test_plan_graph_projection_preserves_logical_identity_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            PlanGraphAudit(
                repository=root,
                run_root=root,
                graph_run_id="graph-attempt",
                plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40,
                registration_binding={
                    "logical_graph_id": "graph",
                    "registration_protocol": "plan-graph-registration/1",
                    "registration_digest": "b" * 64,
                    "graph_attempt_id": "graph-attempt",
                },
                objective="test graph",
                nodes={
                    "root": {"status": "queued", "feature_run_id": "child-root", "depends_on": []},
                    "join": {"status": "queued", "feature_run_id": "child-join", "depends_on": ["root"]},
                },
                functionality_tests=(),
            )
            graph = build_run_catalog(root)["plan_graphs"][0]

        self.assertEqual(graph["plan_path"], str(plan))
        self.assertEqual(len(graph["plan_digest"]), 64)
        self.assertEqual(len(graph["plan_graph_digest"]), 64)
        self.assertEqual([node["node_id"] for node in graph["nodes"]], ["root", "join"])
        self.assertEqual(graph["nodes"][1]["depends_on"], ["root"])

    def test_catalog_accepts_closed_plan_graph_lineage_and_rejects_malformed_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            lineage = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="lineage-graph", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("lineage-graph"),
                objective="lineage graph", nodes={}, functionality_tests=(),
            )
            descriptor_path = lineage.journal.run_dir / "descriptor.json"
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            descriptor.update({
                "logical_graph_id": "logical-graph",
                "graph_attempt_id": "lineage-attempt-2",
                "predecessor_attempt_id": "lineage-attempt-1",
            })
            raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            descriptor_path.write_bytes(raw)
            lineage.journal.append(
                "run_descriptor_bound", status="succeeded",
                payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()},
            )
            lineage.journal.checkpoint("running", lineage.state)
            malformed = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="malformed-graph", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("malformed-graph"),
                objective="malformed lineage graph", nodes={}, functionality_tests=(),
            )
            descriptor_path = malformed.journal.run_dir / "descriptor.json"
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            descriptor["logical_graph_id"] = "not/path-safe"
            raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            descriptor_path.write_bytes(raw)
            malformed.journal.append(
                "run_descriptor_bound", status="succeeded",
                payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()},
            )
            malformed.journal.checkpoint("running", malformed.state)
            unknown = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="unknown-field-graph", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("unknown-field-graph"),
                objective="unknown descriptor field", nodes={}, functionality_tests=(),
            )
            descriptor_path = unknown.journal.run_dir / "descriptor.json"
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            descriptor["unexpected"] = "not schema-authorized"
            raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            descriptor_path.write_bytes(raw)
            unknown.journal.append(
                "run_descriptor_bound", status="succeeded",
                payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()},
            )
            unknown.journal.checkpoint("running", unknown.state)
            snapshot = build_run_catalog(root)

        self.assertEqual([graph["run_id"] for graph in snapshot["plan_graphs"]], ["lineage-graph"])
        graph = snapshot["plan_graphs"][0]
        self.assertEqual(graph["logical_graph_id"], "logical-graph")
        self.assertEqual(graph["graph_attempt_id"], "lineage-attempt-2")
        self.assertEqual(graph["predecessor_attempt_id"], "lineage-attempt-1")
        self.assertEqual(graph["retention_constraints"], {
            "state": "unavailable",
            "reason": "retention constraints were not recorded in the audited descriptor or checkpoint",
        })
        diagnostics = {item["run_id"]: item for item in snapshot["diagnostics"]}
        self.assertEqual(diagnostics["malformed-graph"]["code"], "corrupt_run")
        self.assertIn("lineage", diagnostics["malformed-graph"]["message"])
        self.assertEqual(diagnostics["unknown-field-graph"]["code"], "corrupt_run")
        self.assertIn("does not bind", diagnostics["unknown-field-graph"]["message"])

    def test_plan_graph_projection_exposes_recorded_attempts_without_fabricating_parallel_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            audit = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-attempt", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-attempt"),
                objective="test graph",
                nodes={"lane": {"status": "queued", "feature_run_id": "child", "depends_on": []}},
                functionality_tests=(),
            )
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="lane", logical_attempt=1, allocation_id="allocation-lane",
                parent_candidate_commit="a" * 40, expected_revision=checkpoint["revision"],
                expected_staging_head="a" * 40,
            )
            graph = build_run_catalog(root)["plan_graphs"][0]

        execution = graph["execution"]
        self.assertEqual(execution["logical_graph"]["base_commit"], "a" * 40)
        self.assertEqual(execution["concurrency"]["active_nodes"], ["lane"])
        self.assertEqual(execution["concurrency"]["active_count"], 1)
        self.assertEqual(execution["concurrency"]["max_parallelism"]["state"], "unavailable")
        self.assertEqual(execution["integration"]["staging_head"], "a" * 40)
        self.assertEqual(execution["integration"]["lease"]["state"], "unavailable")
        self.assertIsNone(execution["integration"]["lease_record"])
        self.assertEqual(execution["integration"]["barriers"], [{
            "barrier_id": "lane:integration:allocation-lane", "node_id": "lane",
            "attempt_id": "graph-attempt:attempt:allocation-lane", "allocation_id": None,
            "logical_attempt": None, "checkpoint_revision": None, "lease_id": None,
            "action": None, "input_commit": "a" * 40, "expected_staging_head": "a" * 40,
            "integrated_commit": None, "evidence_refs": [],
        }])
        self.assertEqual(execution["recovery"]["attempt_lineage"], [{
            "attempt_id": "graph-attempt:attempt:allocation-lane", "node_id": "lane",
            "logical_attempt": 1, "allocation_id": "allocation-lane", "input_commit": "a" * 40,
            "predecessor_attempt_id": None,
        }])
        self.assertEqual(execution["recovery"]["retry_state"], {"invalidations": [], "reuse": []})
        self.assertEqual(execution["attempts"], [{
            "node_id": "lane", "logical_attempt": 1, "allocation_id": "allocation-lane",
            "checkpoint_revision": checkpoint["revision"], "parent_candidate_commit": "a" * 40,
            "expected_staging_head": "a" * 40, "status": "reserved", "candidate_commit": None,
        }])
        self.assertEqual(graph["nodes"][0]["liveness"]["state"], "liveness_unavailable")

    def test_plan_graph_projection_retains_recovery_dispositions_and_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            audit = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-recovery", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-recovery"),
                objective="test graph",
                nodes={"blocked": {"status": "blocked", "feature_run_id": "child-blocked", "depends_on": [], "evidence": {"reason": "force_reconcile"}}},
                functionality_tests=(),
            )
            audit.journal.append("plan_graph_child_recovery_blocked", status="blocked", payload={"plan_node_id": "blocked", "reason": "force_reconcile", "evidence_ref": f"artifact:sha256:{'b' * 64}"})
            audit.journal.append("plan_graph_child_seal_adopted", status="succeeded", payload={"plan_node_id": "sealed", "forced": True, "force_evidence_ref": f"artifact:sha256:{'c' * 64}", "seal_receipt": {"canonical_manifest_ref": f"artifact:sha256:{'d' * 64}", "verification_evidence_ref": f"artifact:sha256:{'e' * 64}"}})
            audit.journal.checkpoint("running", audit.state)
            graph = build_run_catalog(root)["plan_graphs"][0]

        self.assertEqual(graph["nodes"][0]["evidence"], {"state": "partial", "reason": "force_reconcile"})
        self.assertEqual(graph["execution"]["recovery"]["dispositions"], [
            {"node_id": "blocked", "disposition": "blocked", "reason": "force_reconcile", "forced": True, "evidence_refs": [f"artifact:sha256:{'b' * 64}"]},
            {"node_id": "sealed", "disposition": "sealed", "reason": None, "forced": True, "evidence_refs": sorted([f"artifact:sha256:{'c' * 64}", f"artifact:sha256:{'d' * 64}", f"artifact:sha256:{'e' * 64}"])},
        ])

    def test_execution_projection_exposes_active_lease_barrier_and_retry_lineage(self) -> None:
        state = {
            "nodes": {"join": {"status": "reserved"}}, "active_node_ids": ["join"],
            "current_candidate_commit": "a" * 40,
            "integration_lease": {"node_id": "join", "lease_id": "lease-join", "expected_staging_head": "a" * 40},
            "integration_barriers": [{"node_id": "join", "attempt_id": "graph:attempt:alloc-join", "allocation_id": "alloc-join", "logical_attempt": 2, "checkpoint_revision": 7, "lease_id": "lease-join", "action": "lease_acquired", "receipt_ref": f"artifact:sha256:{'b' * 64}"}],
            "attempt_lineage": [{"attempt_id": "graph:attempt:alloc-join", "node_id": "join", "logical_attempt": 2, "allocation_id": "alloc-join", "input_commit": "a" * 40, "predecessor_attempt_id": "graph:attempt:old"}],
            "retry_state": {"invalidations": [{"attempt_id": "graph:attempt:old", "node_id": "join", "allocation_id": "old", "reason": "stopped", "invalidated_at": "2026-08-11T00:00:00Z"}], "reuse": [{"node_id": "join", "reused_from_attempt_id": "graph:attempt:old", "replacement_attempt_id": "graph:attempt:alloc-join"}]},
        }
        execution = _graph_execution({"checkpoint": {"state": state}, "events": []})
        self.assertEqual(execution["integration"]["lease"], {"state": "available", "reason": None})
        self.assertEqual(execution["integration"]["lease_record"]["lease_id"], "lease-join")
        self.assertEqual(execution["integration"]["barriers"][0]["evidence_refs"], [f"artifact:sha256:{'b' * 64}"])
        self.assertEqual(execution["recovery"]["attempt_lineage"][0]["predecessor_attempt_id"], "graph:attempt:old")
        self.assertEqual(execution["recovery"]["retry_state"]["invalidations"][0]["reason"], "stopped")

    def test_correlated_child_liveness_replaces_graph_controller_liveness(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {"run_id": "graph", "status": "running", "liveness": {"state": "live", "reason": None}, "evidence": {"state": "available", "reason": None}, "nodes": [{"node_id": "node", "status": "running", "feature_run_id": "child", "liveness": {"state": "liveness_unavailable", "reason": "child liveness is unavailable until a correlated FeatureRun is discovered"}, "evidence": {"state": "available", "reason": None}}]},
            {"run_id": "child", "kind": "feature_run", "status": "running", "liveness": {"state": "stale", "reason": "heartbeat expired"}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "node", "parent_run_id": "graph"}},
        ]
        snapshot = _snapshot(Path("/runs"), now, [], records)
        self.assertEqual(snapshot["plan_graphs"][0]["nodes"][0]["liveness"]["state"], "stale")


if __name__ == "__main__":
    unittest.main()
