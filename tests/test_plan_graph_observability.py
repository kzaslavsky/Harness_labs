"""Acceptance tests for durable and correlated PlanGraph execution."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.audit import AuditConflictError, AuditError, AuditJournal
from harness_labs.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    PlanGraphError,
    PlanGraphPlan,
    PlanRun,
)


def _plan(path: Path, *, base_commit: str = "base") -> PlanGraphPlan:
    path.write_text("approved plan\n", encoding="utf-8")
    return PlanGraphPlan(
        plan=str(path),
        base_commit=base_commit,
        runs=(
            PlanRun("first", "First", ("1",), ("AC-1",)),
            PlanRun("second", "Second", ("2",), ("AC-2",), ("first",)),
        ),
        plan_sections={"1": "First AC-1", "2": "Second AC-2"},
        acceptance_criteria={"AC-1": "AC-1", "AC-2": "AC-2"},
    )


def _success(request, commit: str) -> FeatureRunOutcome:
    return FeatureRunOutcome(
        "succeeded",
        commit,
        plan_graph_id=request.plan_graph_id,
        plan_node_id=request.plan_node_id,
        feature_run_id=request.feature_run_id,
        run_dir=str(request.run_dir),
    )


class PlanGraphObservabilityTests(unittest.TestCase):
    def test_audit_state_keeps_logical_identity_attempt_lineage_and_retry_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_commit = "a" * 40
            audit = PlanGraph(
                _plan(root / "plan.md", base_commit=base_commit),
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-lineage",
            )._audit_for_run()

            initial = audit.state
            self.assertEqual(initial["logical_graph"]["logical_graph_id"], "graph-lineage")
            self.assertEqual(initial["logical_graph"]["plan_digest"], initial["plan_digest"])
            self.assertEqual(initial["logical_graph"]["base_commit"], base_commit)
            self.assertEqual(initial["graph_attempt"], {
                "graph_attempt_id": "graph-lineage",
                "predecessor_attempt_id": None,
            })
            self.assertIsNone(initial["nodes"]["first"]["input_commit"])
            self.assertIsNone(initial["nodes"]["first"]["integrated_commit"])

            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=1,
                allocation_id="allocation-first",
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )
            reserved = audit.state
            first_lineage = reserved["attempt_lineage"]
            self.assertEqual(len(first_lineage), 1)
            self.assertEqual(first_lineage[0]["input_commit"], base_commit)
            self.assertIsNone(first_lineage[0]["predecessor_attempt_id"])
            self.assertEqual(reserved["nodes"]["first"]["input_commit"], base_commit)

            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.node_failed("first", "interrupted", {"reason": "controller stopped"})
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            invalidated = audit.invalidate_successor_attempt(
                allocation_id="allocation-first",
                reason="verified repair required",
                expected_revision=checkpoint["revision"],
            )
            self.assertEqual(invalidated["attempt_id"], first_lineage[0]["attempt_id"])
            self.assertEqual(audit.state["retry_state"]["invalidations"][0]["allocation_id"], "allocation-first")

            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=2,
                allocation_id="allocation-first-retry",
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )
            retried = audit.state
            self.assertEqual(len(retried["attempt_lineage"]), 2)
            self.assertEqual(
                retried["attempt_lineage"][1]["predecessor_attempt_id"],
                first_lineage[0]["attempt_id"],
            )
            self.assertEqual(retried["retry_state"]["reuse"][0]["reused_from_attempt_id"], first_lineage[0]["attempt_id"])

            audit.node_completed("first", "c" * 40)
            completed = audit.state
            self.assertEqual(completed["nodes"]["first"]["integrated_commit"], "c" * 40)
            self.assertEqual(completed["integration_barriers"][0]["input_commit"], base_commit)
            self.assertNotIn("integration_receipts", completed)
            AuditJournal.verify(audit.run_dir)

    def test_succeeded_successor_attempt_cannot_be_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_commit = "a" * 40
            audit = PlanGraph(
                _plan(root / "plan.md", base_commit=base_commit),
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-succeeded-attempt",
            )._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            audit.reserve_successor_attempt(
                node_id="first",
                logical_attempt=1,
                allocation_id="allocation-first",
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )
            audit.node_completed("first", "c" * 40)
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())

            with self.assertRaisesRegex(AuditError, "succeeded attempt"):
                audit.invalidate_successor_attempt(
                    allocation_id="allocation-first",
                    reason="must not retry success",
                    expected_revision=checkpoint["revision"],
                )

            self.assertEqual(audit.state["nodes"]["first"]["status"], "succeeded")
            self.assertEqual(len(audit.state["attempt_lineage"]), 1)

    def test_successor_attempt_batch_is_cas_bound_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_commit = "a" * 40
            graph = PlanGraph(
                _plan(root / "plan.md", base_commit=base_commit),
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-successor-attempt",
            )
            audit = graph._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            receipts = audit.reserve_successor_attempt_batch(
                allocations=(
                    {"node_id": "first", "allocation_id": "allocation-first"},
                    {"node_id": "second", "allocation_id": "allocation-second"},
                ),
                logical_attempt=1,
                parent_candidate_commit=base_commit,
                expected_revision=checkpoint["revision"],
                expected_staging_head=base_commit,
            )

            state = audit.state
            evidence = state["successor_attempts"]
            self.assertEqual(len(evidence), 2)
            self.assertEqual(
                {item["checkpoint_revision"] for item in evidence}, {checkpoint["revision"]}
            )
            self.assertEqual(
                {item["parent_candidate_commit"] for item in evidence}, {base_commit}
            )
            self.assertEqual(
                {item["logical_attempt"] for item in evidence}, {1}
            )
            self.assertEqual(state["nodes"]["first"]["status"], "reserved")
            self.assertEqual(state["nodes"]["second"]["status"], "reserved")
            self.assertEqual(state["active_node_ids"], ["first", "second"])
            self.assertEqual(len({receipt["event_hash"] for receipt in receipts}), 1)

            with self.assertRaisesRegex(AuditConflictError, "revision changed"):
                audit.reserve_successor_attempt_batch(
                    allocations=({"node_id": "second", "allocation_id": "allocation-retry"},),
                    logical_attempt=2,
                    parent_candidate_commit=base_commit,
                    expected_revision=checkpoint["revision"],
                    expected_staging_head=base_commit,
                )
            events = [
                json.loads(line)
                for line in (audit.run_dir / "events.jsonl").read_text().splitlines()
            ]
            reservations = [
                event for event in events
                if event["event_type"] == "plan_graph_successor_attempts_reserved"
            ]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["payload"]["allocations"], evidence)
            AuditJournal.verify(audit.run_dir)

    def test_successor_attempt_rejects_non_schema_commit_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-invalid-identity",
            )._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            for parent_candidate_commit, expected_staging_head in (
                ("base", "a" * 40),
                ("a" * 40, "base"),
            ):
                with self.subTest(
                    parent_candidate_commit=parent_candidate_commit,
                    expected_staging_head=expected_staging_head,
                ), self.assertRaisesRegex(ValueError, "full lowercase Git commit"):
                    audit.reserve_successor_attempt(
                        node_id="first",
                        logical_attempt=1,
                        allocation_id="allocation-first",
                        parent_candidate_commit=parent_candidate_commit,
                        expected_revision=checkpoint["revision"],
                        expected_staging_head=expected_staging_head,
                    )

    def test_legacy_checkpoint_requires_explicit_migration_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-legacy",
            )
            audit = graph._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            del checkpoint["state"]["audit_state_protocol"]
            audit.journal.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(PlanGraphError, "legacy-incompatible"):
                PlanGraph(
                    _plan(root / "plan.md"),
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-legacy",
                )._audit_for_run()

    def test_prior_audit_protocol_is_explicitly_legacy_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: _success(request, "unused"),
                run_root=root / "runs",
                graph_run_id="graph-prior-protocol",
            )
            audit = graph._audit_for_run()
            checkpoint = json.loads(audit.journal.checkpoint_path.read_text())
            checkpoint["state"]["audit_state_protocol"] = "harness-plan-graph-audit/1"
            audit.journal.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(PlanGraphError, "legacy-incompatible"):
                PlanGraph(
                    _plan(root / "plan.md"),
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-prior-protocol",
                )._audit_for_run()

    def test_plan_graph_rejects_non_audited_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(TypeError, "run_root"):
                PlanGraph(
                    _plan(root / "plan.md"),
                    lambda request: _success(request, "unused"),
                )
            with self.assertRaisesRegex(TypeError, "state_path"):
                PlanGraph(
                    _plan(root / "plan.md"),
                    lambda request: _success(request, "unused"),
                    state_path=root / "legacy.json",
                )
            with self.assertRaisesRegex(PlanGraphError, "audited PlanGraph"):
                PlanGraph(
                    _plan(root / "plan.md"),
                    lambda request: _success(request, "unused"),
                    run_root=None,
                )

    def test_normal_cli_launch_always_creates_canonical_graph_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved_plan = root / "plan.md"
            approved_plan.write_text("Build A AC-1\n", encoding="utf-8")
            decomposition = root / "decomposition.json"
            decomposition.write_text(
                json.dumps(
                    {
                        "plan": str(approved_plan),
                        "base_commit": "base",
                        "runs": [
                            {
                                "id": "A",
                                "objective": "Build A",
                                "plan_sections": ["1"],
                                "criteria": ["AC-1"],
                            }
                        ],
                        "plan_sections": {"1": "Build A AC-1"},
                        "acceptance_criteria": {"AC-1": "AC-1"},
                    }
                ),
                encoding="utf-8",
            )
            launcher = root / "launcher.py"
            launcher.write_text(
                "import json, sys\n"
                "request = json.load(sys.stdin)\n"
                "print(json.dumps({\n"
                "    'status': 'succeeded',\n"
                "    'candidate_commit': 'candidate',\n"
                "    'plan_graph_id': request['plan_graph_id'],\n"
                "    'plan_node_id': request['plan_node_id'],\n"
                "    'feature_run_id': request['feature_run_id'],\n"
                "    'run_dir': request['run_dir'],\n"
                "}))\n",
                encoding="utf-8",
            )
            run_root = root / "logs" / "runs"
            runner = (
                Path(__file__).resolve().parents[1] / "scripts" / "run_plan_graph.py"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    str(decomposition),
                    "--launcher-command",
                    sys.executable,
                    str(launcher),
                    "--graph-run-id",
                    "normal-launch",
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            run_dir = run_root / "normal-launch"
            descriptor = json.loads((run_dir / "descriptor.json").read_text())
            self.assertEqual(descriptor["run_kind"], "plan_graph")
            self.assertEqual(AuditJournal.verify(run_dir)["run_id"], "normal-launch")
            self.assertTrue((run_dir / "events.jsonl").is_file())

    def test_graph_id_cannot_resolve_to_the_run_root_or_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for graph_run_id in (".", ".."):
                with self.subTest(graph_run_id=graph_run_id):
                    with self.assertRaisesRegex(PlanGraphError, "path-safe"):
                        PlanGraph(
                            _plan(root / f"{graph_run_id}.md"),
                            lambda request: _success(request, "unused"),
                            run_root=root / "runs",
                            graph_run_id=graph_run_id,
                        ).run()

    def test_new_graph_binds_descriptor_and_durable_node_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = []
            result = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: requests.append(request) or _success(
                    request, f"{request.run.id}-commit"
                ),
                run_root=root / "logs" / "runs",
                graph_run_id="graph-1",
            ).run()

            run_dir = root / "logs" / "runs" / "graph-1"
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            descriptor = json.loads((run_dir / "descriptor.json").read_text())
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(descriptor["run_id"], "graph-1")
            self.assertEqual(
                checkpoint["state"]["nodes"]["first"]["status"], "succeeded"
            )
            self.assertEqual(
                checkpoint["state"]["nodes"]["second"]["candidate_commit"],
                "second-commit",
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in events
                    if event["event_type"].startswith("plan_")
                ],
                [
                    "plan_graph_initialized",
                    "plan_node_started",
                    "plan_node_completed",
                    "plan_node_started",
                    "plan_node_completed",
                    "plan_graph_completed",
                ],
            )
            self.assertEqual(AuditJournal.verify(run_dir)["run_id"], "graph-1")
            self.assertEqual(requests[0].plan_graph_id, "graph-1")
            self.assertEqual(requests[0].plan_node_id, "first")
            self.assertEqual(requests[0].feature_run_id, "graph-1-first")
            self.assertEqual(
                requests[0].run_dir,
                (root / "logs" / "runs" / "graph-1-first").resolve(),
            )

    def test_resume_uses_durable_successful_node_not_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_plan = _plan(root / "plan.md")
            with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                PlanGraph(
                    queue_plan,
                    lambda request: (
                        _success(request, "first-commit")
                        if request.run.id == "first"
                        else (_ for _ in ()).throw(RuntimeError("controller stopped"))
                    ),
                    run_root=root / "runs",
                    graph_run_id="graph-resume",
                ).run()
            calls = []
            second = PlanGraph(
                queue_plan,
                lambda request: calls.append(request.run.id) or _success(request, "second-commit"),
                run_root=root / "runs",
                graph_run_id="graph-resume",
            ).run()
            self.assertEqual(second.status, "succeeded")
            self.assertEqual(calls, ["second"])

    def test_resume_rejects_a_changed_decomposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_plan = _plan(root / "plan.md")
            with self.assertRaisesRegex(RuntimeError, "controller stopped"):
                PlanGraph(
                    queue_plan,
                    lambda request: (
                        _success(request, "first-commit")
                        if request.run.id == "first"
                        else (_ for _ in ()).throw(RuntimeError("controller stopped"))
                    ),
                    run_root=root / "runs",
                    graph_run_id="graph-plan-binding",
                ).run()
            changed_plan = PlanGraphPlan(
                plan=queue_plan.plan,
                base_commit=queue_plan.base_commit,
                runs=(
                    queue_plan.runs[0],
                    PlanRun("second", "Second", ("2",), ("AC-2",)),
                ),
                plan_sections=queue_plan.plan_sections,
                acceptance_criteria=queue_plan.acceptance_criteria,
            )
            with self.assertRaisesRegex(PlanGraphError, "does not match the supplied plan"):
                PlanGraph(
                    changed_plan,
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-plan-binding",
                ).run()

    def test_resume_rejects_changed_launch_or_final_test_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_plan = _plan(root / "plan.md")
            first = PlanGraph(
                queue_plan,
                lambda request: _success(request, f"{request.run.id}-commit"),
                run_root=root / "runs",
                graph_run_id="graph-contract-binding",
            ).run()
            self.assertEqual(first.status, "succeeded")
            changed_plan = PlanGraphPlan(
                plan=queue_plan.plan,
                base_commit=queue_plan.base_commit,
                runs=(
                    PlanRun(
                        "first",
                        "First",
                        ("1",),
                        ("AC-1",),
                        verification_argv=("verify-first",),
                    ),
                    queue_plan.runs[1],
                ),
                plan_sections=queue_plan.plan_sections,
                acceptance_criteria=queue_plan.acceptance_criteria,
                functionality_tests=("verify final",),
            )
            with self.assertRaisesRegex(PlanGraphError, "does not match the supplied plan"):
                PlanGraph(
                    changed_plan,
                    lambda request: _success(request, "unused"),
                    run_root=root / "runs",
                    graph_run_id="graph-contract-binding",
                ).run()

    def test_mismatched_child_identity_is_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = PlanGraph(
                _plan(root / "plan.md"),
                lambda request: FeatureRunOutcome("succeeded", "bad-commit"),
                run_root=root / "runs",
                graph_run_id="graph-mismatch",
            ).run()
            self.assertEqual(result.status, "failed")
            checkpoint = json.loads(
                (root / "runs" / "graph-mismatch" / "checkpoint.json").read_text()
            )
            self.assertEqual(
                checkpoint["state"]["nodes"]["first"]["status"], "failed"
            )


if __name__ == "__main__":
    unittest.main()
