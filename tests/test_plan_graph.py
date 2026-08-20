"""Deterministic acceptance tests for registered PlanGraph execution."""

from __future__ import annotations

from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harness_labs.core.audit import AuditError
from harness_labs.plangraph.plan_graph import (
    ESCALATION_JUDGMENT_PROTOCOL,
    FEATURE_RUN_REQUEST_PROTOCOL,
    FeatureRunOutcome,
    FeatureRunRequest,
    GateSlot,
    PlanGraph,
    PlanGraphError,
    PlanGraphRegistration,
    PlanRun,
    RepairResumeDirective,
    ReadySetDispatch,
    ReadySetScheduler,
    SubprocessFeatureRunLauncher,
    load_registration,
    persist_registration,
    register_plan_graph,
    registration_bytes,
    verify_registration,
)
from harness_labs.plangraph.plan_graph_budget import RetryBudgetLedger


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def decomposition(base_commit: str, *, functionality_tests=()) -> dict[str, object]:
    return {
        "plan": "docs/approved-plan.md",
        "base_commit": base_commit,
        "runs": [
            {
                "id": "a",
                "objective": "Build A",
                "plan_sections": ["1"],
                "criteria": ["AC-1"],
                "depends_on": [],
                "verification_argv": ["python3", "-m", "unittest"],
            },
            {
                "id": "b",
                "objective": "Build B",
                "plan_sections": ["2"],
                "criteria": ["AC-2"],
                "depends_on": ["a"],
                "verification_argv": ["python3", "-m", "unittest"],
            },
        ],
        "plan_sections": {
            "1": "Build A. AC-1: A works.",
            "2": "Build B. AC-2: B works.",
        },
        "acceptance_criteria": {"AC-1": "A works.", "AC-2": "B works."},
        "functionality_tests": list(functionality_tests),
    }


class PlanGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "tests@example.com")
        git(self.repository, "config", "user.name", "Tests")
        plan = self.repository / "docs" / "approved-plan.md"
        plan.parent.mkdir()
        plan.write_text("Approved PlanGraph plan\n", encoding="utf-8")
        git(self.repository, "add", "docs/approved-plan.md")
        git(self.repository, "commit", "-m", "approved plan")
        self.base_commit = git(self.repository, "rev-parse", "HEAD")
        self.payload = decomposition(self.base_commit)
        self.registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=self.payload,
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph(self, launcher, *, registration=None, **options) -> PlanGraph:
        self.counter += 1

        def correlated(request):
            outcome = launcher(request)
            if outcome.status != "succeeded":
                return outcome
            return replace(
                outcome,
                plan_graph_id=request.plan_graph_id,
                plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id,
                run_dir=str(request.run_dir),
            )

        return PlanGraph(
            self.repository,
            registration or self.registration,
            correlated,
            run_root=self.root / "runs",
            graph_run_id=f"attempt-{self.counter}",
            **options,
        )


    def _transfer_registration(self):
        payload = dict(self.payload)
        payload["runs"] = [
            dict(payload["runs"][0], allowed_paths=["producer.py"]),
            dict(payload["runs"][1], allowed_paths=["consumer.py"]),
        ]
        return register_plan_graph(
            repository=self.repository,
            logical_graph_id="transfer-graph",
            decomposition=payload,
        )

    def test_scope_expanding_finding_is_handed_to_downstream_owner(self) -> None:
        calls = []
        transfer = {
            "key": "consumer.py:wire-producer",
            "file": "producer.py",
            "subject": "wire producer",
            "statement": "Consume the producer receipt.",
            "scope_expanding": True,
            "outcome": "transferred",
            "origin_node": "a",
            "transferred_to": "b",
            "required_paths": ["consumer.py"],
        }

        def launcher(request):
            calls.append(request)
            evidence = (
                {"transferred_findings": [transfer]}
                if request.run.id == "a"
                else {"transferred_findings": []}
            )
            return FeatureRunOutcome(
                "succeeded", f"{request.run.id}-commit", evidence=evidence
            )

        result = self.graph(launcher, registration=self._transfer_registration()).run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(calls[0].finding_transfer_targets, {"consumer.py": "b"})
        self.assertEqual(calls[1].finding_obligations, (transfer,))
        self.assertTrue(calls[1].inherited_ledger_frozen)

    def test_a_directory_grant_resolves_a_transferred_finding_to_its_owner(self) -> None:
        """PlanGraph must validate the claim its own grant map made possible.

        ``_pending_findings`` re-resolves every ``required_path`` of a
        transferred finding and rejects the transfer unless it lands uniquely
        on the node the child named.  It used to look beneath a grant only
        when the grant string ended in ``/``, which the plan contract forbids,
        so a child that correctly routed a file to the owner of a *directory*
        grant had its transfer rejected as unresolvable.
        """

        payload = dict(self.payload)
        payload["runs"] = [
            dict(payload["runs"][0], allowed_paths=["producer.py"]),
            dict(payload["runs"][1], allowed_paths=["web/routes"]),
        ]
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="directory-transfer-graph",
            decomposition=payload,
        )
        transfer = {
            "key": "web/routes/l2.py:wire-producer",
            "file": "producer.py",
            "subject": "wire producer",
            "statement": "Consume the producer receipt.",
            "scope_expanding": True,
            "outcome": "transferred",
            "origin_node": "a",
            "transferred_to": "b",
            "required_paths": ["web/routes/l2.py"],
        }
        calls = []

        def launcher(request):
            calls.append(request)
            evidence = (
                {"transferred_findings": [transfer]}
                if request.run.id == "a"
                else {"transferred_findings": []}
            )
            return FeatureRunOutcome(
                "succeeded", f"{request.run.id}-commit", evidence=evidence
            )

        result = self.graph(launcher, registration=registration).run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(calls[0].finding_transfer_targets, {"web/routes": "b"})
        self.assertEqual(calls[1].finding_obligations, (transfer,))

    def test_blocked_node_carries_its_open_findings_into_graph_state(self) -> None:
        """A blocked node's open findings survive as its own obligations.

        Without this the findings live only in the FeatureRun ledger, and the
        successor attempt re-runs implementation and rediscovers them.
        """

        open_finding = {
            "key": "producer.py:missing-guard",
            "file": "producer.py",
            "subject": "missing guard",
            "statement": "The guard was never added.",
            "requires_disposition": True,
            "outcome": "open",
            "origin_node": "",
            "transferred_to": "",
            "required_paths": ["producer.py"],
        }

        result = self.graph(
            lambda request: FeatureRunOutcome(
                "blocked",
                None,
                evidence={
                    "review_fix": {
                        "status": "blocked",
                        "open_finding_keys": [open_finding["key"]],
                        "open_findings": [open_finding],
                        "transferred_findings": [],
                    }
                },
            ),
            registration=self._transfer_registration(),
        ).run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failed_run_id, "a")
        state = json.loads(
            (self.root / "runs" / "attempt-1" / "checkpoint.json").read_text()
        )["state"]
        carried = state["finding_obligations"]["a"]
        self.assertEqual([item["key"] for item in carried], [open_finding["key"]])
        # Self-carried, not transferred: origin stays on the blocked node so a
        # retry that re-implements the work is not review-frozen.
        self.assertEqual(carried[0]["origin_node"], "a")
        self.assertEqual(carried[0]["transferred_to"], "")

    def test_self_carried_obligations_do_not_freeze_review_discovery(self) -> None:
        """A retry that re-implements must still be allowed to find new work."""

        carried = {
            "key": "producer.py:missing-guard",
            "file": "producer.py",
            "origin_node": "a",
            "transferred_to": "",
            "required_paths": ["producer.py"],
        }
        requests = []

        graph = self.graph(
            lambda request: (
                requests.append(request)
                or FeatureRunOutcome("succeeded", f"{request.run.id}-commit")
            ),
            registration=self._transfer_registration(),
        )
        graph._audit_for_run()
        request = graph._request_for_run(graph.plan.runs[0], self.base_commit, (carried,))

        self.assertEqual(request.finding_obligations, (carried,))
        self.assertFalse(request.inherited_ledger_frozen)

    def test_transfer_to_unbound_owner_fails_closed(self) -> None:
        transfer = {
            "key": "consumer.py:wire-producer",
            "file": "consumer.py",
            "scope_expanding": True,
            "transferred_to": "c",
            "required_paths": ["consumer.py"],
        }
        result = self.graph(
            lambda request: FeatureRunOutcome(
                "succeeded",
                "a-commit",
                evidence={"transferred_findings": [transfer]},
            ),
            registration=self._transfer_registration(),
        ).run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failed_run_id, "a")
        state = json.loads(
            (self.root / "runs" / "attempt-1" / "checkpoint.json").read_text()
        )["state"]
        node = state["nodes"]["a"]
        self.assertEqual(node["status"], "blocked")
        self.assertEqual(
            node["candidate_verified_pending_transfer"]["candidate_commit"], "a-commit"
        )
        self.assertTrue(
            node["candidate_verified_pending_transfer"]["proof_ref"].startswith("artifact:sha256:")
        )
        self.assertIn("evidence_ref", node["evidence"])
        reopened = PlanGraph(
            self.repository,
            self._transfer_registration(),
            lambda request: (_ for _ in ()).throw(AssertionError("terminal graph relaunched")),
            run_root=self.root / "runs",
            graph_run_id="attempt-1",
        ).run()
        self.assertEqual(reopened.status, "blocked")
        self.assertEqual(reopened.candidate_commit, "a-commit")
        def retry_launcher(request):
            return FeatureRunOutcome(
                "succeeded", f"{request.run.id}-retry", evidence={},
                plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
            )

        resumed = PlanGraph.resume(
            self.repository,
            self._transfer_registration(),
            retry_launcher,
            run_root=self.root / "runs",
            directive=RepairResumeDirective(
                "attempt-1", "attempt-1", ("a",), node["evidence"]["evidence_ref"]
            ),
        ).run()
        self.assertEqual(resumed.status, "succeeded")

    def test_terminal_review_payload_transfers_to_downstream_owner(self) -> None:
        calls = []
        transfer = {
            "key": "consumer.py:wire-producer", "file": "producer.py",
            "scope_expanding": True, "transferred_to": "b",
            "required_paths": ["consumer.py"],
        }

        def launcher(request):
            calls.append(request)
            return FeatureRunOutcome(
                "succeeded", f"{request.run.id}-commit",
                evidence={"review_fix": {"transferred_findings": [transfer]}}
                if request.run.id == "a" else {"review_fix": {"transferred_findings": []}},
            )

        result = self.graph(launcher, registration=self._transfer_registration()).run()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(calls[1].finding_obligations, (transfer,))

    def test_same_commit_is_deterministic_across_worktrees(self) -> None:
        worktree = self.root / "worktree"
        git(self.repository, "worktree", "add", "--detach", str(worktree), self.base_commit)
        other = register_plan_graph(
            repository=worktree,
            logical_graph_id="example-graph",
            decomposition=self.payload,
        )
        self.assertEqual(registration_bytes(other), registration_bytes(self.registration))

    def test_every_semantic_change_changes_digest(self) -> None:
        changes = []
        for mutate in (
            lambda value: value["runs"][0].update(objective="Build A more"),
            lambda value: value["runs"].reverse(),
            lambda value: value["runs"][1].update(depends_on=[]),
            lambda value: value["runs"][0].update(verification_argv=["true"]),
            lambda value: value.update(functionality_tests=["true"]),
        ):
            changed = json.loads(json.dumps(self.payload))
            mutate(changed)
            if changed["runs"][0]["objective"] == "Build A more":
                changed["plan_sections"][changed["runs"][0]["plan_sections"][0]] += " Build A more"
            changes.append(changed)
        for changed in changes:
            with self.subTest(changed=changed):
                candidate = register_plan_graph(
                    repository=self.repository,
                    logical_graph_id="example-graph",
                    decomposition=changed,
                )
                self.assertNotEqual(candidate.graph_digest, self.registration.graph_digest)

    def test_identity_is_deliberately_part_of_digest(self) -> None:
        other = register_plan_graph(
            repository=self.repository,
            logical_graph_id="other-graph",
            decomposition=self.payload,
        )
        self.assertNotEqual(other.graph_digest, self.registration.graph_digest)

    def test_registration_reads_git_not_dirty_worktree(self) -> None:
        (self.repository / "docs" / "approved-plan.md").write_text("dirty\n")
        dirty = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=self.payload,
        )
        self.assertEqual(dirty.plan_sha256, self.registration.plan_sha256)
        changed = dict(self.payload, plan="docs/untracked.md")
        (self.repository / "docs" / "untracked.md").write_text("untracked\n")
        with self.assertRaises(PlanGraphError):
            register_plan_graph(
                repository=self.repository,
                logical_graph_id="untracked-graph",
                decomposition=changed,
            )

    def test_invalid_paths_and_ids_are_rejected_before_attempt(self) -> None:
        for plan in ("/absolute.md", "../escape.md", "docs/./approved-plan.md"):
            with self.subTest(plan=plan), self.assertRaises(PlanGraphError):
                register_plan_graph(
                    repository=self.repository,
                    logical_graph_id="example-graph",
                    decomposition=dict(self.payload, plan=plan),
                )
        with self.assertRaises(ValueError):
            register_plan_graph(
                repository=self.repository,
                logical_graph_id="Bad/Graph",
                decomposition=self.payload,
            )
        with self.assertRaises(ValueError):
            PlanGraph(
                self.repository,
                self.registration,
                lambda request: FeatureRunOutcome("failed"),
                run_root=self.root / "runs",
                graph_run_id="../bad",
            )
        self.assertFalse((self.root / "runs").exists())

    def test_persistence_is_canonical_idempotent_and_collision_safe(self) -> None:
        root = self.root / "registrations"
        path = persist_registration(
            repository=self.repository,
            registration_root=root,
            registration=self.registration,
        )
        again = persist_registration(
            repository=self.repository,
            registration_root=root,
            registration=self.registration,
        )
        self.assertEqual(path, again)
        self.assertEqual(load_registration(path), self.registration)
        different = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=dict(self.payload, functionality_tests=["true"]),
        )
        with self.assertRaisesRegex(PlanGraphError, "already registered differently"):
            persist_registration(
                repository=self.repository,
                registration_root=root,
                registration=different,
            )

    def test_concurrent_different_publications_have_one_winner(self) -> None:
        root = self.root / "concurrent-registrations"
        different = register_plan_graph(
            repository=self.repository,
            logical_graph_id="example-graph",
            decomposition=dict(self.payload, functionality_tests=["true"]),
        )

        def publish(registration):
            try:
                persist_registration(
                    repository=self.repository,
                    registration_root=root,
                    registration=registration,
                )
                return "published"
            except PlanGraphError:
                return "collision"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(publish, (self.registration, different))
            )
        self.assertCountEqual(outcomes, ["published", "collision"])
        winner = load_registration(root / "example-graph.json")
        self.assertIn(
            winner.graph_digest,
            {self.registration.graph_digest, different.graph_digest},
        )

    def test_registration_tampering_is_rejected(self) -> None:
        variants = (
            replace(self.registration, protocol="unknown/1"),
            replace(self.registration, graph_digest="0" * 64),
            replace(self.registration, plan_sha256="0" * 64),
            replace(self.registration, definition_json="{ \"runs\": [] }"),
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(PlanGraphError):
                verify_registration(self.repository, variant)
        payload = asdict(self.registration)
        payload["unknown"] = True
        path = self.root / "bad.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(PlanGraphError):
            load_registration(path)

    @patch("harness_labs.plangraph.plan_graph.subprocess.run")
    def test_subprocess_wire_contract_keeps_both_commit_meanings(self, run) -> None:
        request = FeatureRunRequest(
            FEATURE_RUN_REQUEST_PROTOCOL,
            PlanRun("a", "Build A", ("1",), ("AC-1",)),
            "candidate",
            "docs/approved-plan.md",
            self.base_commit,
            self.registration.plan_sha256,
            "attempt-1",
            "a",
            "attempt-1-a",
            self.root / "runs" / "attempt-1-a",
            inherited_ledger_frozen=True,
        )
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "failed"}),
            stderr="",
        )
        SubprocessFeatureRunLauncher(("launcher",))(request)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["protocol"], FEATURE_RUN_REQUEST_PROTOCOL)
        self.assertEqual(payload["base_commit"], "candidate")
        self.assertEqual(payload["plan_base_commit"], self.base_commit)
        self.assertEqual(payload["plan_sha256"], self.registration.plan_sha256)
        self.assertTrue(payload["inherited_ledger_frozen"])

    def test_sequential_candidate_and_registered_functionality_test(self) -> None:
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="with-final-test",
            decomposition=dict(self.payload, functionality_tests=["test final"]),
        )
        calls = []
        tests = []
        result = self.graph(
            lambda request: calls.append((request.run.id, request.base_commit))
            or FeatureRunOutcome("succeeded", f"{request.run.id}-commit"),
            registration=registration,
            functionality_test_runner=lambda command, commit: tests.append((command, commit)),
        ).run()
        self.assertEqual(result.candidate_commit, "b-commit")
        self.assertEqual(calls, [("a", self.base_commit), ("b", "a-commit")])
        self.assertEqual(tests, [("test final", "b-commit")])
        with self.assertRaises(TypeError):
            PlanGraph(
                self.repository,
                registration,
                lambda request: FeatureRunOutcome("failed"),
                run_root=self.root / "runs",
                functionality_tests=("runtime addition",),
            )

    @patch("harness_labs.plangraph.plan_graph.subprocess.run")
    def test_default_functionality_clone_uses_explicit_repository(self, run) -> None:
        run.return_value.returncode = 0
        from harness_labs.plangraph.plan_graph import _run_functionality_test

        _run_functionality_test(self.repository, "true", self.base_commit)
        clone = run.call_args_list[0]
        self.assertEqual(clone.args[0][-2], str(self.repository))

    def test_request_requires_initialized_audit(self) -> None:
        graph = self.graph(lambda request: FeatureRunOutcome("failed"))
        with self.assertRaisesRegex(PlanGraphError, "initialized PlanGraph audit"):
            graph._request_for_run(graph.plan.runs[0], self.base_commit)

    def test_gate_slot_event_guards_reject_invalid_arguments(self) -> None:
        graph = self.graph(lambda request: FeatureRunOutcome("failed"))
        audit = graph._audit_for_run()
        with self.assertRaisesRegex(AuditError, "non-empty node id"):
            audit.gate_slot_acquired("", 1)
        with self.assertRaisesRegex(AuditError, "positive integer"):
            audit.gate_slot_acquired("a", 0)
        with self.assertRaisesRegex(AuditError, "positive integer"):
            audit.gate_slot_acquired("a", -1)
        with self.assertRaisesRegex(AuditError, "positive integer"):
            audit.gate_slot_acquired("a", True)
        with self.assertRaisesRegex(AuditError, "non-empty node id"):
            audit.gate_slot_released("", 1)
        with self.assertRaisesRegex(AuditError, "non-empty node id"):
            audit.gate_slot_bypassed("")

    def test_gate_slot_event_leaves_checkpoint_bound_to_journal_head(self) -> None:
        """A gate slot append must not leave the checkpoint lagging the journal.

        Guards against the checkpoint going stale for the whole duration of a
        slow verification window, which made ``AuditJournal.verify`` see an
        unbound checkpoint until the next per-node transition.
        """
        graph = self.graph(lambda request: FeatureRunOutcome("failed"))
        audit = graph._audit_for_run()
        audit.gate_slot_acquired("a", 1)
        events = [
            json.loads(line)
            for line in audit.journal.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        checkpoint = json.loads(
            audit.journal.checkpoint_path.read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["head_hash"], events[-1]["event_hash"])
        audit.gate_slot_released("a", 1)
        events = [
            json.loads(line)
            for line in audit.journal.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        checkpoint = json.loads(
            audit.journal.checkpoint_path.read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["head_hash"], events[-1]["event_hash"])

    def test_failure_stops_dependents_and_identity_mismatch_fails(self) -> None:
        calls = []
        result = self.graph(
            lambda request: calls.append(request.run.id) or FeatureRunOutcome("failed")
        ).run()
        self.assertEqual((result.status, result.failed_run_id, calls), ("failed", "a", ["a"]))
        self.counter += 1
        mismatch = PlanGraph(
            self.repository,
            self.registration,
            lambda request: FeatureRunOutcome("succeeded", "bad-commit"),
            run_root=self.root / "runs",
            graph_run_id=f"attempt-{self.counter}",
        ).run()
        self.assertEqual(mismatch.status, "failed")

    def test_retired_importer_refuses_without_creating_state(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "import_plan_graph_state.py"
        completed = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("incompatible", completed.stderr)


class _RecordingJudge:
    """An EscalationJudge stub that returns a fixed, schema-valid verdict."""

    def __init__(self, *, identity: str, verdict: str, rationale: str, evidence_refs=()) -> None:
        self.identity = identity
        self.verdict = verdict
        self.rationale = rationale
        self.evidence_refs = tuple(evidence_refs)
        self.calls: list[dict] = []

    def __call__(self, packet):
        self.calls.append(dict(packet))
        return {
            "protocol": ESCALATION_JUDGMENT_PROTOCOL,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "evidence_refs": list(self.evidence_refs),
        }


class _RaisingJudge:
    """An EscalationJudge stub that proves routing never consults a model."""

    def __init__(self, identity: str = "judge-raises") -> None:
        self.identity = identity
        self.calls = 0

    def __call__(self, packet):
        self.calls += 1
        raise AssertionError("escalation judge must not be invoked for this scenario")


def _schema_assert(testcase: unittest.TestCase, instance: object, schema: dict) -> None:
    """A minimal, dependency-free structural check against a JSON Schema.

    Not a general validator -- it only understands the subset
    ``schemas/block-escalation.json`` and
    ``schemas/plan-graph-escalation-judgment.json`` actually use (object
    required/properties, array items, string pattern, const, enum) -- but it
    walks the real schema file on disk rather than re-asserting a hand-copied
    shape, so a schema/code drift would be caught here.
    """

    schema_type = schema.get("type")
    if schema_type == "object":
        testcase.assertIsInstance(instance, dict)
        for key in schema.get("required", ()):
            testcase.assertIn(key, instance)
        for key, subschema in schema.get("properties", {}).items():
            if key in instance:
                _schema_assert(testcase, instance[key], subschema)
    elif schema_type == "array":
        testcase.assertIsInstance(instance, list)
        item_schema = schema.get("items")
        if item_schema:
            for item in instance:
                _schema_assert(testcase, item, item_schema)
    elif schema_type == "string":
        testcase.assertIsInstance(instance, str)
        pattern = schema.get("pattern")
        if pattern:
            testcase.assertRegex(instance, pattern)
    if "const" in schema:
        testcase.assertEqual(instance, schema["const"])
    if "enum" in schema:
        testcase.assertIn(instance, schema["enum"])


class PlanGraphEscalationTests(unittest.TestCase):
    """CC-08 / ADR 0007: in-graph escalation routing, judgment, packet, unseal, cascade."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "tests@example.com")
        git(self.repository, "config", "user.name", "Tests")
        plan = self.repository / "docs" / "approved-plan.md"
        plan.parent.mkdir()
        plan.write_text("Approved PlanGraph plan\n", encoding="utf-8")
        git(self.repository, "add", "docs/approved-plan.md")
        git(self.repository, "commit", "-m", "approved plan")
        self.base_commit = git(self.repository, "rev-parse", "HEAD")
        self.payload = decomposition(self.base_commit)
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def graph(self, launcher, *, registration, **options) -> PlanGraph:
        self.counter += 1

        def correlated(request):
            outcome = launcher(request)
            if outcome.status != "succeeded":
                return outcome
            return replace(
                outcome,
                plan_graph_id=request.plan_graph_id,
                plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id,
                run_dir=str(request.run_dir),
            )

        return PlanGraph(
            self.repository,
            registration,
            correlated,
            run_root=self.root / "runs",
            graph_run_id=f"escalation-attempt-{self.counter}",
            **options,
        )

    def _chain_registration(self, *, logical_id: str, automatic_recovery=None):
        """``a`` (predecessor/owner) with two dependents: ``d`` (an
        independent sibling of the escalating node, unrelated to any
        escalation) and ``b`` (the escalating node). ``d`` is declared before
        ``b``, so ``_ordered_runs`` schedules and seals it first -- the shape
        AC-CC08-15's cascade assertion needs to prove ``d`` gets swept into
        invalidation solely because it depends on the unsealed owner ``a``,
        not because ``d`` itself had anything to do with the escalation.
        """

        payload = dict(self.payload)
        payload["runs"] = [
            {
                "id": "a", "objective": "Build A", "plan_sections": ["1"],
                "criteria": ["AC-1"], "depends_on": [],
                "verification_argv": ["python3", "-m", "unittest"],
                "allowed_paths": ["producer.py"],
            },
            {
                "id": "d", "objective": "Build D", "plan_sections": ["1"],
                "criteria": ["AC-1"], "depends_on": ["a"],
                "verification_argv": ["python3", "-m", "unittest"],
                "allowed_paths": ["sibling.py"],
            },
            {
                "id": "b", "objective": "Build B", "plan_sections": ["2"],
                "criteria": ["AC-2"], "depends_on": ["a"],
                "verification_argv": ["python3", "-m", "unittest"],
                "allowed_paths": ["consumer.py"],
            },
        ]
        return register_plan_graph(
            repository=self.repository,
            logical_graph_id=logical_id,
            decomposition=payload,
            automatic_recovery=automatic_recovery,
        )

    @staticmethod
    def _escalated_record(key: str, required_paths, **overrides) -> dict:
        record = {
            "key": key,
            "file": required_paths[0],
            "anchor_path": required_paths[0],
            "line": None,
            "end_line": None,
            "subject": "out of grant",
            "statement": "This finding needs a path outside my grant.",
            "category": "review",
            "severity": "critical",
            "score": 90,
            "fix_cost": "local",
            "protects": "AC-2",
            "requires_disposition": True,
            "contract_violation": False,
            "scope_expanding": True,
            "outcome": "escalated",
            "outcome_reason": "escalated: required_paths_outside_grant",
            "escalation_reason": "required_paths_outside_grant",
            "cycles_seen": [1],
            "occurrences": 1,
            "source_finding_ids": [key],
            "evidence_refs": [],
            "fix_attempts": [],
            "reopened_count": 0,
            "origin_node": "",
            "transferred_to": "",
            "transfer_eligible": True,
            "required_paths": list(required_paths),
            "anchor_out_of_grant": False,
            "scope_screen_class": "",
            "inherited": False,
        }
        record.update(overrides)
        return record

    # -- AC-CC08-6: full-plan routing, including predecessors -------------

    def test_owner_for_paths_resolves_full_plan_including_predecessors(self) -> None:
        registration = self._chain_registration(logical_id="owner-lookup")
        judge = _RaisingJudge()
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"),
            registration=registration, escalation_judge=judge,
        )

        # "a" is a *predecessor* of no node in this plan and a common
        # ancestor of "b"/"d" -- _transfer_targets_for(b) (dependents-only)
        # would never resolve it, but _owner_for_paths walks the whole plan.
        self.assertEqual(graph._owner_for_paths(("producer.py",)), "a")
        self.assertIsNone(graph._owner_for_paths(("nobody/owns/this.py",)))
        self.assertEqual(judge.calls, 0, "routing must never consult the judge")

    def test_owner_for_paths_refuses_equal_depth_ambiguity(self) -> None:
        payload = dict(self.payload)
        payload["runs"] = [
            dict(payload["runs"][0], allowed_paths=["shared/thing.py"]),
            dict(
                payload["runs"][1], id="z", depends_on=[],
                allowed_paths=["shared/thing.py"],
            ),
        ]
        registration = register_plan_graph(
            repository=self.repository, logical_graph_id="owner-ambiguous",
            decomposition=payload,
        )
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"), registration=registration,
        )
        self.assertIsNone(graph._owner_for_paths(("shared/thing.py",)))

    # -- AC-CC08-7: reviewer-independence refusal --------------------------

    def test_reviewer_independence_refusal_before_judge_invoked(self) -> None:
        registration = self._chain_registration(
            logical_id="reviewer-independence",
            automatic_recovery={
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": ["transfer_ownership"],
                "max_extra_node_launches": 0, "max_structural_decisions": 1,
            },
        )
        # Configured identity equals the escalating node's own id: the only
        # reviewer identity a review-ledger record carries is the node whose
        # review-fix loop raised it.
        judge = _RaisingJudge(identity="b")
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"),
            registration=registration, escalation_judge=judge,
        )
        audit = graph._audit_for_run()
        run_b = next(run for run in graph.plan.runs if run.id == "b")
        outcome = FeatureRunOutcome(
            "succeeded", "b-commit",
            evidence={"review_fix": {"escalated_findings": [
                self._escalated_record("consumer.py:needs-producer", ["producer.py"])
            ]}},
        )

        with self.assertRaisesRegex(PlanGraphError, "independent of the reviewer"):
            graph._resolve_escalations(run_b, outcome, {"a": "a-commit"}, audit)
        self.assertEqual(judge.calls, 0)

    # -- AC-CC08-8: reject write-back ---------------------------------------

    def test_reject_judgment_writes_back_to_escalating_node(self) -> None:
        registration = self._chain_registration(logical_id="reject-writeback")
        judge = _RecordingJudge(
            identity="judge-1", verdict="reject",
            rationale="not a real cross-node dependency",
        )
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"),
            registration=registration, escalation_judge=judge,
        )
        audit = graph._audit_for_run()
        run_b = next(run for run in graph.plan.runs if run.id == "b")
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])
        outcome = FeatureRunOutcome(
            "succeeded", "b-commit",
            evidence={"review_fix": {"escalated_findings": [record]}},
        )
        before = graph.budget.deviation_records()

        disposition = graph._resolve_escalations(run_b, outcome, {"a": "a-commit"}, audit)
        after = graph.budget.deviation_records()

        self.assertEqual(len(disposition.advances), 1)
        target, rejected = disposition.advances[0]
        self.assertEqual(target, "b")
        self.assertEqual(rejected["outcome"], "open")
        self.assertIn("not a real cross-node dependency", rejected["outcome_reason"])
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(after, before, "reject spends no structural decision")

        # Drive the record through the same write _seal_outcome uses (not
        # just inspect the in-memory disposition) into
        # finding_obligations['b'], as the queued-confirm test below does for
        # its own advance.
        finding_obligations = graph._apply_escalation_advances({}, disposition.advances)
        self.assertIn("b", finding_obligations)
        self.assertEqual(len(finding_obligations["b"]), 1)
        self.assertEqual(finding_obligations["b"][0]["key"], record["key"])
        # origin_node must stay "b": this finding is self-carried, not
        # transferred, so b's own retry must not freeze discovery on it.
        self.assertEqual(finding_obligations["b"][0]["origin_node"], "b")

        request = graph._request_for_run(
            run_b, self.base_commit, tuple(finding_obligations["b"]),
        )
        self.assertIs(request.inherited_ledger_frozen, False)
        self.assertIs(request.bounded_fix_only, False)

    def test_rejected_escalation_is_not_re_litigated_on_the_next_launch(self) -> None:
        """A rejected finding is reopened clean by review_fix.py's own ingest
        on the escalating node's next retry and re-escalated with no memory
        of the earlier judgment -- so the durable closure has to come from
        this attempt's own journal, not from anything review_fix.py carries.
        A second escalation of the identical finding_key must not reach the
        judge again and must instead force an ordinary operator block."""

        registration = self._chain_registration(logical_id="reject-no-reloop")
        judge = _RecordingJudge(
            identity="judge-1", verdict="reject",
            rationale="not a real cross-node dependency",
        )
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"),
            registration=registration, escalation_judge=judge,
        )
        audit = graph._audit_for_run()
        run_b = next(run for run in graph.plan.runs if run.id == "b")
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])
        outcome = FeatureRunOutcome(
            "succeeded", "b-commit",
            evidence={"review_fix": {"escalated_findings": [record]}},
        )

        first = graph._resolve_escalations(run_b, outcome, {"a": "a-commit"}, audit)
        self.assertEqual(len(judge.calls), 1)
        self.assertIsNone(first.already_rejected)

        # review_fix.py's seed_transferred + _escalate_out_of_grant reopen
        # this exact finding_key clean and re-escalate it verbatim on b's
        # next launch -- simulated here by resolving the same record again.
        before = graph.budget.deviation_records()
        second = graph._resolve_escalations(run_b, outcome, {"a": "a-commit"}, audit)
        after = graph.budget.deviation_records()

        self.assertEqual(len(judge.calls), 1, "the judge must not be asked again")
        self.assertEqual(after, before, "no structural decision is spent on a repeat")
        self.assertIsNotNone(second.already_rejected)
        self.assertEqual(second.already_rejected["finding_key"], record["key"])
        self.assertEqual(second.already_rejected["owner_node"], "a")

    def test_rejected_escalation_stays_closed_across_a_repair_resume(self) -> None:
        """The reopened, verbatim re-escalation review_fix.py produces on the
        escalating node's next launch is never in the same graph attempt --
        a launch that isn't "succeeded" finalizes the attempt, so the next
        launch of "b" can only happen in a repair successor with its own,
        otherwise-empty ``finding_obligations``. The durable closure has to
        survive that attempt boundary."""

        registration = self._chain_registration(logical_id="reject-no-reloop-resume")
        judge1 = _RecordingJudge(
            identity="judge-1", verdict="reject",
            rationale="not a real cross-node dependency",
        )
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])

        def launcher1(request):
            if request.run.id == "b":
                return FeatureRunOutcome(
                    "blocked",
                    evidence={"review_fix": {"escalated_findings": [record]}},
                )
            return FeatureRunOutcome("succeeded", f"{request.run.id}-commit")

        graph1 = self.graph(launcher1, registration=registration, escalation_judge=judge1)
        result1 = graph1.run()
        self.assertEqual(result1.status, "blocked")
        self.assertEqual(len(judge1.calls), 1)

        checkpoint_state = json.loads(
            (self.root / "runs" / graph1.graph_run_id / "checkpoint.json")
            .read_text(encoding="utf-8")
        )["state"]
        blocker_ref = checkpoint_state["block_escalation_ref"]

        # review_fix.py's ingest reopened the rejected record clean and
        # re-escalated the identical finding_key -- same key, same owner.
        record2 = self._escalated_record("consumer.py:needs-producer", ["producer.py"])
        judge2 = _RaisingJudge()

        def launcher2(request):
            if request.run.id == "b":
                return FeatureRunOutcome(
                    "failed",
                    evidence={"review_fix": {"escalated_findings": [record2]}},
                )
            return FeatureRunOutcome(
                "succeeded", f"{request.run.id}-commit-2",
                plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
            )

        graph2 = PlanGraph.resume(
            self.repository, registration, launcher2,
            run_root=self.root / "runs",
            directive=RepairResumeDirective(
                None, graph1.graph_run_id, ("b",), blocker_ref,
            ),
            escalation_judge=judge2,
        )
        result2 = graph2.run()

        self.assertEqual(result2.status, "blocked")
        self.assertEqual(judge2.calls, 0, "a rejected finding_key must not reach the judge again")
        escalation2_path = self.root / "runs" / graph2.graph_run_id / "escalation.json"
        escalation2 = json.loads(escalation2_path.read_text(encoding="utf-8"))
        self.assertIs(escalation2["decision_request"]["required"], True)
        self.assertIn("already rejected", escalation2["reason"])

    # -- AC-CC08-9: confirm into a queued owner -----------------------------

    def test_confirm_into_queued_owner_injects_obligation_without_authority(self) -> None:
        registration = self._chain_registration(logical_id="confirm-queued")
        judge = _RecordingJudge(
            identity="judge-1", verdict="confirm",
            rationale="legitimate cross-node dependency",
        )
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"),
            registration=registration, escalation_judge=judge,
        )
        audit = graph._audit_for_run()
        run_a = next(run for run in graph.plan.runs if run.id == "a")
        run_b = next(run for run in graph.plan.runs if run.id == "b")
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])
        outcome = FeatureRunOutcome(
            "succeeded", "b-commit",
            evidence={"review_fix": {"escalated_findings": [record]}},
        )
        before = graph.budget.deviation_records()

        # "a" has not run yet in this attempt: it is queued, not sealed.
        disposition = graph._resolve_escalations(run_b, outcome, {}, audit)
        after = graph.budget.deviation_records()

        self.assertEqual(len(disposition.advances), 1)
        target, confirmed = disposition.advances[0]
        self.assertEqual(target, "a")
        self.assertFalse(confirmed["bounded_fix_only"])
        self.assertEqual(after, before)

        request = graph._request_for_run(run_a, self.base_commit, (confirmed,))
        self.assertIs(request.inherited_ledger_frozen, True)
        self.assertIs(request.bounded_fix_only, False)

    # -- AC-CC08-10 / AC-CC08-11: confirm into a sealed owner (unseal) ------

    def test_confirm_into_sealed_owner_unseals_with_exact_budget_events(self) -> None:
        registration = self._chain_registration(
            logical_id="confirm-sealed",
            automatic_recovery={
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": ["transfer_ownership"],
                "max_extra_node_launches": 0, "max_structural_decisions": 1,
            },
        )
        judge = _RecordingJudge(
            identity="judge-1", verdict="confirm",
            rationale="legitimate cross-node dependency",
        )
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"),
            registration=registration, escalation_judge=judge,
        )
        audit = graph._audit_for_run()
        run_a = next(run for run in graph.plan.runs if run.id == "a")
        run_b = next(run for run in graph.plan.runs if run.id == "b")
        with graph.budget._locked(shared=True) as handle:
            before_state = graph.budget._fold(handle)
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])
        outcome = FeatureRunOutcome(
            "succeeded", "b-commit",
            evidence={"review_fix": {"escalated_findings": [record]}},
        )
        before_lines = graph.budget.path.read_text(encoding="utf-8").splitlines()

        disposition = graph._resolve_escalations(run_b, outcome, {"a": "a-commit"}, audit)

        after_lines = graph.budget.path.read_text(encoding="utf-8").splitlines()
        new_events = [json.loads(line) for line in after_lines[len(before_lines):]]
        self.assertEqual(
            [event["event"] for event in new_events],
            ["recovery_decision", "obligation_transferred"],
        )
        decision = new_events[0]["decision"]
        self.assertEqual(decision["action"], "transfer_ownership")
        self.assertEqual(decision["target"], "b")
        self.assertEqual(decision["payload"], {"receiving_node": "a"})
        self.assertEqual(new_events[1]["source_node"], "b")
        self.assertEqual(new_events[1]["receiving_node"], "a")

        with graph.budget._locked(shared=True) as handle:
            after_state = graph.budget._fold(handle)
        self.assertEqual(
            after_state["automatic_recovery_structural_decisions"],
            before_state["automatic_recovery_structural_decisions"] + 1,
        )

        self.assertEqual(disposition.retry_frontier_prefix, ("a", "b"))
        self.assertEqual(len(disposition.advances), 1)
        target, confirmed = disposition.advances[0]
        self.assertEqual(target, "a")
        self.assertIs(confirmed["bounded_fix_only"], True)  # AC-CC08-11

        request = graph._request_for_run(run_a, self.base_commit, (confirmed,))
        self.assertIs(request.bounded_fix_only, True)
        other_request = graph._request_for_run(run_b, self.base_commit, ())
        self.assertIs(other_request.bounded_fix_only, False)

        # A second unseal against max_structural_decisions == 1 raises
        # BudgetError, which _resolve_escalations turns into a block rather
        # than an uncaught exception.
        record2 = self._escalated_record(
            "consumer.py:needs-producer-2", ["producer.py"],
        )
        outcome2 = FeatureRunOutcome(
            "succeeded", "b-commit-2",
            evidence={"review_fix": {"escalated_findings": [record2]}},
        )
        disposition2 = graph._resolve_escalations(run_b, outcome2, {"a": "a-commit"}, audit)
        self.assertIsNotNone(disposition2.budget_error)
        self.assertIn("structural recovery allowance exhausted", disposition2.budget_error)

    def test_failed_launch_and_non_first_terminal_preserve_escalation_disposition(
        self,
    ) -> None:
        """A confirmed unseal must still surface through escalation.json when
        the triggering launch's own outcome is "failed" (not "blocked" or
        "succeeded"), and it must not be discarded when an unrelated sibling
        terminalizes first within the same ready-set attempt."""

        registration = self._chain_registration(
            logical_id="failed-terminal-escalation",
            automatic_recovery={
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": ["transfer_ownership"],
                "max_extra_node_launches": 0, "max_structural_decisions": 1,
            },
        )
        judge = _RecordingJudge(
            identity="judge-1", verdict="confirm",
            rationale="a's file is a genuine cross-node dependency",
        )
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])

        def launcher(request):
            if request.run.id == "a":
                return FeatureRunOutcome("succeeded", "a-commit")
            if request.run.id == "d":
                # An unrelated sibling failure with no escalation of its
                # own -- terminalizing first must not bury b's escalation.
                return FeatureRunOutcome("failed", evidence={"error": "unrelated"})
            # "b": a *failed* launch (not "blocked") that also escalated a
            # finding requiring producer.py, owned by the already-sealed
            # "a" -- and it is admitted concurrently with "d" so it
            # terminalizes second.
            time.sleep(0.05)
            return FeatureRunOutcome(
                "failed",
                evidence={"review_fix": {"escalated_findings": [record]}},
            )

        graph = self.graph(
            launcher, registration=registration, escalation_judge=judge,
            max_parallelism=2,
        )
        result = graph.run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(judge.calls), 1)

        escalation_path = self.root / "runs" / graph.graph_run_id / "escalation.json"
        self.assertTrue(
            escalation_path.exists(),
            "a failed (non-blocked) launch's confirmed unseal must still "
            "produce escalation.json",
        )
        escalation = json.loads(escalation_path.read_text(encoding="utf-8"))
        self.assertEqual(len(escalation["escalations"]), 1)
        self.assertEqual(escalation["escalations"][0]["finding_key"], record["key"])
        self.assertEqual(
            escalation["resume_directive_template"]["retry_frontier"], ["a", "b"],
        )

    def test_second_unseal_against_exhausted_budget_blocks_with_required_decision(
        self,
    ) -> None:
        """AC-CC08-10's exhaustion clause, proven through a real block on a
        resumed attempt rather than only on the ``_EscalationDisposition``
        object: the structural allowance is shared across attempts by the
        plan lineage, so a first attempt's unseal spends it and a resumed
        attempt's second unseal must hit the ordinary graph-blocking path
        with ``decision_request.required is True``."""

        registration = self._chain_registration(
            logical_id="confirm-sealed-exhausted",
            automatic_recovery={
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": ["transfer_ownership"],
                "max_extra_node_launches": 0, "max_structural_decisions": 1,
            },
        )
        judge = _RecordingJudge(
            identity="judge-1", verdict="confirm",
            rationale="a's file is a genuine cross-node dependency",
        )
        record1 = self._escalated_record("consumer.py:needs-producer-1", ["producer.py"])

        def launcher1(request):
            if request.run.id == "b":
                return FeatureRunOutcome(
                    "succeeded", "b-commit-1",
                    evidence={"review_fix": {"escalated_findings": [record1]}},
                )
            return FeatureRunOutcome("succeeded", f"{request.run.id}-commit-1")

        graph1 = self.graph(launcher1, registration=registration, escalation_judge=judge)
        result1 = graph1.run()
        self.assertEqual(result1.status, "blocked")
        self.assertEqual(len(judge.calls), 1)

        checkpoint_state = json.loads(
            (self.root / "runs" / graph1.graph_run_id / "checkpoint.json")
            .read_text(encoding="utf-8")
        )["state"]
        blocker_ref = checkpoint_state["block_escalation_ref"]

        record2 = self._escalated_record("consumer.py:needs-producer-2", ["producer.py"])

        def launcher2(request):
            if request.run.id == "b":
                evidence = {"review_fix": {"escalated_findings": [record2]}}
                commit = "b-commit-2"
            else:
                evidence = {}
                commit = f"{request.run.id}-commit-2"
            return FeatureRunOutcome(
                "succeeded", commit, evidence=evidence,
                plan_graph_id=request.plan_graph_id, plan_node_id=request.plan_node_id,
                feature_run_id=request.feature_run_id, run_dir=str(request.run_dir),
            )

        graph2 = PlanGraph.resume(
            self.repository, registration, launcher2,
            run_root=self.root / "runs",
            directive=RepairResumeDirective(
                None, graph1.graph_run_id, ("a", "b"), blocker_ref,
            ),
            escalation_judge=judge,
        )
        result2 = graph2.run()

        self.assertEqual(result2.status, "blocked")
        self.assertEqual(len(judge.calls), 2)
        escalation2_path = self.root / "runs" / graph2.graph_run_id / "escalation.json"
        self.assertTrue(escalation2_path.exists())
        escalation2 = json.loads(escalation2_path.read_text(encoding="utf-8"))
        self.assertIn("structural recovery allowance exhausted", escalation2["reason"])
        self.assertIs(escalation2["decision_request"]["required"], True)

    # -- AC-CC08-12 / AC-CC08-13 / AC-CC08-15: full-run unseal, artifact,
    #    journal order, and cascade -----------------------------------------

    def test_unseal_produces_valid_artifact_journal_order_and_cascade(self) -> None:
        registration = self._chain_registration(
            logical_id="unseal-integration",
            automatic_recovery={
                "protocol": "plan-graph-automatic-recovery/1",
                "allowed_actions": ["transfer_ownership"],
                "max_extra_node_launches": 0, "max_structural_decisions": 1,
            },
        )
        judge = _RecordingJudge(
            identity="judge-1", verdict="confirm",
            rationale="a's file is a genuine cross-node dependency",
        )
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])

        def launcher(request):
            if request.run.id == "b":
                return FeatureRunOutcome(
                    "succeeded", "b-commit",
                    evidence={"review_fix": {"escalated_findings": [record]}},
                )
            return FeatureRunOutcome("succeeded", f"{request.run.id}-commit")

        graph = self.graph(launcher, registration=registration, escalation_judge=judge)
        result = graph.run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failed_run_id, "b")
        self.assertIn("a", result.completed)
        self.assertIn("d", result.completed)
        self.assertNotIn("b", result.completed)
        self.assertEqual(len(judge.calls), 1)

        escalation_path = self.root / "runs" / graph.graph_run_id / "escalation.json"
        escalation = json.loads(escalation_path.read_text(encoding="utf-8"))
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas" / "block-escalation.json")
            .read_text(encoding="utf-8")
        )
        _schema_assert(self, escalation, schema)  # AC-CC08-12

        self.assertEqual(len(escalation["escalations"]), 1)
        item = escalation["escalations"][0]
        self.assertEqual(item["finding_key"], record["key"])
        self.assertEqual(item["origin_node"], "b")
        self.assertEqual(item["owner_node"], "a")
        self.assertEqual(item["required_paths"], ["producer.py"])
        self.assertRegex(item["judgment_ref"], r"^artifact:sha256:[0-9a-f]{64}$")
        self.assertEqual(
            escalation["resume_directive_template"]["retry_frontier"], ["a", "b"],
        )

        judgment_schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas"
             / "plan-graph-escalation-judgment.json").read_text(encoding="utf-8")
        )
        audit = graph._audit_for_run()
        judgment_artifact = next(
            path for path in (self.root / "runs" / graph.graph_run_id / "artifacts").glob("*.json")
            if "escalation-judgment" in path.name
        )
        _schema_assert(self, json.loads(judgment_artifact.read_text(encoding="utf-8")), judgment_schema)

        # AC-CC08-13: journal order and finding_key identity.
        events = [
            json.loads(line)
            for line in audit.journal.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        escalation_events = [
            event for event in events
            if event["event_type"] in {
                "plan_graph_finding_escalated",
                "plan_graph_escalation_judged",
                "plan_graph_node_unsealed",
            }
        ]
        self.assertEqual(
            [event["event_type"] for event in escalation_events],
            [
                "plan_graph_finding_escalated",
                "plan_graph_escalation_judged",
                "plan_graph_node_unsealed",
            ],
        )
        self.assertTrue(
            all(event["payload"]["finding_key"] == record["key"] for event in escalation_events)
        )

        # No new retry-budget-ledger/1 event kind: re-opening the lineage's
        # ledger and folding it must raise nothing.
        reopened_ledger = RetryBudgetLedger(graph.run_root, registration.plan_lineage_id)
        with reopened_ledger._locked(shared=True) as handle:
            reopened_ledger._fold(handle)

        # AC-CC08-15: resuming with retry_frontier == [owner, escalating]
        # invalidates every transitive dependent of the owner "a", including
        # "d" -- a sibling of "b" that already sealed successfully this
        # attempt and has nothing to do with the escalation, swept in solely
        # because it depends on the unsealed owner.
        checkpoint_state = json.loads(
            (self.root / "runs" / graph.graph_run_id / "checkpoint.json")
            .read_text(encoding="utf-8")
        )["state"]
        blocker_ref = checkpoint_state["block_escalation_ref"]
        selection = audit.repair_selection(
            retry_frontier=("a", "b"), blocker_evidence_ref=blocker_ref,
        )
        self.assertEqual(set(selection["invalidated_node_ids"]), {"a", "b", "d"})
        self.assertNotIn("d", selection["reused_completed"])
        self.assertNotIn("a", selection["reused_completed"])
        self.assertNotIn("b", selection["reused_completed"])

    # -- AC-CC08-14: unrouted escalation blocks for operator assignment -----

    def test_zero_owner_escalation_blocks_for_operator_assignment(self) -> None:
        registration = self._chain_registration(logical_id="unrouted-zero")
        judge = _RaisingJudge()
        record = self._escalated_record(
            "consumer.py:orphan", ["totally/unclaimed/path.py"],
        )

        def launcher(request):
            if request.run.id == "b":
                return FeatureRunOutcome(
                    "succeeded", "b-commit",
                    evidence={"review_fix": {"escalated_findings": [record]}},
                )
            return FeatureRunOutcome("succeeded", f"{request.run.id}-commit")

        graph = self.graph(launcher, registration=registration, escalation_judge=judge)
        before = graph.budget.deviation_records()
        result = graph.run()
        after = graph.budget.deviation_records()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(judge.calls, 0)
        self.assertEqual(after, before)

        escalation = json.loads(
            (self.root / "runs" / graph.graph_run_id / "escalation.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            escalation["decision_request"]["requested_action"], "assign_finding_owner",
        )
        self.assertEqual(escalation["decision_request"]["candidate_actions"], [])

    def test_ambiguous_owner_escalation_lists_candidates(self) -> None:
        payload = dict(self.payload)
        payload["runs"] = [
            {
                "id": "a", "objective": "Build A", "plan_sections": ["1"],
                "criteria": ["AC-1"], "depends_on": [],
                "verification_argv": ["python3", "-m", "unittest"],
                "allowed_paths": ["shared/thing.py"],
            },
            {
                "id": "z", "objective": "Build Z", "plan_sections": ["1"],
                "criteria": ["AC-1"], "depends_on": [],
                "verification_argv": ["python3", "-m", "unittest"],
                "allowed_paths": ["shared/thing.py"],
            },
            {
                "id": "b", "objective": "Build B", "plan_sections": ["2"],
                "criteria": ["AC-2"], "depends_on": ["a", "z"],
                "verification_argv": ["python3", "-m", "unittest"],
                "allowed_paths": ["consumer.py"],
            },
        ]
        registration = register_plan_graph(
            repository=self.repository, logical_graph_id="ambiguous-owner",
            decomposition=payload,
        )
        judge = _RaisingJudge()
        record = self._escalated_record("consumer.py:ambiguous", ["shared/thing.py"])

        def launcher(request):
            if request.run.id == "b":
                return FeatureRunOutcome(
                    "succeeded", "b-commit",
                    evidence={"review_fix": {"escalated_findings": [record]}},
                )
            return FeatureRunOutcome("succeeded", f"{request.run.id}-commit")

        graph = self.graph(launcher, registration=registration, escalation_judge=judge)
        result = graph.run()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(judge.calls, 0)
        escalation = json.loads(
            (self.root / "runs" / graph.graph_run_id / "escalation.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            escalation["decision_request"]["requested_action"], "assign_finding_owner",
        )
        self.assertEqual(
            sorted(escalation["decision_request"]["candidate_actions"]), ["a", "z"],
        )

    # -- Default-off byte identity ------------------------------------------

    def test_escalation_judge_none_is_a_pure_no_op(self) -> None:
        """AC-CC08-1's contract at this layer: escalation_judge=None (the
        default) never routes, judges, or spends authority, regardless of
        what evidence a child reports."""

        registration = self._chain_registration(logical_id="feature-off")
        record = self._escalated_record("consumer.py:needs-producer", ["producer.py"])
        graph = self.graph(
            lambda request: FeatureRunOutcome("failed"), registration=registration,
        )
        audit = graph._audit_for_run()
        run_b = next(run for run in graph.plan.runs if run.id == "b")
        outcome = FeatureRunOutcome(
            "succeeded", "b-commit",
            evidence={"review_fix": {"escalated_findings": [record]}},
        )

        disposition = graph._resolve_escalations(run_b, outcome, {"a": "a-commit"}, audit)

        self.assertEqual(disposition.advances, ())
        self.assertEqual(disposition.escalations_payload, ())
        self.assertIsNone(disposition.unrouted)
        self.assertIsNone(disposition.budget_error)
        self.assertEqual(disposition.retry_frontier_prefix, ())


class _RecordingGateAudit:
    """Minimal stand-in for PlanGraphAudit's gate-slot journaling surface."""

    def __init__(self, *, fail_acquire: bool = False, fail_release: bool = False) -> None:
        self.acquired: list[tuple[str, int]] = []
        self.released: list[tuple[str, int]] = []
        self.fail_acquire = fail_acquire
        self.fail_release = fail_release

    def gate_slot_acquired(self, node_id: str, admitted_concurrency: int) -> None:
        if self.fail_acquire:
            raise AuditError("simulated acquire journal failure")
        self.acquired.append((node_id, admitted_concurrency))

    def gate_slot_released(self, node_id: str, admitted_concurrency: int) -> None:
        if self.fail_release:
            raise AuditError("simulated release journal failure")
        self.released.append((node_id, admitted_concurrency))


class GateSlotTests(unittest.TestCase):
    """Direct unit coverage for GateSlot/GateSlotHold, independent of a graph."""

    def test_holds_serialize_across_threads(self) -> None:
        slot = GateSlot(_RecordingGateAudit())
        active = 0
        max_active = 0
        lock = threading.Lock()

        def worker(node_id: str) -> None:
            nonlocal active, max_active
            with slot.hold(node_id):
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1

        threads = [
            threading.Thread(target=worker, args=(f"n{i}",)) for i in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(max_active, 1)

    def test_hold_exposes_no_slot_or_audit_attribute(self) -> None:
        slot = GateSlot(_RecordingGateAudit())
        hold = slot.hold("a")
        self.assertEqual(set(vars(hold)), {"_acquire", "_release", "_entered"})
        for name in ("slot", "_slot", "audit", "_audit", "node_id", "_node_id"):
            self.assertFalse(hasattr(hold, name), f"hold must not expose {name!r}")

    def test_entered_flag_tracks_actual_acquisition(self) -> None:
        slot = GateSlot(_RecordingGateAudit())
        hold = slot.hold("a")
        self.assertFalse(hold.entered)
        with hold:
            self.assertTrue(hold.entered)
        self.assertTrue(hold.entered)

    def test_acquire_rolls_back_semaphore_on_audit_error(self) -> None:
        audit = _RecordingGateAudit(fail_acquire=True)
        slot = GateSlot(audit)
        hold = slot.hold("a")
        with self.assertRaises(AuditError):
            hold.__enter__()
        self.assertFalse(hold.entered)

        # A failed acquire must not leave the exclusive slot permanently
        # held: a fresh hold has to be able to acquire it promptly.
        audit.fail_acquire = False
        acquired = threading.Event()
        second = slot.hold("b")

        def worker() -> None:
            with second:
                acquired.set()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertTrue(acquired.is_set())

    def test_release_frees_semaphore_even_if_audit_fails(self) -> None:
        audit = _RecordingGateAudit(fail_release=True)
        slot = GateSlot(audit)
        hold = slot.hold("a")
        hold.__enter__()
        with self.assertRaises(AuditError):
            hold.__exit__(None, None, None)

        # The journal-then-release ordering is preserved, but a failed
        # release journal must not leave a sibling blocked forever.
        audit.fail_release = False
        acquired = threading.Event()
        second = slot.hold("b")

        def worker() -> None:
            with second:
                acquired.set()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertTrue(acquired.is_set())

    def test_release_reports_concurrency_of_contending_sibling(self) -> None:
        """Acquire/release concurrency is sampled live, not fixed at admission.

        Guards against the stale value that repeated whatever was computed
        once at admission time for both a node's acquire and release event,
        regardless of how contention actually evolved in between.
        """
        audit = _RecordingGateAudit()
        slot = GateSlot(audit)
        a_holding = threading.Event()
        b_joined = threading.Event()

        def run_a() -> None:
            with slot.hold("a"):
                a_holding.set()
                b_joined.wait(timeout=5)
                time.sleep(0.05)

        def run_b() -> None:
            a_holding.wait(timeout=5)
            b_joined.set()
            with slot.hold("b"):
                pass

        thread_a = threading.Thread(target=run_a)
        thread_b = threading.Thread(target=run_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

        self.assertEqual(audit.acquired, [("a", 1), ("b", 1)])
        # "a" released while "b" was already contending for the slot, so its
        # release event must report 2, not the 1 it was admitted with.
        self.assertEqual(audit.released, [("a", 2), ("b", 1)])


if __name__ == "__main__":
    unittest.main()
