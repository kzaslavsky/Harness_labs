"""Acceptance coverage for the parameterized PlanGraph autoresume driver.

Every fixture is a real run root: the blocked attempts are produced by running
an actual ``PlanGraph`` against a real git repository, so ``escalation.json``,
``events.jsonl``, ``checkpoint.json``, ``manifest.json`` and the admission
liveness marker are the genuine artifacts the driver has to consume in
production.  Nothing here spawns a campaign -- every launch goes through an
injected runner that only records its argv.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.plangraph.plan_graph import (
    FeatureRunOutcome,
    PlanGraph,
    register_plan_graph,
)
from scripts.plan_graph_autoresume import (
    AutoresumeDriver,
    AutoresumeError,
    NoProgressGuard,
    QuiescenceMonitor,
    blocking_observations,
    find_predecessor,
    reconcile_frontier,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, text=True, capture_output=True, check=False
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


class AutoresumeFixture(unittest.TestCase):
    """A two-node graph whose nodes both block, giving a real run root."""

    node_ids = ("alpha", "beta")
    max_parallelism = 2

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "tests@example.com")
        git(self.repository, "config", "user.name", "Tests")
        (self.repository / "plan.md").write_text("AC-1", encoding="utf-8")
        git(self.repository, "add", "plan.md")
        git(self.repository, "commit", "-m", "plan")
        base_commit = git(self.repository, "rev-parse", "HEAD")
        self.run_root = self.root / "runs"
        decomposition = {
            "plan": "plan.md",
            "base_commit": base_commit,
            "runs": [
                {"id": node_id, "objective": node_id, "plan_sections": ["1"], "criteria": ["AC-1"]}
                for node_id in self.node_ids
            ],
            "plan_sections": {"1": "AC-1"},
            "acceptance_criteria": {"AC-1": "AC-1"},
        }
        self.registration = register_plan_graph(
            repository=self.repository, logical_graph_id="logical", decomposition=decomposition
        )

    def block_attempt(self, attempt_id: str, *, reason: str = "assertion failed") -> Path:
        graph = PlanGraph(
            self.repository, self.registration,
            lambda request: FeatureRunOutcome("blocked", evidence={"error": reason}),
            run_root=self.run_root, graph_run_id=attempt_id,
            max_parallelism=self.max_parallelism,
        )
        self.assertEqual(graph.run().status, "blocked")
        return self.run_root / attempt_id

    def driver(self, **overrides: object) -> AutoresumeDriver:
        arguments: dict[str, object] = {
            "run_root": self.run_root,
            "seed_attempt_id": "attempt",
            "resume_command": ("fake-launcher",),
            "max_attempts": 1,
            "poll_interval": 0.0,
            "sleep": lambda seconds: None,
            "runner": lambda argv: 1,
            "emit": lambda record: self.records.append(dict(record)),
        }
        arguments.update(overrides)
        self.records: list[dict[str, object]] = getattr(self, "records", [])
        return AutoresumeDriver(**arguments)  # type: ignore[arg-type]


class FrontierReconciliationTests(AutoresumeFixture):
    def test_events_recover_a_terminal_node_the_default_template_omits(self) -> None:
        """The documented flag-off under-report must not reach the successor.

        With ``continue_independent_after_block`` off, a drained attempt that
        terminalized both nodes still publishes a single-element
        ``retry_frontier``.  A driver that trusted it would retry one node and
        re-block on the other immediately.
        """
        directory = self.block_attempt("attempt")
        escalation = json.loads((directory / "escalation.json").read_text(encoding="utf-8"))
        self.assertEqual(
            len(escalation["resume_directive_template"]["retry_frontier"]), 1,
            "fixture must reproduce the default single-element frontier",
        )
        predecessor = find_predecessor(self.run_root, "attempt")

        reconciliation = reconcile_frontier(predecessor.escalation, predecessor.events)

        self.assertEqual(set(reconciliation.frontier), {"alpha", "beta"})
        self.assertEqual(reconciliation.frontier[0], escalation["blocked_node_id"])
        omitted = tuple(
            node for node in ("alpha", "beta") if node != escalation["blocked_node_id"]
        )
        self.assertEqual(reconciliation.missing_from_template, omitted)
        self.assertEqual(reconciliation.missing_from_events, ())
        self.assertEqual(reconciliation.discrepancies, 1)

    def test_a_template_node_with_no_failure_event_is_kept_and_reported(self) -> None:
        """Disagreement in the other direction is surfaced, never silently dropped."""
        self.block_attempt("attempt")
        predecessor = find_predecessor(self.run_root, "attempt")
        escalation = dict(predecessor.escalation)
        template = dict(escalation["resume_directive_template"])
        template["retry_frontier"] = [*template["retry_frontier"], "gamma"]
        escalation["resume_directive_template"] = template

        reconciliation = reconcile_frontier(escalation, predecessor.events)

        self.assertIn("gamma", reconciliation.frontier)
        self.assertEqual(reconciliation.missing_from_events, ("gamma",))

    def test_a_node_that_failed_then_sealed_is_not_retried(self) -> None:
        """A ``plan_node_failed`` event alone is not authority to retry."""
        self.block_attempt("attempt")
        predecessor = find_predecessor(self.run_root, "attempt")
        escalation = dict(predecessor.escalation)
        blocker = escalation["blocked_node_id"]
        escalation["nodes"] = [
            dict(node) | ({"status": "succeeded"} if node["node_id"] != blocker else {})
            for node in escalation["nodes"]
        ]

        reconciliation = reconcile_frontier(escalation, predecessor.events)

        self.assertEqual(reconciliation.frontier, (blocker,))
        self.assertEqual(
            set(reconciliation.recovered),
            {node for node in self.node_ids if node != blocker},
        )

    def test_the_driver_logs_the_discrepancy_it_repaired(self) -> None:
        self.block_attempt("attempt")
        self.records = []
        self.driver(dry_run=True).run()
        discrepancies = [r for r in self.records if r.get("event") == "frontier_discrepancy"]
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["discrepancies"], 1)


class QuiescenceTests(AutoresumeFixture):
    def test_a_live_admission_process_defers_the_relaunch(self) -> None:
        """An unfinalized attempt whose recorded process still runs blocks."""
        self.block_attempt("attempt")
        successor = self.run_root / "attempt-successor"
        successor.mkdir()
        (successor / "descriptor.json").write_text(
            json.dumps({"predecessor_attempt_id": "attempt"}), encoding="utf-8"
        )
        (successor / "plan-graph-admission-liveness.json").write_text(
            json.dumps({
                "protocol": "harness-plan-graph-admission-liveness/1",
                "pid": os.getpid(), "process_start_token": "token",
            }),
            encoding="utf-8",
        )
        monitor = QuiescenceMonitor(self.run_root, process_probe=lambda pid: "token")

        blocking = blocking_observations(monitor.observe("attempt"))

        self.assertEqual([item.attempt_id for item in blocking], ["attempt-successor"])
        self.assertEqual(blocking[0].state, "live")

    def test_a_dead_recorded_process_does_not_block(self) -> None:
        self.block_attempt("attempt")
        successor = self.run_root / "attempt-successor"
        successor.mkdir()
        (successor / "descriptor.json").write_text(
            json.dumps({"predecessor_attempt_id": "attempt"}), encoding="utf-8"
        )
        (successor / "plan-graph-admission-liveness.json").write_text(
            json.dumps({
                "protocol": "harness-plan-graph-admission-liveness/1",
                "pid": 4242, "process_start_token": "token",
            }),
            encoding="utf-8",
        )
        monitor = QuiescenceMonitor(self.run_root, process_probe=lambda pid: None)

        self.assertEqual(blocking_observations(monitor.observe("attempt")), ())

    def test_a_reused_pid_is_not_mistaken_for_the_recorded_process(self) -> None:
        """The start token is what makes this safe where a name match is not."""
        self.block_attempt("attempt")
        successor = self.run_root / "attempt-successor"
        successor.mkdir()
        (successor / "descriptor.json").write_text(
            json.dumps({"predecessor_attempt_id": "attempt"}), encoding="utf-8"
        )
        (successor / "plan-graph-admission-liveness.json").write_text(
            json.dumps({
                "protocol": "harness-plan-graph-admission-liveness/1",
                "pid": os.getpid(), "process_start_token": "recorded",
            }),
            encoding="utf-8",
        )
        monitor = QuiescenceMonitor(self.run_root, process_probe=lambda pid: "different")

        self.assertEqual(blocking_observations(monitor.observe("attempt")), ())

    def test_a_finalized_attempt_never_blocks_on_its_own_marker(self) -> None:
        """The fixture's own admission pid is this test process; the manifest wins."""
        self.block_attempt("attempt")
        marker = json.loads(
            (self.run_root / "attempt" / "plan-graph-admission-liveness.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(marker["pid"], os.getpid())
        monitor = QuiescenceMonitor(
            self.run_root, process_probe=lambda pid: marker["process_start_token"]
        )

        self.assertEqual(blocking_observations(monitor.observe("attempt")), ())

    def test_the_driver_waits_and_launches_nothing_until_quiescence(self) -> None:
        self.block_attempt("attempt")
        successor = self.run_root / "attempt-successor"
        successor.mkdir()
        (successor / "descriptor.json").write_text(
            json.dumps({"predecessor_attempt_id": "attempt"}), encoding="utf-8"
        )
        (successor / "plan-graph-admission-liveness.json").write_text(
            json.dumps({
                "protocol": "harness-plan-graph-admission-liveness/1",
                "pid": os.getpid(), "process_start_token": "token",
            }),
            encoding="utf-8",
        )
        launches: list[tuple[str, ...]] = []
        self.records = []
        driver = self.driver(
            runner=lambda argv: launches.append(tuple(argv)) or 0,
            process_probe=lambda pid: "token",
            quiescence_timeout=0.0,
        )

        result = driver.run()

        self.assertEqual(launches, [])
        self.assertEqual(result.status, "externally_blocked")
        self.assertIn("did not become quiescent", result.reason)
        self.assertEqual(result.exit_code, 1)

    def test_the_wait_clears_when_the_process_goes_away(self) -> None:
        self.block_attempt("attempt")
        successor = self.run_root / "attempt-successor"
        successor.mkdir()
        (successor / "descriptor.json").write_text(
            json.dumps({"predecessor_attempt_id": "attempt"}), encoding="utf-8"
        )
        (successor / "plan-graph-admission-liveness.json").write_text(
            json.dumps({
                "protocol": "harness-plan-graph-admission-liveness/1",
                "pid": os.getpid(), "process_start_token": "token",
            }),
            encoding="utf-8",
        )
        polls: list[int] = []

        def probe(pid: int) -> str | None:
            polls.append(pid)
            return "token" if len(polls) < 3 else None

        self.records = []
        driver = self.driver(process_probe=probe)
        observations = driver.wait_for_quiescence()

        self.assertEqual(blocking_observations(observations), ())
        self.assertGreaterEqual(len(polls), 3)
        self.assertEqual(
            [record["event"] for record in self.records],
            ["waiting_for_quiescence", "quiescent"],
        )

    def test_an_unfinalized_attempt_without_a_marker_is_ambiguous_not_dead(self) -> None:
        self.block_attempt("attempt")
        successor = self.run_root / "attempt-successor"
        successor.mkdir()
        (successor / "descriptor.json").write_text(
            json.dumps({"predecessor_attempt_id": "attempt"}), encoding="utf-8"
        )
        monitor = QuiescenceMonitor(self.run_root, process_probe=lambda pid: None)

        blocking = blocking_observations(monitor.observe("attempt"))

        self.assertEqual([item.state for item in blocking], ["ambiguous"])


class NoProgressGuardTests(AutoresumeFixture):
    @staticmethod
    def escalation(reason: str, blocker: str = "alpha") -> dict[str, object]:
        return {"reason": reason, "blocked_node_id": blocker}

    def test_the_guard_fires_on_the_third_identical_escalation(self) -> None:
        guard = NoProgressGuard(3)
        self.assertFalse(guard.observe(self.escalation("same"), ["alpha"]))
        self.assertFalse(guard.observe(self.escalation("same"), ["alpha"]))
        self.assertTrue(guard.observe(self.escalation("same"), ["alpha"]))

    def test_a_differing_reason_resets_the_guard(self) -> None:
        guard = NoProgressGuard(3)
        guard.observe(self.escalation("same"), ["alpha"])
        guard.observe(self.escalation("same"), ["alpha"])
        self.assertFalse(guard.observe(self.escalation("different"), ["alpha"]))
        self.assertEqual(guard.repeats, 1)

    def test_a_differing_frontier_resets_the_guard(self) -> None:
        guard = NoProgressGuard(3)
        guard.observe(self.escalation("same"), ["alpha"])
        guard.observe(self.escalation("same"), ["alpha"])
        self.assertFalse(guard.observe(self.escalation("same"), ["alpha", "beta"]))

    def test_the_driver_stops_relaunching_a_campaign_that_never_moves(self) -> None:
        """Three identical escalations in a row halt the loop below the ceiling."""
        self.block_attempt("attempt")
        launches: list[tuple[str, ...]] = []
        self.records = []
        driver = self.driver(max_attempts=10, runner=lambda argv: launches.append(tuple(argv)) or 1)

        result = driver.run()

        self.assertEqual(result.status, "no_progress")
        self.assertEqual(result.exit_code, 2)
        # Two launches, then the third identical escalation stops the driver.
        self.assertEqual(len(launches), 2)
        self.assertIn("consecutive identical escalations", result.reason)

    def test_differing_escalations_run_to_the_attempt_ceiling_instead(self) -> None:
        """The guard must not be what ends an otherwise-progressing campaign."""
        self.block_attempt("attempt")
        launches: list[tuple[str, ...]] = []
        self.records = []
        driver = self.driver(max_attempts=4, runner=lambda argv: launches.append(tuple(argv)) or 1)
        reasons = iter(["first", "second", "third", "fourth"])
        original = driver.guard.observe

        def observe(escalation, frontier):
            return original(dict(escalation) | {"reason": next(reasons)}, frontier)

        driver.guard.observe = observe  # type: ignore[method-assign]

        result = driver.run()

        self.assertEqual(result.status, "ceiling_reached")
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(len(launches), 4)


class DryRunTests(AutoresumeFixture):
    def test_dry_run_reports_the_argv_and_launches_nothing(self) -> None:
        self.block_attempt("attempt")
        launches: list[tuple[str, ...]] = []
        self.records = []
        driver = self.driver(
            dry_run=True, resume_command=("python3", "run_campaign.py", "run"),
            runner=lambda argv: launches.append(tuple(argv)) or 0,
        )

        result = driver.run()

        self.assertEqual(launches, [])
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.exit_code, 0)
        would = next(record for record in self.records if record["event"] == "would_launch")
        argv = would["argv"]
        self.assertEqual(argv[:3], ["python3", "run_campaign.py", "run"])
        self.assertIn("--resume", argv)
        self.assertIn("--predecessor-attempt-id", argv)
        self.assertEqual(argv[argv.index("--predecessor-attempt-id") + 1], "attempt")
        self.assertEqual(argv.count("--retry-frontier"), 2)
        blocker = argv[argv.index("--blocker-evidence-ref") + 1]
        self.assertTrue(blocker.startswith("artifact:sha256:"), blocker)

    def test_the_command_line_dry_run_exits_zero_and_spawns_nothing(self) -> None:
        self.block_attempt("attempt")
        completed = subprocess.run(
            [sys.executable,
             str(Path(__file__).parents[1] / "scripts" / "plan_graph_autoresume.py"),
             "--run-root", str(self.run_root), "--attempt-id", "attempt",
             "--resume-command", "false", "--dry-run"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(result["launches"]), 1)


class PredecessorSelectionTests(AutoresumeFixture):
    def test_an_attempt_that_already_has_a_successor_is_not_resumed_again(self) -> None:
        self.block_attempt("attempt")
        self.block_attempt("attempt-2")
        (self.run_root / "attempt-2" / "descriptor.json").write_text(
            json.dumps(
                json.loads(
                    (self.run_root / "attempt-2" / "descriptor.json").read_text(encoding="utf-8")
                ) | {"predecessor_attempt_id": "attempt"}
            ),
            encoding="utf-8",
        )

        self.assertEqual(find_predecessor(self.run_root, "attempt").attempt_id, "attempt-2")

    def test_an_unfinalized_lineage_has_no_resumable_predecessor(self) -> None:
        self.block_attempt("attempt")
        # An attempt still in flight has no manifest; ``open_repair_predecessor``
        # refuses it and so must this driver.
        (self.run_root / "attempt" / "manifest.json").unlink()

        with self.assertRaises(AutoresumeError) as raised:
            find_predecessor(self.run_root, "attempt")

        self.assertIn("no finalized lineage leaf", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
