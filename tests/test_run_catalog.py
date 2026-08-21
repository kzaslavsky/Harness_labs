from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.plangraph.plan_graph_audit import PlanGraphAudit
from harness_labs.observability.run_catalog import _ID_MATCH_REASON, _REUSE_UNRESOLVED_REASON, _apply_unique_display_names, _detail_metrics, _feature_run_display_name, _graph_execution, _snapshot, build_run_catalog, build_run_detail, merge_run_catalogs


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

    def test_in_flight_run_evidence_is_partial_not_unavailable(self) -> None:
        # A running run legitimately has no terminal manifest yet (the
        # manifest is written at seal), but its journal prefix was verified
        # or the record would be corrupt.  That is partial evidence -- "not
        # yet produced" -- and must never read as the blanket "no terminal
        # manifest exists" that describes a terminal run's missing manifest.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, "active")
            self._run(root, "done", terminal=True)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            audit = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-live", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-live"),
                objective="test graph",
                nodes={"lane": {"status": "running", "feature_run_id": "active", "depends_on": []}},
                functionality_tests=(),
            )
            audit.journal.checkpoint("running", audit.state)
            snapshot = build_run_catalog(root)
        runs = {record["run_id"]: record for record in snapshot["feature_runs"]}
        self.assertEqual(runs["active"]["status"], "running")
        self.assertEqual(runs["active"]["evidence"]["state"], "partial")
        self.assertIn("in flight", runs["active"]["evidence"]["reason"])
        self.assertIn("not yet produced", runs["active"]["evidence"]["reason"])
        self.assertEqual(runs["done"]["evidence"], {"state": "available", "reason": None})
        graph = snapshot["plan_graphs"][0]
        self.assertEqual(graph["status"], "running")
        self.assertEqual(graph["evidence"]["state"], "partial")
        self.assertIn("in flight", graph["evidence"]["reason"])

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
        self.assertIsNone(projected["by_agent"][0]["peak_input_tokens"])  # claude-print usage is cumulative; no per-invocation peak
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

    def test_detail_infers_claude_cost_without_long_context_premium(self) -> None:
        # Claude 4.6+ bills the full context window at standard rates, and
        # claude-print usage is cumulative across turns, so a large input sum
        # must never trigger the GPT long-context multipliers.
        metrics = _detail_metrics({
            "events": [{
                "event_type": "backend_transport", "attempt_id": "implement-cost/attempt-1",
                "actor": {"id": "worker", "role": "semantic_worker"}, "backend_id": "claude-print", "duration_ms": 1,
                "payload": {"model": "claude-sonnet-5", "reasoning": "medium", "usage": {
                    "input_tokens": 3_000_000, "cached_input_tokens": 2_000_000, "output_tokens": 50_000, "cost_usd": None,
                }},
            }],
            "checkpoint": {"state": {}}, "summary": None,
        })
        cost = metrics["totals"]["cost"]
        self.assertEqual(cost["state"], "estimated")
        # 1M uncached * $2 + 2M cache-read * $0.20 + 50k output * $10, per MTok
        self.assertEqual(cost["usd"], 2.9)
        self.assertEqual(cost["long_context_records"], 0)
        self.assertIn("platform.claude.com", cost["sources"][0])

    def test_detail_projects_claude_session_stream_coordinator_row(self) -> None:
        # ClaudeAgentSession coordinators journal their stream-json transcript
        # but never emit a backend_transport usage record; the projection must
        # recover their tokens, authoritative cost, and per-request context
        # peak from the stream — deduplicating repeated per-content-block
        # assistant events and merging events journaled before the session id
        # was identified.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "claude-session-run")
            journal = AuditJournal.open_existing(run, actor=AuditActor("claude-session", "backend"))
            journal.append("backend_process_started", status="started", backend_id="claude-session", payload={"model": "claude-fable-5", "effort": "medium", "pid": 123})
            init = journal.write_artifact("claude-stream-inbound", {"type": "system", "subtype": "init", "model": "claude-fable-5", "session_id": "sess-1"}, media_type="application/x-ndjson")
            journal.append("transport_message", status="received", backend_id="claude-session", artifacts=(init,), payload={"direction": "inbound", "type": "system", "subtype": "init"})
            turn_one = {"type": "assistant", "session_id": "sess-1", "message": {"id": "msg_1", "model": "claude-fable-5", "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 40, "output_tokens": 5}}}
            for _ in range(2):  # stream repeats the event per content block
                artifact = journal.write_artifact("claude-stream-inbound", turn_one, media_type="application/x-ndjson")
                journal.append("transport_message", status="received", backend_id="claude-session", artifacts=(artifact,), payload={"direction": "inbound", "type": "assistant"})
            turn_two = {"type": "assistant", "session_id": "sess-1", "message": {"id": "msg_2", "model": "claude-fable-5", "usage": {"input_tokens": 12, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 0, "output_tokens": 9}}}
            artifact = journal.write_artifact("claude-stream-inbound", turn_two, media_type="application/x-ndjson")
            journal.append("transport_message", status="received", backend_id="claude-session", artifacts=(artifact,), payload={"direction": "inbound", "type": "assistant"})
            result = {"type": "result", "session_id": "sess-1", "num_turns": 2, "total_cost_usd": 1.25, "usage": {"input_tokens": 22, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 40, "output_tokens": 14}}
            artifact = journal.write_artifact("claude-stream-inbound", result, media_type="application/x-ndjson")
            journal.append("transport_message", status="received", backend_id="claude-session", artifacts=(artifact,), payload={"direction": "inbound", "type": "result", "subtype": "success"})
            journal.checkpoint("running", {"controller": {"criteria": {}, "tasks": {}, "findings": {}}})
            detail = build_run_detail(root, "claude-session-run")
        projected = detail["metrics"]
        self.assertEqual(len(projected["by_agent"]), 1)
        row = projected["by_agent"][0]
        self.assertEqual(row["label"], "claude-session coordinator")
        self.assertEqual(row["model"], "claude-fable-5")
        self.assertEqual(row["phase"], "coordinate")  # not the overseen controller phase
        self.assertEqual(row["effort"], "medium")
        self.assertEqual(row["calls"], 2)
        self.assertEqual(row["total_tokens"], 576)  # result usage: 22+500+40 in, 14 out
        self.assertEqual(row["peak_input_tokens"], 412)  # msg_2 context: 12+400+0
        self.assertEqual(row["cost"]["state"], "available")
        self.assertEqual(row["cost"]["usd"], 1.25)
        self.assertEqual(projected["totals"]["peak_input_tokens"], 412)
        self.assertIn("claude session stream artifacts", projected["provenance"]["collection_method"])

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

    def test_id_matched_legacy_child_correlates_without_descriptor(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {"run_id": "graph", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "nodes": [
                {"node_id": "matched", "status": "succeeded", "feature_run_id": "graph-matched", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}},
                {"node_id": "attested", "status": "running", "feature_run_id": "attested-child", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}},
                {"node_id": "missing", "status": "queued", "feature_run_id": "graph-missing", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}},
            ]},
            {"run_id": "graph-matched", "kind": "legacy_feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "partial", "reason": "descriptor was absent for the legacy run"}, "correlation": None},
            {"run_id": "attested-child", "kind": "feature_run", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "attested", "parent_run_id": "graph"}},
        ]

        snapshot = _snapshot(Path("/runs"), now, [], records)

        nodes = {node["node_id"]: node for node in snapshot["plan_graphs"][0]["nodes"]}
        self.assertEqual(nodes["matched"]["evidence"], {"state": "partial", "reason": _ID_MATCH_REASON})
        self.assertEqual(nodes["matched"]["liveness"], {"state": "terminal", "reason": None})
        self.assertEqual(nodes["attested"]["evidence"], {"state": "available", "reason": None})
        self.assertEqual(nodes["missing"]["evidence"], {"state": "partial", "reason": "child correlation is not verified"})
        runs = {record["run_id"]: record for record in snapshot["feature_runs"]}
        self.assertEqual(runs["graph-matched"]["correlation"], {
            "plan_graph_id": "graph", "plan_node_id": "matched", "parent_run_id": "graph",
            "state": "id_matched", "reason": _ID_MATCH_REASON,
        })
        self.assertEqual(runs["attested-child"]["correlation"], {"plan_graph_id": "graph", "plan_node_id": "attested", "parent_run_id": "graph"})
        self.assertEqual(snapshot["ungrouped_feature_runs"], [])

    def test_id_matching_is_reserved_for_descriptor_less_legacy_runs(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {"run_id": "graph", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "nodes": [
                {"node_id": "standalone", "status": "running", "feature_run_id": "standalone-child", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}},
                {"node_id": "broken", "status": "failed", "feature_run_id": "corrupt-child", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}},
            ]},
            {"run_id": "standalone-child", "kind": "feature_run", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "correlation": None},
            {"run_id": "corrupt-child", "kind": "legacy_feature_run", "status": "corrupt", "liveness": {"state": "liveness_unavailable", "reason": "run is corrupt"}, "evidence": {"state": "unavailable", "reason": "corrupt"}, "correlation": None},
        ]

        snapshot = _snapshot(Path("/runs"), now, [], records)

        for node in snapshot["plan_graphs"][0]["nodes"]:
            self.assertEqual(node["evidence"], {"state": "partial", "reason": "child correlation is not verified"})
        for record in snapshot["feature_runs"]:
            self.assertIsNone(record["correlation"])
        self.assertEqual([record["run_id"] for record in snapshot["ungrouped_feature_runs"]], ["standalone-child", "corrupt-child"])

    def test_merged_roots_id_match_without_upgrading_to_attested_evidence(self) -> None:
        graph_catalog = {
            "availability": {"state": "available", "reason": None}, "generated_at": "2026-08-09T00:00:00Z", "diagnostics": [],
            "plan_graphs": [{"run_id": "graph", "status": "running", "liveness": {"state": "liveness_unavailable", "reason": "no lease"}, "evidence": {"state": "available", "reason": None}, "nodes": [
                {"node_id": "node", "status": "succeeded", "feature_run_id": "graph-node", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "partial", "reason": "child correlation is not verified"}},
            ]}],
            "feature_runs": [], "ungrouped_feature_runs": [],
        }
        child = {"run_id": "graph-node", "kind": "legacy_feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "partial", "reason": "descriptor was absent for the legacy run"}, "correlation": None}
        child_catalog = {
            "availability": {"state": "available", "reason": None}, "generated_at": "2026-08-09T00:00:01Z", "diagnostics": [],
            "plan_graphs": [], "feature_runs": [child], "ungrouped_feature_runs": [child],
        }

        merged = merge_run_catalogs([(Path("/roots/a"), graph_catalog), (Path("/roots/b"), child_catalog)])

        node = merged["plan_graphs"][0]["nodes"][0]
        self.assertEqual(node["evidence"], {"state": "partial", "reason": _ID_MATCH_REASON})
        self.assertEqual(node["liveness"], {"state": "terminal", "reason": None})
        merged_child = merged["feature_runs"][0]
        self.assertEqual(merged_child["correlation"]["state"], "id_matched")
        self.assertEqual(merged_child["correlation"]["plan_node_id"], "node")
        self.assertEqual(merged["ungrouped_feature_runs"], [])

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

    @staticmethod
    def _reuse_graph(run_id: str, *, reused_from: str | None, commit: str | None = "c" * 40, node_id: str = "N") -> dict:
        return {
            "run_id": run_id, "status": "running",
            "liveness": {"state": "liveness_unavailable", "reason": "no lease"},
            "evidence": {"state": "available", "reason": None},
            "nodes": [{
                "node_id": node_id, "status": "succeeded", "feature_run_id": f"{run_id}-{node_id}",
                "depends_on": [], "liveness": {"state": "not_applicable", "reason": None},
                "evidence": {"state": "available", "reason": None},
                "reused_from_attempt": reused_from, "candidate_commit": commit,
            }],
        }

    @staticmethod
    def _legacy_run(run_id: str, status: str = "succeeded") -> dict:
        liveness = {"state": "terminal", "reason": None} if status != "corrupt" else {"state": "liveness_unavailable", "reason": "run is corrupt"}
        evidence = {"state": "partial", "reason": "descriptor was absent for the legacy run"} if status != "corrupt" else {"state": "unavailable", "reason": "corrupt"}
        return {"run_id": run_id, "kind": "legacy_feature_run", "status": status, "liveness": liveness, "evidence": evidence, "correlation": None}

    def test_reused_nodes_resolve_origin_across_two_hop_chain(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            self._reuse_graph("root", reused_from=None),
            self._reuse_graph("attempt-1", reused_from="root"),
            self._reuse_graph("attempt-2", reused_from="attempt-1"),
            self._legacy_run("root-N"),
        ]

        snapshot = _snapshot(Path("/runs"), now, [], records)

        graphs = {graph["run_id"]: graph for graph in snapshot["plan_graphs"]}
        two_hop = graphs["attempt-2"]["nodes"][0]
        self.assertEqual(two_hop["correlation"]["state"], "reused")
        self.assertEqual(two_hop["correlation"]["origin_attempt_id"], "root")
        self.assertEqual(two_hop["correlation"]["origin_feature_run_id"], "root-N")
        self.assertEqual(two_hop["correlation"]["reused_from_attempt"], "attempt-1")
        self.assertEqual(two_hop["evidence"]["state"], "partial")
        self.assertIn("root-N", two_hop["evidence"]["reason"])
        self.assertEqual(two_hop["liveness"], {"state": "terminal", "reason": None})
        one_hop = graphs["attempt-1"]["nodes"][0]
        self.assertEqual(one_hop["correlation"]["origin_attempt_id"], "root")
        # The origin node keeps its own (stronger) direct correlation path.
        self.assertNotIn("correlation", graphs["root"]["nodes"][0])
        self.assertEqual(graphs["root"]["nodes"][0]["evidence"]["reason"], _ID_MATCH_REASON)

    def test_reuse_resolution_refuses_broken_chains_without_fabricating_origins(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        missing_chain = self._reuse_graph("gone-attempt", reused_from="never-recorded")
        mismatched_origin = self._reuse_graph("swapped-attempt", reused_from="swapped-root", commit="d" * 40)
        swapped_root = self._reuse_graph("swapped-root", reused_from=None, commit="e" * 40)
        corrupt_attempt = self._reuse_graph("corrupt-attempt", reused_from="corrupt-root")
        corrupt_root = self._reuse_graph("corrupt-root", reused_from=None)
        records = [
            missing_chain, mismatched_origin, swapped_root, corrupt_attempt, corrupt_root,
            self._legacy_run("swapped-root-N"),
            self._legacy_run("corrupt-root-N", status="corrupt"),
        ]

        snapshot = _snapshot(Path("/runs"), now, [], records)

        graphs = {graph["run_id"]: graph for graph in snapshot["plan_graphs"]}
        for run_id in ("gone-attempt", "swapped-attempt", "corrupt-attempt"):
            node = graphs[run_id]["nodes"][0]
            self.assertIsNone(node["correlation"], run_id)
            self.assertEqual(node["evidence"], {"state": "partial", "reason": _REUSE_UNRESOLVED_REASON}, run_id)

    def test_merged_roots_resolve_reuse_chain_across_audit_roots(self) -> None:
        def catalog(graphs: list[dict], features: list[dict]) -> dict:
            return {
                "availability": {"state": "available", "reason": None}, "generated_at": "2026-08-09T00:00:00Z",
                "diagnostics": [], "plan_graphs": graphs, "feature_runs": features,
                "ungrouped_feature_runs": list(features),
            }

        successor_catalog = catalog([self._reuse_graph("attempt-2", reused_from="attempt-1")], [])
        origin_catalog = catalog(
            [self._reuse_graph("attempt-1", reused_from="root"), self._reuse_graph("root", reused_from=None)],
            [self._legacy_run("root-N")],
        )

        merged = merge_run_catalogs([(Path("/roots/a"), successor_catalog), (Path("/roots/b"), origin_catalog)])

        graphs = {graph["run_id"]: graph for graph in merged["plan_graphs"]}
        node = graphs["attempt-2"]["nodes"][0]
        self.assertEqual(node["correlation"]["state"], "reused")
        self.assertEqual(node["correlation"]["origin_attempt_id"], "root")
        self.assertEqual(node["correlation"]["origin_feature_run_id"], "root-N")
        self.assertEqual(node["liveness"], {"state": "terminal", "reason": None})

    def test_detail_busy_time_is_interval_union_not_summed_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "busy")
            journal = AuditJournal.open_existing(run, actor=AuditActor("controller", "controller"))
            usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}
            for attempt in ("implement-busy/attempt-1", "implement-busy/attempt-1/verify"):
                journal.append(
                    "backend_transport", status="succeeded", attempt_id=attempt,
                    actor=AuditActor(attempt, "semantic_worker"), backend_id="codex-exec", duration_ms=1_000,
                    payload={"model": "gpt-test", "usage": usage},
                )
            journal.checkpoint("running", journal.checkpoint_state())
            totals = build_run_detail(root, "busy")["metrics"]["totals"]
        # The two 1000 ms activity intervals were recorded back-to-back and
        # overlap in monotonic time, so the honest busy union is strictly
        # smaller than the summed backend durations.
        self.assertEqual(totals["duration_ms"], 2_000)
        self.assertIsInstance(totals["busy_ms"], int)
        self.assertGreaterEqual(totals["busy_ms"], 1_000)
        self.assertLess(totals["busy_ms"], 2_000)

    def test_detail_busy_time_is_unavailable_when_a_record_lacks_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self._run(root, "untimed")
            journal = AuditJournal.open_existing(run, actor=AuditActor("controller", "controller"))
            journal.append(
                "backend_transport", status="succeeded", attempt_id="implement-untimed/attempt-1",
                actor=AuditActor("implement-untimed/attempt-1", "semantic_worker"), backend_id="codex-exec",
                payload={"model": "gpt-test", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}},
            )
            journal.checkpoint("running", journal.checkpoint_state())
            totals = build_run_detail(root, "untimed")["metrics"]["totals"]
        self.assertIsNone(totals["busy_ms"])

    def test_plan_graph_display_name_uses_stem_and_attempt_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "dashboard-observability-metrics-plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-attempt", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-attempt"),
                objective="test graph", nodes={}, functionality_tests=(),
            )
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
            snapshot = build_run_catalog(root)
        graphs = {graph["run_id"]: graph for graph in snapshot["plan_graphs"]}
        # Basename split on -/_, title-cased; no attempt suffix when
        # graph_attempt_id == logical_graph_id (the default first attempt).
        self.assertEqual(graphs["graph-attempt"]["display_name"], "Dashboard Observability Metrics Plan")
        # graph_attempt_id != logical_graph_id triggers the attempt suffix.
        self.assertTrue(graphs["lineage-graph"]["display_name"].startswith("Dashboard Observability Metrics Plan (Attempt"))

    def test_plan_graph_display_names_are_unique_without_lineage_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "shared-plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            for index, run_id in enumerate(("historical-a", "historical-b")):
                audit = PlanGraphAudit(
                    repository=root, run_root=root, graph_run_id=run_id, plan=str(plan),
                    plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                    base_commit="a" * 40, registration_binding=_registration_binding(run_id),
                    objective=f"historical graph {index}", nodes={}, functionality_tests=(),
                )
                descriptor_path = audit.journal.run_dir / "descriptor.json"
                descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
                # Historical descriptors predate the lineage extension: the
                # fields are absent from the raw JSON entirely, not merely
                # equal to the run ID (both cases collapse to the same
                # defaulted logical_graph_id/graph_attempt_id downstream).
                for key in ("logical_graph_id", "graph_attempt_id", "predecessor_attempt_id"):
                    del descriptor[key]
                descriptor["created_at"] = f"2026-08-{10 + index:02d}T00:00:00Z"
                raw = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
                descriptor_path.write_bytes(raw)
                audit.journal.append(
                    "run_descriptor_bound", status="succeeded",
                    payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()},
                )
                audit.journal.checkpoint("running", audit.state)
            snapshot = build_run_catalog(root)
        names = {graph["run_id"]: graph["display_name"] for graph in snapshot["plan_graphs"]}
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names.values())), 2)
        for name in names.values():
            # Both share the "Shared Plan" base name; the ordinal-suffix rule
            # alone never fires for lineage-absent records (their descriptor
            # never names a graph_attempt_id != logical_graph_id), so
            # uniqueness comes only from the created_at/run_id disambiguator.
            self.assertTrue(name.startswith("Shared Plan ("))
            self.assertIn("#", name)

    def test_unique_display_names_catch_cross_group_collisions(self) -> None:
        # r1 and r2 both start from base name "Plan" and collide, so r2 is
        # disambiguated to "Plan (unknown-date #r2)". r3's *own* base name is
        # already exactly that literal string (as a lineage-absent base name
        # would embed its own disambiguator). A naive per-base-name-group
        # resolution never compares r2's disambiguated output against r3's
        # base name and would let them collide; the shared `used` set must
        # catch it and push r3 to the terminal run_id tiebreak.
        graphs = [{"run_id": "r1"}, {"run_id": "r2"}, {"run_id": "r3"}]
        base_names = {"r1": "Plan", "r2": "Plan", "r3": "Plan (unknown-date #r2)"}
        _apply_unique_display_names(graphs, base_names)
        names = {graph["run_id"]: graph["display_name"] for graph in graphs}
        self.assertEqual(len(set(names.values())), 3)
        self.assertEqual(names["r2"], "Plan (unknown-date #r2)")
        self.assertEqual(names["r3"], "Plan (unknown-date #r2) (unknown-date #r3)")

    def test_merged_catalog_plan_graph_display_names_stay_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory_a, tempfile.TemporaryDirectory() as directory_b:
            root_a, root_b = Path(directory_a), Path(directory_b)
            for root, run_id in ((root_a, "graph-a"), (root_b, "graph-b")):
                plan = root / "shared-plan.md"
                plan.write_text("approved plan\n", encoding="utf-8")
                PlanGraphAudit(
                    repository=root, run_root=root, graph_run_id=run_id, plan=str(plan),
                    plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                    base_commit="a" * 40, registration_binding=_registration_binding(run_id),
                    objective="graph", nodes={}, functionality_tests=(),
                )
            catalog_a = build_run_catalog(root_a)
            catalog_b = build_run_catalog(root_b)
            # Each independent single-attempt graph is unambiguous within its
            # own root: no attempt suffix, no lineage-absent disambiguator.
            self.assertEqual(catalog_a["plan_graphs"][0]["display_name"], "Shared Plan")
            self.assertEqual(catalog_b["plan_graphs"][0]["display_name"], "Shared Plan")
            merged = merge_run_catalogs([(root_a, catalog_a), (root_b, catalog_b)])
        names = {graph["run_id"]: graph["display_name"] for graph in merged["plan_graphs"]}
        self.assertEqual(len(set(names.values())), 2)
        self.assertNotEqual(names["graph-a"], "Shared Plan")
        self.assertNotEqual(names["graph-b"], "Shared Plan")

    def test_merged_catalog_renumbers_attempt_ordinal_when_original_attempt_is_in_another_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory_a, tempfile.TemporaryDirectory() as directory_b:
            root_a, root_b = Path(directory_a), Path(directory_b)
            plan_a = root_a / "shared-plan.md"
            plan_a.write_text("approved plan\n", encoding="utf-8")
            origin = PlanGraphAudit(
                repository=root_a, run_root=root_a, graph_run_id="origin-graph", plan=str(plan_a),
                plan_sha256=hashlib.sha256(plan_a.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("origin-graph"),
                objective="graph", nodes={}, functionality_tests=(),
            )
            origin_descriptor_path = origin.journal.run_dir / "descriptor.json"
            origin_descriptor = json.loads(origin_descriptor_path.read_text(encoding="utf-8"))
            origin_descriptor["created_at"] = "2026-08-01T00:00:00Z"
            origin_raw = (json.dumps(origin_descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            origin_descriptor_path.write_bytes(origin_raw)
            origin.journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(origin_raw).hexdigest()})
            origin.journal.checkpoint("running", origin.state)

            plan_b = root_b / "shared-plan.md"
            plan_b.write_text("approved plan\n", encoding="utf-8")
            successor = PlanGraphAudit(
                repository=root_b, run_root=root_b, graph_run_id="successor-graph", plan=str(plan_b),
                plan_sha256=hashlib.sha256(plan_b.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("successor-graph"),
                objective="graph", nodes={}, functionality_tests=(),
            )
            successor_descriptor_path = successor.journal.run_dir / "descriptor.json"
            successor_descriptor = json.loads(successor_descriptor_path.read_text(encoding="utf-8"))
            successor_descriptor.update({
                "logical_graph_id": "origin-graph",
                "graph_attempt_id": "successor-attempt-2",
                "predecessor_attempt_id": "origin-graph",
                "created_at": "2026-08-02T00:00:00Z",
            })
            successor_raw = (json.dumps(successor_descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
            successor_descriptor_path.write_bytes(successor_raw)
            successor.journal.append("run_descriptor_bound", status="succeeded", payload={"descriptor_sha256": hashlib.sha256(successor_raw).hexdigest()})
            successor.journal.checkpoint("running", successor.state)

            catalog_a = build_run_catalog(root_a)
            catalog_b = build_run_catalog(root_b)
            # Each root only sees its own attempt: the origin (attempt_id ==
            # logical_id) gets no suffix, and the successor -- unaware the
            # origin exists locally -- also computes ordinal 1.
            self.assertEqual(catalog_a["plan_graphs"][0]["display_name"], "Shared Plan")
            self.assertEqual(catalog_b["plan_graphs"][0]["display_name"], "Shared Plan (Attempt 1)")

            merged = merge_run_catalogs([(root_a, catalog_a), (root_b, catalog_b)])
        names = {graph["run_id"]: graph["display_name"] for graph in merged["plan_graphs"]}
        # The un-suffixed origin still occupies ordinal position 1 in the
        # full merged sibling set, so the successor must be renumbered to
        # (Attempt 2) rather than left at its stale, locally-computed
        # (Attempt 1).
        self.assertEqual(names["origin-graph"], "Shared Plan")
        self.assertEqual(names["successor-graph"], "Shared Plan (Attempt 2)")

    def test_node_projection_carries_objective_from_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-attempt", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-attempt"),
                objective="test graph",
                nodes={"root": {"status": "queued", "feature_run_id": "child-root", "depends_on": [], "objective": "Implement the catalog naming projection."}},
                functionality_tests=(),
            )
            graph = build_run_catalog(root)["plan_graphs"][0]
        self.assertEqual(graph["nodes"][0]["objective"], "Implement the catalog naming projection.")

    def test_feature_run_display_name_and_objective_fallback_chain(self) -> None:
        now = datetime(2026, 8, 9, tzinfo=timezone.utc)
        records = [
            {
                "run_id": "graph", "status": "running",
                "liveness": {"state": "liveness_unavailable", "reason": "no lease"},
                "evidence": {"state": "available", "reason": None},
                "nodes": [
                    {"node_id": "node-with-objective", "status": "succeeded", "feature_run_id": "child-a", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}, "objective": "Ship the naming projection. It has two sentences."},
                    {"node_id": "node-without-objective", "status": "succeeded", "feature_run_id": "child-b", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}, "objective": None},
                    {"node_id": "node-bare", "status": "succeeded", "feature_run_id": "child-c", "liveness": {"state": "not_applicable", "reason": None}, "evidence": {"state": "available", "reason": None}, "objective": None},
                ],
            },
            {"run_id": "child-a", "kind": "feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "node-with-objective", "parent_run_id": "graph"}, "objective": "Descriptor objective that should lose to the node's."},
            {"run_id": "child-b", "kind": "feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "node-without-objective", "parent_run_id": "graph"}, "objective": "Only the descriptor carries prose here."},
            {"run_id": "child-c", "kind": "feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "available", "reason": None}, "correlation": {"plan_graph_id": "graph", "plan_node_id": "node-bare", "parent_run_id": "graph"}, "objective": None},
            {"run_id": "ungrouped-run", "kind": "legacy_feature_run", "status": "succeeded", "liveness": {"state": "terminal", "reason": None}, "evidence": {"state": "partial", "reason": "descriptor was absent for the legacy run"}, "correlation": None, "objective": None},
        ]
        snapshot = _snapshot(Path("/runs"), now, [], records)
        features = {record["run_id"]: record for record in snapshot["feature_runs"]}
        ungrouped = {record["run_id"]: record for record in snapshot["ungrouped_feature_runs"]}
        # 1) A correlated node's own objective outranks the FeatureRun's
        #    descriptor objective.
        self.assertEqual(features["child-a"]["objective"], "Ship the naming projection. It has two sentences.")
        self.assertEqual(features["child-a"]["display_name"], "Ship the naming projection.")
        # 2) No node objective: fall back to the descriptor's own objective.
        self.assertEqual(features["child-b"]["objective"], "Only the descriptor carries prose here.")
        self.assertEqual(features["child-b"]["display_name"], "Only the descriptor carries prose here.")
        # 3) No prose anywhere: fall back to the correlated node_id.
        self.assertIsNone(features["child-c"]["objective"])
        self.assertEqual(features["child-c"]["display_name"], "node-bare")
        # 4) No prose and no correlated node: fall back to run_id.
        self.assertIsNone(ungrouped["ungrouped-run"]["objective"])
        self.assertEqual(ungrouped["ungrouped-run"]["display_name"], "ungrouped-run")

    def test_feature_run_display_name_truncates_long_objectives(self) -> None:
        long_sentence = "A" * 90
        name = _feature_run_display_name(long_sentence, None, "run-id")
        self.assertEqual(len(name), 80)
        self.assertTrue(name.endswith("…"))

    def test_block_escalation_indicator_reflects_the_journal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.md"
            plan.write_text("approved plan\n", encoding="utf-8")
            PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-clean", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-clean"),
                objective="test graph", nodes={}, functionality_tests=(),
            )
            audit = PlanGraphAudit(
                repository=root, run_root=root, graph_run_id="graph-blocked", plan=str(plan),
                plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
                base_commit="a" * 40, registration_binding=_registration_binding("graph-blocked"),
                objective="test graph", nodes={}, functionality_tests=(),
            )
            evidence_ref = f"artifact:sha256:{'a' * 64}"
            audit.journal.append(
                "plan_graph_block_escalated", status="blocked",
                payload={"blocker_evidence_ref": evidence_ref, "stable_path": "escalation.json"},
            )
            audit.journal.checkpoint("running", audit.state)
            graphs = {graph["run_id"]: graph for graph in build_run_catalog(root)["plan_graphs"]}
        self.assertEqual(graphs["graph-blocked"]["execution"]["block_escalation"], {
            "escalated": True, "blocker_evidence_ref": evidence_ref, "stable_path": "escalation.json",
        })
        self.assertEqual(graphs["graph-clean"]["execution"]["block_escalation"], {
            "escalated": False, "blocker_evidence_ref": None, "stable_path": None,
        })


if __name__ == "__main__":
    unittest.main()
