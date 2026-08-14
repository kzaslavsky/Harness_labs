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
from harness_labs.plan_graph import (
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

    @patch("harness_labs.plan_graph.subprocess.run")
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

    @patch("harness_labs.plan_graph.subprocess.run")
    def test_default_functionality_clone_uses_explicit_repository(self, run) -> None:
        run.return_value.returncode = 0
        from harness_labs.plan_graph import _run_functionality_test

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
