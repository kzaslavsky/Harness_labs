"""Fences for the controller liveness lease the run catalog reads.

``run_catalog._liveness`` has always looked for ``liveness.json`` beside the
journal, and until this suite existed the only writer of that filename in the
repository was ``scripts/dashboard_fixture_run.py``.  Every real non-terminal
run therefore projected as ``liveness_unavailable`` and the dashboard could
not say whether anything was actually running.

Each test below pins one half of the contract: a controller that is running
says so, and a controller that is not never does.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_kernel import RunContract
from harness_labs.core.coordinator_schema import (
    CoordinatorDispatchSchema,
    CoordinatorSegment,
)
from harness_labs.core.controller_liveness import (
    LIVENESS_FILENAME,
    LIVENESS_PROTOCOL,
    ControllerLivenessLease,
)
from harness_labs.featurerun.feature_run import run_feature_worktree
from harness_labs.observability.run_catalog import build_run_catalog
from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    register_plan_graph,
)

SCHEMAS = Path(__file__).parents[1] / "schemas"


def _records(catalog: dict) -> dict[str, dict]:
    return {
        record["run_id"]: record
        for record in catalog["plan_graphs"] + catalog["feature_runs"]
    }


class _LivenessFixture(unittest.TestCase):
    """One throwaway Git repository and run root per test."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repository, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"],
                       cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Tests"], cwd=self.repository, check=True)
        (self.repository / "plan.md").write_text("AC-1", encoding="utf-8")
        subprocess.run(["git", "add", "plan.md"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "--no-gpg-sign", "-m", "plan"],
                       cwd=self.repository, check=True, capture_output=True)
        self.base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repository, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.run_root = self.root / "runs"

    def tearDown(self) -> None:
        self.temp.cleanup()

    # -- helpers ----------------------------------------------------------

    def feature_journal(self, run_id: str) -> AuditJournal:
        """One FeatureRun journal wired the way ``run_feature_worktree`` wires it."""

        journal = AuditJournal(
            self.run_root / run_id, run_id,
            actor=AuditActor("kernel", "controller_kernel"),
            controller_kind="feature_run",
        )
        raw = (json.dumps({
            "protocol": "harness-run-descriptor/1", "run_kind": "feature_run",
            "run_id": run_id, "created_at": "2026-08-19T00:00:00+00:00",
            "objective": "objective", "evidence_classification": "production_lifecycle",
            "repository": {"path": str(self.repository), "base_branch": "main",
                           "base_commit": self.base_commit},
            "approved_plan": None, "parent_correlation": None,
        }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        (journal.run_dir / "descriptor.json").write_bytes(raw)
        journal.append("run_descriptor_bound", status="succeeded",
                       payload={"descriptor_sha256": hashlib.sha256(raw).hexdigest()})
        return journal

    def plan_graph(self, launcher):
        decomposition = {
            "plan": "plan.md", "base_commit": self.base_commit,
            "runs": [{"id": "node", "objective": "AC-1", "plan_sections": ["1"],
                      "criteria": ["AC-1"]}],
            "plan_sections": {"1": "AC-1"}, "acceptance_criteria": {"AC-1": "AC-1"},
        }
        registration = register_plan_graph(
            repository=self.repository, logical_graph_id="logical",
            decomposition=decomposition,
        )
        return PlanGraph(self.repository, registration, launcher,
                         run_root=self.run_root, graph_run_id="attempt")


class ControllerLivenessLeaseTests(_LivenessFixture):
    # -- a running controller says so -------------------------------------

    def test_a_running_plan_graph_attempt_reports_live_to_the_catalog(self) -> None:
        """The whole point: a real, non-terminal attempt is observably alive."""

        observed = {}

        def launcher(request):
            observed["catalog"] = build_run_catalog(self.run_root)
            return FeatureRunOutcome("succeeded", "0" * 40)

        self.plan_graph(launcher).run()

        record = _records(observed["catalog"])["attempt"]
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["liveness"], {"state": "live", "reason": None})

    def test_run_feature_worktree_claims_the_lease_for_its_own_run(self) -> None:
        """The production wiring, not a hand-built journal.

        Sampled from inside the coordinator session, because the lease is
        released the moment the journal is finalized -- which is exactly the
        window in which a dashboard needs to see it.
        """

        observed = {}
        run_dir = self.root / "fr-run"

        def contract_factory(worktree, receipt):
            return RunContract(
                run_id="fr-run", objective="Observe liveness.", phases=("active",),
                criteria=({"id": "seen", "statement": "Seen.", "source": "operator"},),
                terminal_artifact_kinds=("implementation-summary",),
                repository={
                    "path": str(worktree), "branch": receipt["feature_branch"],
                    "base_branch": receipt["base_branch"],
                    "base_commit": receipt["base_commit"],
                },
            )

        lease_path = run_dir.resolve() / LIVENESS_FILENAME

        def profile_builder(worktree, evidence):
            observed["lease"] = lease_path.is_file()
            if observed["lease"]:
                observed["payload"] = json.loads(lease_path.read_text(encoding="utf-8"))
            # Stops the run right after the journal exists; the lease is
            # released the instant the journal is finalized, and this is the
            # window a dashboard actually needs to see.
            return ()

        schema = CoordinatorDispatchSchema(
            "liveness-probe/1",
            (CoordinatorSegment(id="active", phases=("active",),
                                instructions="Do nothing."),),
        )
        with self.assertRaises(Exception) as raised:
            run_feature_worktree(
                base_repository=self.repository, base_branch="main",
                feature_branch="feature/liveness",
                worktree_path=self.root / "fr-worktree", run_dir=run_dir,
                contract_factory=contract_factory, schema=schema,
                session_factory=lambda worktree, launch, evidence: None,
                profile_builder=profile_builder,
                allowed_paths=("plan.md",), commit_message="probe",
            )

        self.assertTrue(observed.get("lease"),
                        f"run_feature_worktree wrote no lease: {raised.exception}")
        self.assertEqual(observed["payload"]["run_id"], "fr-run")
        self.assertEqual(observed["payload"]["controller_kind"], "feature_run")

    def test_a_feature_run_that_escapes_stops_claiming_to_be_alive(self) -> None:
        """An abandoned run must not read ``live`` while its process lives on.

        Launchers run in a ``ThreadPoolExecutor``, so a ``run_feature_worktree``
        that raises escapes into ``PlanGraph``'s launcher-escape handler, which
        records the node failed and keeps the *same* process running.  The
        journal is never finalized, so nothing else releases the lease: the
        run would keep a fresh heartbeat under a genuinely live pid and read
        ``live`` for the rest of the graph.  Neither guard catches that -- the
        pid is real and the heartbeat is current -- so the release has to
        happen on the error path itself.
        """

        run_dir = self.run_root / "escaped"

        def contract_factory(worktree, receipt):
            return RunContract(
                run_id="escaped", objective="Escape.", phases=("active",),
                criteria=({"id": "seen", "statement": "Seen.", "source": "operator"},),
                terminal_artifact_kinds=("implementation-summary",),
                repository={
                    "path": str(worktree), "branch": receipt["feature_branch"],
                    "base_branch": receipt["base_branch"],
                    "base_commit": receipt["base_commit"],
                },
            )

        schema = CoordinatorDispatchSchema(
            "liveness-probe/1",
            (CoordinatorSegment(id="active", phases=("active",),
                                instructions="Do nothing."),),
        )
        with self.assertRaises(Exception):
            run_feature_worktree(
                base_repository=self.repository, base_branch="main",
                feature_branch="feature/escaped",
                worktree_path=self.root / "escaped-worktree", run_dir=run_dir,
                contract_factory=contract_factory, schema=schema,
                session_factory=lambda worktree, launch, evidence: None,
                # Stops the run after the journal -- and its lease -- exist.
                profile_builder=lambda worktree, evidence: (),
                allowed_paths=("plan.md",), commit_message="probe",
            )

        # This process is still alive, exactly as the graph process would be.
        self.assertFalse(
            (run_dir.resolve() / LIVENESS_FILENAME).exists(),
            "the lease outlived the run that escaped",
        )
        record = _records(build_run_catalog(self.run_root))["escaped"]
        self.assertNotEqual(record["status"], "succeeded")
        self.assertEqual(
            record["liveness"],
            {"state": "liveness_unavailable", "reason": "no liveness lease"},
        )

    def test_a_running_feature_run_reports_live_to_the_catalog(self) -> None:
        self.feature_journal("child")

        record = _records(build_run_catalog(self.run_root))["child"]

        self.assertEqual(record["liveness"], {"state": "live", "reason": None})

    def test_the_lease_binds_the_run_it_sits_beside(self) -> None:
        journal = self.feature_journal("child")

        lease = json.loads((journal.run_dir / LIVENESS_FILENAME).read_text(encoding="utf-8"))

        self.assertEqual(lease["protocol"], LIVENESS_PROTOCOL)
        self.assertEqual(lease["run_id"], "child")
        self.assertEqual(lease["controller_kind"], "feature_run")
        self.assertEqual(lease["pid"], os.getpid())
        self.assertTrue(lease["process_start_token"])

    def test_the_lease_validates_against_its_published_schema(self) -> None:
        journal = self.feature_journal("child")
        lease = json.loads((journal.run_dir / LIVENESS_FILENAME).read_text(encoding="utf-8"))
        schema = json.loads(
            (SCHEMAS / "controller-liveness.schema.json").read_text(encoding="utf-8")
        )

        self.assertEqual(set(lease), set(schema["required"]))
        self.assertEqual(set(lease), set(schema["properties"]))

    def test_a_later_heartbeat_advances_the_sequence(self) -> None:
        journal = self.feature_journal("child")
        first = json.loads((journal.run_dir / LIVENESS_FILENAME).read_text())

        journal._liveness.beat()
        second = json.loads((journal.run_dir / LIVENESS_FILENAME).read_text())

        self.assertEqual(second["heartbeat_sequence"], first["heartbeat_sequence"] + 1)
        self.assertEqual(second["controller_instance_id"], first["controller_instance_id"])

    # -- a controller that is not running never says it is ----------------

    def test_a_dead_controller_never_reads_live(self) -> None:
        """A pid alone is not an identity; the start token is the proof.

        A killed controller leaves its last lease on disk.  Its pid is either
        gone or has been recycled by an unrelated process, and both must read
        as not-live rather than as a running run.
        """

        self.feature_journal("child")

        for label, probe in (
            ("pid recycled by another process", lambda pid: "a-different-boot-token"),
            ("pid no longer exists", lambda pid: None),
        ):
            with self.subTest(label):
                record = _records(
                    build_run_catalog(self.run_root, process_probe=probe)
                )["child"]
                self.assertEqual(record["liveness"]["state"], "stale")
                self.assertEqual(
                    record["liveness"]["reason"], "process identity does not match"
                )

    def test_finalizing_a_run_releases_its_lease(self) -> None:
        journal = self.feature_journal("child")
        self.assertTrue((journal.run_dir / LIVENESS_FILENAME).is_file())

        journal.finalize("succeeded", result={"status": "succeeded"})

        self.assertFalse((journal.run_dir / LIVENESS_FILENAME).exists())
        self.assertEqual(
            _records(build_run_catalog(self.run_root))["child"]["liveness"]["state"],
            "terminal",
        )

    def test_a_journal_opened_without_a_controller_kind_claims_nothing(self) -> None:
        """Reading or replaying a run must not publish a heartbeat for it."""

        journal = AuditJournal(
            self.run_root / "child", "child",
            actor=AuditActor("kernel", "controller_kernel"),
        )

        self.assertFalse((journal.run_dir / LIVENESS_FILENAME).exists())
        self.assertIsNone(journal._liveness)

    def test_a_predecessor_reopened_for_reading_does_not_claim_liveness(self) -> None:
        journal = self.feature_journal("child")
        journal.finalize("succeeded", result={"status": "succeeded"})

        AuditJournal.open_existing(
            journal.run_dir, actor=AuditActor("reader", "controller_kernel")
        )

        self.assertFalse((journal.run_dir / LIVENESS_FILENAME).exists())

    def test_stopping_the_lease_is_idempotent(self) -> None:
        lease = ControllerLivenessLease(self.root, "run", "feature_run", interval_seconds=0)
        self.assertTrue((self.root / LIVENESS_FILENAME).is_file())

        lease.stop()
        lease.stop()

        self.assertFalse((self.root / LIVENESS_FILENAME).exists())

    def test_a_lease_rejects_a_kind_the_catalog_cannot_report(self) -> None:
        with self.assertRaises(ValueError):
            ControllerLivenessLease(self.root, "run", "coordinator", interval_seconds=0)


@unittest.skipIf(sys.platform == "win32", "POSIX process identity")
class LivenessAcrossProcessesTests(_LivenessFixture):
    """The realistic failure: the controller process is simply gone."""

    def test_a_lease_left_by_an_exited_process_reads_stale_not_live(self) -> None:
        """No stubbed probe: a real vanished pid, judged by the real probe.

        A ``kill -9`` leaves the last lease on disk with no chance to run an
        atexit sweep.  Only the recorded process-start token can tell that
        lease apart from a live one, which is why the lease records one.
        """

        journal = self.feature_journal("orphan")
        program = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from harness_labs.core.controller_liveness import ControllerLivenessLease\n"
            "ControllerLivenessLease(%r, 'orphan', 'feature_run', interval_seconds=0)\n"
            "os._exit(0)\n"
        ) % (str(Path(__file__).parents[1]), str(journal.run_dir))
        completed = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lease = json.loads(
            (journal.run_dir / LIVENESS_FILENAME).read_text(encoding="utf-8")
        )
        self.assertNotEqual(lease["pid"], os.getpid())

        record = _records(build_run_catalog(self.run_root))["orphan"]

        self.assertEqual(record["status"], "running")
        self.assertEqual(record["liveness"]["state"], "stale")
        self.assertEqual(record["liveness"]["reason"], "process identity does not match")


if __name__ == "__main__":
    unittest.main()
