"""Acceptance tests for the plan-prep refinement loop.

The corpus case is vendored: ``flow-editor-uistreamline`` is the real 26-run
decomposition that produced 36 join-conflict escalations, carried into
``tests/fixtures/plan_graph/`` whole so the test cannot silently skip on a
machine that does not have the campaign worktree.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.plangraph.plan_approval import (
    OPERATOR_APPROVAL_PROTOCOL,
    PlanApprovalError,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_graph import (
    plan_from_mapping,
    validate_plan_graph_plan,
)
from harness_labs.plangraph.plan_graph_contract import canonical_plan_graph_payload
from harness_labs.plangraph.plan_refinement import (
    NoProgressGuard,
    PlanRefinementError,
    _serialize,
    refine_decomposition,
    refine_repository_decomposition,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "plan_graph"
    / "flow-editor-uistreamline-decomposition.json"
)


def _serializing_judge(reason: str = "shared file grant; no ownership split available"):
    """A judge that always orders the pair, recording every request it saw."""

    seen: list[dict] = []

    def judge(request):
        seen.append(dict(request))
        return {"repair": "serialize", "reason": reason}

    return judge, seen


class FlowEditorRefinementTests(unittest.TestCase):
    """The loop against the real defective plan."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.decomposition = cls.fixture["canonical_decomposition"]

    def _refine(self, **overrides):
        return refine_decomposition(
            self.decomposition,
            base_commit=self.fixture["base_commit"],
            repository_id=self.fixture["repository_id"],
            plan_sha256=self.fixture["plan_sha256"],
            **overrides,
        )

    def test_loop_drives_the_real_plan_high_warnings_to_zero(self) -> None:
        judge, seen = _serializing_judge()
        outcome = self._refine(judge=judge)

        self.assertEqual(outcome.initial_warnings, {"high": 17, "info": 102})
        self.assertEqual(
            outcome.final_warnings["high"], 0,
            "the refinement loop left high-severity overlap warnings on the "
            f"flow-editor plan: {[dict(item) for item in outcome.open_warnings]}",
        )
        self.assertEqual(outcome.status, "clean")
        self.assertEqual(len(outcome.applied), 17)
        self.assertEqual(len(seen), 17)
        self.assertTrue(outcome.revised)
        self.assertNotEqual(
            outcome.initial_plan_graph_digest, outcome.final_plan_graph_digest
        )
        # Every round re-prepares, so every round publishes its own identity.
        digests = [entry.plan_graph_digest for entry in outcome.rounds]
        self.assertEqual(len(digests), len(set(digests)))
        # The revised decomposition is a plan the controller would accept.
        validate_plan_graph_plan(
            plan_from_mapping(
                outcome.decomposition,
                base_commit=self.fixture["base_commit"],
                repository_id=self.fixture["repository_id"],
                plan_sha256=self.fixture["plan_sha256"],
            )
        )

    def test_report_only_when_no_judge_is_injected(self) -> None:
        outcome = self._refine()

        self.assertEqual(outcome.status, "report_only")
        self.assertEqual(outcome.applied, ())
        self.assertFalse(outcome.revised)
        self.assertEqual(
            canonical_plan_graph_payload(self.decomposition), outcome.decomposition,
            "report-only refinement edited the decomposition",
        )
        self.assertEqual(len(outcome.proposals), 17 + 102)
        classes = {
            proposal["severity"]: proposal["repair_class"]
            for proposal in outcome.proposals
        }
        self.assertEqual(classes, {"high": "deterministic", "info": "judgment"})

    def test_report_records_the_diff_from_what_the_operator_wrote(self) -> None:
        judge, _ = _serializing_judge()
        record = self._refine(judge=judge).as_mapping()

        diff = record["decomposition_diff"]
        self.assertIn("WP-03", diff)
        self.assertEqual(diff["WP-03"]["depends_on"]["added"], ["WP-02"])
        self.assertTrue(
            all(
                repair["reason"] and repair["decided_by"] in {"judge", "deterministic"}
                for repair in record["applied"]
            )
        )


class RefinementLoopPropertyTests(unittest.TestCase):
    @staticmethod
    def _decomposition(runs, criteria=("AC-1",)):
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "section"},
            "acceptance_criteria": {name: f"{name} holds" for name in criteria},
            "runs": [
                {
                    "id": run_id,
                    "objective": f"objective {run_id}",
                    "plan_sections": ["1"],
                    "criteria": list(run_criteria),
                    "depends_on": list(depends_on),
                    "allowed_paths": list(allowed_paths),
                    "path_intents": [],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                }
                for run_id, depends_on, allowed_paths, run_criteria in runs
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def _refine(self, decomposition, **overrides):
        return refine_decomposition(
            decomposition,
            base_commit="0" * 40,
            repository_id="refinement-tests",
            plan_sha256="a" * 64,
            **overrides,
        )

    def test_clean_plan_passes_through_untouched(self) -> None:
        def judge(request):
            raise AssertionError(f"a clean plan consulted the judge: {request}")

        # A routine shared directory grant: advisory, never a reason to edit.
        decomposition = self._decomposition(
            [
                ("A", (), ("src/a.py", "tests"), ("AC-1",)),
                ("B", (), ("src/b.py", "tests"), ("AC-2",)),
            ],
            criteria=("AC-1", "AC-2"),
        )
        outcome = self._refine(decomposition, judge=judge)

        self.assertEqual(outcome.status, "clean")
        self.assertEqual(outcome.final_warnings, {"high": 0, "info": 1})
        self.assertEqual(outcome.applied, ())
        self.assertFalse(outcome.revised)
        self.assertEqual(
            outcome.as_mapping()["decomposition_diff"], {},
            "the refiner edited a plan that had nothing wrong with it",
        )

    def test_reverse_direction_is_used_when_plan_order_would_cycle(self) -> None:
        decomposition = canonical_plan_graph_payload(
            self._decomposition(
                [
                    ("A", (), ("src/shared.py",), ("AC-1",)),
                    ("B", ("A",), ("src/shared.py",), ("AC-1",)),
                    ("C", ("B",), ("src/shared.py",), ("AC-1",)),
                ]
            )
        )
        # A is already an ancestor of C, so ordering C before A would close a
        # cycle; the reverse direction is the one that survives.
        self.assertEqual(
            _serialize(decomposition, "C", "A"),
            {"edge": {"dependency": "A", "dependent": "C"}},
        )
        runs = {run["id"]: run for run in decomposition["runs"]}
        self.assertEqual(runs["A"]["depends_on"], [])
        self.assertEqual(runs["C"]["depends_on"], ["A", "B"])

    def test_judge_direction_that_would_cycle_falls_back_and_stays_acyclic(self) -> None:
        decomposition = self._decomposition(
            [
                ("A", (), ("src/shared.py",), ("AC-1",)),
                ("B", (), ("src/shared.py",), ("AC-1",)),
                ("C", (), ("src/shared.py",), ("AC-1",)),
            ]
        )
        # Warnings arrive sorted: (A,B), (A,C), (B,C). These directions make C
        # an ancestor of B by the time the third pair is decided, so the third
        # request names an ordering the graph cannot carry.
        directions = {("A", "B"): ("A", "B"), ("A", "C"): ("C", "A"), ("B", "C"): ("B", "C")}

        def judge(request):
            pair = tuple(request["finding"]["runs"])
            first, second = directions[pair]
            return {
                "repair": "serialize",
                "first": first,
                "second": second,
                "reason": f"ordering {first} before {second}",
            }

        outcome = self._refine(decomposition, judge=judge)

        self.assertEqual(outcome.status, "clean")
        runs = {run["id"]: run for run in outcome.decomposition["runs"]}
        self.assertNotIn(
            "B", runs["C"]["depends_on"],
            "the loop inserted the judge's cycle-closing edge",
        )
        self.assertIn("C", runs["B"]["depends_on"])
        validate_plan_graph_plan(
            plan_from_mapping(
                outcome.decomposition,
                base_commit="0" * 40,
                repository_id="refinement-tests",
                plan_sha256="a" * 64,
            )
        )

    def test_no_progress_guard_stops_a_judge_that_changes_nothing(self) -> None:
        decomposition = self._decomposition(
            [
                ("A", (), ("src/shared.py", "docs"), ("AC-1",)),
                ("B", (), ("src/shared.py", "docs"), ("AC-1",)),
            ]
        )
        calls: list[dict] = []

        def judge(request):
            calls.append(dict(request))
            # A grant rewrite that drops and re-adds the same path: legal, and
            # a perfect no-op as far as the overlap analysis is concerned.
            return {
                "repair": "narrow_grant",
                "run": "A",
                "drop_paths": ["docs"],
                "add_paths": ["docs"],
                "reason": "no-op rewrite",
            }

        outcome = self._refine(decomposition, judge=judge, no_progress_threshold=2)

        self.assertEqual(outcome.status, "no_progress")
        self.assertIn("consecutive rounds", outcome.reason)
        self.assertEqual(outcome.final_warnings["high"], 1)
        self.assertLessEqual(
            len(calls), 2,
            "the guard let the judge run past the no-progress threshold",
        )

    def test_judgment_only_terminates_when_the_judge_defers(self) -> None:
        decomposition = self._decomposition(
            [
                ("A", (), ("src/shared.py",), ("AC-1",)),
                ("B", (), ("src/shared.py",), ("AC-1",)),
            ]
        )

        def judge(request):
            return {"repair": "defer", "reason": "the operator owns this split"}

        outcome = self._refine(decomposition, judge=judge)

        self.assertEqual(outcome.status, "judgment_only")
        self.assertEqual(len(outcome.deferred), 1)
        self.assertEqual(outcome.deferred[0]["disposition"], "operator")
        self.assertEqual(outcome.final_warnings["high"], 1)

    def test_judge_decisions_require_a_reason_and_a_known_repair(self) -> None:
        decomposition = self._decomposition(
            [
                ("A", (), ("src/shared.py",), ("AC-1",)),
                ("B", (), ("src/shared.py",), ("AC-1",)),
            ]
        )
        with self.assertRaisesRegex(PlanRefinementError, "non-empty reason"):
            self._refine(decomposition, judge=lambda request: {"repair": "serialize"})
        with self.assertRaisesRegex(PlanRefinementError, "unsupported judge repair"):
            self._refine(
                decomposition,
                judge=lambda request: {"repair": "split_node", "reason": "why not"},
            )

    def test_a_grant_may_not_be_narrowed_out_from_under_a_declared_intent(self) -> None:
        decomposition = self._decomposition(
            [
                ("A", (), ("src/shared.py", "src/own.py"), ("AC-1",)),
                ("B", (), ("src/shared.py",), ("AC-1",)),
            ]
        )
        decomposition["runs"][0]["path_intents"] = [
            {"path": "src/shared.py", "action": "create"}
        ]

        def judge(request):
            return {
                "repair": "narrow_grant",
                "run": "A",
                "drop_paths": ["src/shared.py"],
                "reason": "B owns the file",
            }

        with self.assertRaisesRegex(PlanRefinementError, "path intents outside"):
            self._refine(decomposition, judge=judge)

    def test_guard_counts_consecutive_identical_signatures(self) -> None:
        guard = NoProgressGuard(3)
        self.assertFalse(guard.observe(("a",)))
        self.assertFalse(guard.observe(("b",)))
        self.assertFalse(guard.observe(("b",)))
        self.assertTrue(guard.observe(("b",)))


class RefinedPlanReachesAReceiptTests(unittest.TestCase):
    """Refine, commit, prepare, approve: the whole operator path."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Plan Refinement Test")
        (self.repository / ".harness").mkdir()
        self._write(
            self.repository / ".harness" / "repository.json",
            {
                "protocol": "harness-repository-identity/1",
                "repository_id": "plan-refinement-test-repository",
            },
        )
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "plan.md").write_text(
            "AC-1: shared works.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _overlapping_plan(self) -> dict:
        run = {
            "objective": "edit the shared module",
            "plan_sections": ["1"],
            "criteria": ["AC-1"],
            "depends_on": [],
            "allowed_paths": ["src/shared.py"],
            "path_intents": [],
            "verification_argv": [sys.executable, "-c", "pass"],
            "verification_timeout_seconds": 30,
            "verification_required_paths": [],
        }
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "AC-1: shared works."},
            "acceptance_criteria": {"AC-1": "shared works."},
            "runs": [{**run, "id": "A"}, {**run, "id": "B"}],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def test_issue_refuses_an_unacknowledged_high_warning(self) -> None:
        decomposition = self._commit(self._overlapping_plan())
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "approval",
        )
        self.assertEqual(len(prepared.warnings), 1)
        self.assertEqual(prepared.warnings[0]["severity"], "high")

        with self.assertRaisesRegex(
            PlanApprovalError, "unacknowledged high-severity admission warnings"
        ):
            self._issue(prepared)

        digest = warning_identity(prepared.warnings[0])
        receipt = self._issue(
            prepared,
            acknowledgements=[
                {
                    "warning_sha256": digest,
                    "reason": "A and B edit disjoint hunks; verified by hand",
                }
            ],
        )
        self.assertTrue(receipt.exists())

    def test_acknowledging_an_absent_warning_is_refused(self) -> None:
        decomposition = self._commit(self._overlapping_plan())
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "approval",
        )
        with self.assertRaisesRegex(PlanApprovalError, "absent from gate evidence"):
            self._issue(
                prepared,
                acknowledgements=[
                    {"warning_sha256": "f" * 64, "reason": "blanket bypass attempt"},
                    {
                        "warning_sha256": warning_identity(prepared.warnings[0]),
                        "reason": "reviewed",
                    },
                ],
            )

    def test_refined_plan_needs_no_acknowledgement_at_all(self) -> None:
        decomposition = self._commit(self._overlapping_plan())
        judge, _ = _serializing_judge()
        outcome = refine_repository_decomposition(
            repository=self.repository,
            decomposition_path=decomposition,
            judge=judge,
        )
        self.assertEqual(outcome.status, "clean")

        self._commit(outcome.decomposition, message="refine plan")
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "approval-refined",
        )
        self.assertEqual(
            prepared.warnings, (),
            "the refined decomposition still carries admission warnings",
        )
        # No acknowledgements at all: the defect was repaired, not waived.
        self.assertTrue(self._issue(prepared).exists())

    def test_cli_refine_reports_the_findings_prepare_used_to_bury(self) -> None:
        decomposition = self._commit(self._overlapping_plan())
        report = self.root / "refinement.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts" / "approve_plan.py"),
                "refine",
                str(decomposition),
                "--repository", str(self.repository),
                "--report", str(report),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "report_only")
        self.assertEqual(payload["initial_warnings"]["high"], 1)
        self.assertFalse(payload["revised"])
        record = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(record["proposals"][0]["repair_class"], "deterministic")

    # -- helpers ------------------------------------------------------------

    def _commit(self, payload, message: str = "add plan") -> Path:
        decomposition = self.repository / "decomposition.json"
        self._write(decomposition, copy.deepcopy(payload))
        self._git("add", ".")
        self._git("commit", "-m", message)
        return decomposition

    def _issue(self, prepared, acknowledgements=None) -> Path:
        directory = prepared.subject_path.parent
        operator = directory / "operator.json"
        payload = {
            "protocol": OPERATOR_APPROVAL_PROTOCOL,
            "subject_sha256": prepared.subject_sha256,
            "actor": "test-operator",
            "approved_at": "2026-08-18T00:00:00Z",
            "statement": "I approve this exact subject.",
        }
        if acknowledgements is not None:
            payload["warning_acknowledgements"] = acknowledgements
        self._write(operator, payload)
        receipt = directory / "receipt.json"
        issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator,
            receipt_path=receipt,
        )
        return receipt

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    @staticmethod
    def _write(path: Path, payload) -> None:
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
