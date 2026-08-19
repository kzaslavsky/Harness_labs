"""Acceptance tests for the plan-prep refinement loop.

The corpus case is vendored: ``flow-editor-uistreamline`` is the real 26-run
decomposition that produced 36 join-conflict escalations, carried into
``tests/fixtures/plan_graph/`` whole so the test cannot silently skip on a
machine that does not have the campaign worktree.
"""

from __future__ import annotations

import collections
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from harness_labs.plangraph.plan_approval import (
    OPERATOR_APPROVAL_PROTOCOL,
    PlanApprovalError,
    _unclaimed_grant_warnings,
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


def _graph_shape(decomposition) -> dict:
    """Depth and width of the decomposition's dependency graph.

    Parallelism is the thing intent-aware narrowing exists to protect, so the
    tests measure it directly rather than trusting that a warning count of
    zero was reached the cheap way. A run's level is one past its deepest
    dependency; depth is the longest chain (how many waves the controller
    needs) and width is how many runs a wave can dispatch at once.
    """

    runs = {run["id"]: run for run in decomposition["runs"]}
    level: dict[str, int] = {}

    def depth_of(run_id: str) -> int:
        if run_id not in level:
            level[run_id] = 1 + max(
                (depth_of(dependency) for dependency in runs[run_id]["depends_on"]),
                default=0,
            )
        return level[run_id]

    widths = collections.Counter(depth_of(run_id) for run_id in runs)
    return {
        "depth": max(widths),
        "max_width": max(widths.values()),
        "mean_width": round(len(runs) / max(widths), 2),
    }


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
        # 13 of the 17 findings were surplus grant breadth, and narrowing one
        # run's unintended grants dissolves every finding that grant fed; only
        # WP-08/WP-09 -- both declaring intent on the same module -- is real
        # contention, and it is the only thing the judge is asked about.
        kinds = collections.Counter(repair.kind for repair in outcome.applied)
        self.assertEqual(kinds, {"narrow_grant": 13, "serialize": 1})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["finding"]["runs"], ["WP-08", "WP-09"])
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

    def test_narrowing_preserves_the_plans_parallelism(self) -> None:
        """The property the built-in repair exists for.

        Serializing all 17 findings costs three extra waves and two runs off
        the widest one. Intent-aware narrowing reaches the same clean plan
        with the dependency graph bit-for-bit unchanged in shape.
        """

        judge, _ = _serializing_judge()
        before = _graph_shape(canonical_plan_graph_payload(self.decomposition))
        self.assertEqual(before, {"depth": 12, "max_width": 8, "mean_width": 2.17})

        outcome = self._refine(judge=judge)

        self.assertEqual(outcome.status, "clean")
        self.assertEqual(
            _graph_shape(outcome.decomposition), before,
            "refinement flattened the plan's parallelism: "
            f"{_graph_shape(outcome.decomposition)} against {before}",
        )
        # The single serialization is between runs already on different waves,
        # so it costs nothing; every other repair left depends_on alone.
        ordered = [
            run_id
            for run_id, entry in outcome.as_mapping()["decomposition_diff"].items()
            if "depends_on" in entry
        ]
        self.assertEqual(ordered, ["WP-09"])

    def test_no_judge_narrows_but_will_not_serialize(self) -> None:
        outcome = self._refine()

        self.assertEqual(outcome.status, "report_only")
        # Narrowing removes only a permission the run never claimed to need,
        # so it needs nobody's approval; the one genuinely contested finding
        # would change the execution shape and is reported, not applied.
        self.assertEqual(
            collections.Counter(repair.kind for repair in outcome.applied),
            {"narrow_grant": 13},
        )
        self.assertTrue(
            all(repair.decided_by == "deterministic" for repair in outcome.applied)
        )
        self.assertTrue(outcome.revised)
        self.assertEqual(outcome.final_warnings["high"], 1)
        self.assertEqual(
            _graph_shape(outcome.decomposition),
            _graph_shape(canonical_plan_graph_payload(self.decomposition)),
            "the judge-less path changed the plan's execution shape",
        )
        self.assertEqual(
            [dict(item)["runs"] for item in outcome.deferred], [["WP-08", "WP-09"]]
        )
        high = [
            proposal for proposal in outcome.proposals
            if proposal["severity"] == "high"
        ]
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0]["proposed_repair"], "serialize")
        self.assertIn("genuine contention", high[0]["note"])

    def test_report_records_the_diff_from_what_the_operator_wrote(self) -> None:
        judge, _ = _serializing_judge()
        record = self._refine(judge=judge).as_mapping()

        diff = record["decomposition_diff"]
        # WP-08 held a write grant on the catalog module without declaring any
        # intent to touch it; WP-09 declared one. The grant, not the work, was
        # the collision.
        self.assertEqual(
            diff["WP-08"]["allowed_paths"]["removed"],
            ["retinology/web/_l2_document_catalog.py", "tests"],
        )
        self.assertEqual(diff["WP-09"]["depends_on"]["added"], ["WP-08"])
        self.assertTrue(
            all(
                repair["reason"] and repair["decided_by"] in {"judge", "deterministic"}
                for repair in record["applied"]
            )
        )


class SurplusGrantAdvisoryTests(unittest.TestCase):
    """The admission warning that says what narrowing is about to act on."""

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

    def test_every_run_on_the_real_plan_holds_grants_it_never_claimed(self) -> None:
        plan = plan_from_mapping(
            canonical_plan_graph_payload(self.decomposition),
            base_commit=self.fixture["base_commit"],
            repository_id=self.fixture["repository_id"],
            plan_sha256=self.fixture["plan_sha256"],
        )
        grants = [len(run.allowed_paths) for run in plan.runs]
        intents = [len(run.path_intents) for run in plan.runs]
        self.assertEqual(len(plan.runs), 26)
        self.assertAlmostEqual(sum(grants) / len(grants), 3.88, places=2)
        self.assertEqual(sum(intents) / len(intents), 2.0)

        warnings = _unclaimed_grant_warnings(plan)
        self.assertEqual(
            len(warnings), 26,
            "every run on this plan holds at least one unclaimed grant",
        )
        self.assertEqual(
            {record["kind"] for record in warnings},
            {"run-grants-exceed-declared-intents"},
        )
        self.assertEqual(sum(len(record["paths"]) for record in warnings), 57)
        by_run = {record["runs"][0]: record for record in warnings}
        # The node whose objective says its fixes "go back to WP-13-owned
        # CSS/layout" while it holds the write grant on that CSS.
        self.assertIn(
            "retinology/web/static/css/flow_editor.css", by_run["WP-21"]["paths"]
        )
        self.assertTrue(
            all(record["severity"] != "high" for record in warnings),
            "a high-severity advisory would reach the receipt acknowledgement "
            "backstop and the refinement loop's actionable selection",
        )

    def test_the_advisory_predicts_the_narrowing_in_the_diff(self) -> None:
        judge, _ = _serializing_judge()
        record = self._refine(judge=judge).as_mapping()

        advised = {
            str(item["runs"][0]): set(item["paths"])
            for item in record["advisories"]
            if item["kind"] == "run-grants-exceed-declared-intents"
        }
        self.assertEqual(len(advised), 26)
        self.assertTrue(
            all(item["warning_sha256"] for item in record["advisories"])
        )
        narrowed = {
            run_id: set(entry["allowed_paths"]["removed"])
            for run_id, entry in record["decomposition_diff"].items()
            if "allowed_paths" in entry
        }
        self.assertTrue(narrowed)
        for run_id, dropped in narrowed.items():
            self.assertTrue(
                dropped <= advised.get(run_id, set()),
                f"{run_id} lost grants no advisory had named: "
                f"{sorted(dropped - advised.get(run_id, set()))}",
            )

    def test_a_foreign_high_warning_does_not_become_actionable(self) -> None:
        """The kind filter, exercised where its absence would bite.

        The loop selects what it can repair by kind *and* severity. Selecting
        on severity alone would make this fabricated finding actionable, and
        since no repair applies to it the loop would carry it to the round
        ceiling and report ``judgment_only`` on a plan it fully repaired.
        """

        def foreign(plan):
            return [
                {
                    "kind": "some-future-admission-finding",
                    "severity": "high",
                    "runs": ["WP-01", "WP-02"],
                    "paths": ["retinology/web/routes"],
                    "note": "a warning kind this loop has no repair for",
                }
            ]

        judge, _ = _serializing_judge()
        with unittest.mock.patch(
            "harness_labs.plangraph.plan_refinement._unclaimed_grant_warnings",
            foreign,
        ):
            outcome = self._refine(judge=judge)

        self.assertEqual(outcome.status, "clean")
        self.assertEqual(outcome.final_warnings, {"high": 0, "info": 78})
        self.assertEqual(
            collections.Counter(repair.kind for repair in outcome.applied),
            {"narrow_grant": 13, "serialize": 1},
        )

    def test_the_advisory_leaves_the_loop_otherwise_unchanged(self) -> None:
        """The regression that matters: same repairs, same shape, same status."""

        judge, seen = _serializing_judge()
        outcome = self._refine(judge=judge)

        self.assertEqual(outcome.status, "clean")
        self.assertEqual(outcome.initial_warnings, {"high": 17, "info": 102})
        self.assertEqual(outcome.final_warnings, {"high": 0, "info": 78})
        self.assertEqual(
            collections.Counter(repair.kind for repair in outcome.applied),
            {"narrow_grant": 13, "serialize": 1},
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            _graph_shape(outcome.decomposition),
            {"depth": 12, "max_width": 8, "mean_width": 2.17},
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

    @staticmethod
    def _intend(decomposition, **intents):
        """Give named runs their declared ``path_intents``."""

        for run in decomposition["runs"]:
            for path in intents.get(run["id"], ()):
                run["path_intents"].append({"path": path, "action": "modify"})
        return decomposition

    def test_a_run_that_declares_no_intents_is_never_narrowed(self) -> None:
        """Silence is not evidence that a grant is unused.

        ``path_intents`` is optional. A decomposition that omits it would make
        every grant look unintended, and narrowing on that would strip runs of
        the access they need to do their work. B says nothing here, so B keeps
        everything -- even though its grant on the shared module is exactly
        what a naive rule would drop.
        """

        decomposition = self._intend(
            self._decomposition(
                [
                    ("A", (), ("src/shared.py", "src/a.py"), ("AC-1",)),
                    ("B", (), ("src/shared.py", "src/b.py"), ("AC-1",)),
                ]
            ),
            A=("src/shared.py",),
        )
        outcome = self._refine(decomposition)

        self.assertEqual(outcome.applied, ())
        self.assertEqual(
            outcome.as_mapping()["decomposition_diff"], {},
            "a run that declared no path intents was narrowed on silence alone",
        )
        self.assertEqual(outcome.status, "report_only")
        self.assertEqual(outcome.final_warnings["high"], 1)
        self.assertIn("B declares no path intents", outcome.deferred[0]["reason"])

    def test_directory_grant_is_kept_by_an_intent_on_a_file_beneath_it(self) -> None:
        decomposition = self._intend(
            self._decomposition(
                [
                    ("A", (), ("src/pkg", "src/shared.py", "src/a.py"), ("AC-1",)),
                    ("B", (), ("src/pkg", "src/shared.py"), ("AC-1",)),
                ]
            ),
            A=("src/pkg/mod.py", "src/a.py"),
            B=("src/shared.py", "src/pkg/other.py"),
        )
        outcome = self._refine(decomposition)

        self.assertEqual(outcome.status, "clean")
        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(
            outcome.applied[0].detail["dropped"], ["src/shared.py"],
            "the directory grant justifying A's declared intent was dropped too",
        )
        runs = {run["id"]: run for run in outcome.decomposition["runs"]}
        self.assertEqual(runs["A"]["allowed_paths"], ["src/pkg", "src/a.py"])
        self.assertEqual(runs["A"]["depends_on"], [])
        self.assertEqual(runs["B"]["depends_on"], [])

    def test_both_sides_declaring_intent_falls_back_to_serialize(self) -> None:
        decomposition = self._intend(
            self._decomposition(
                [
                    ("A", (), ("src/shared.py",), ("AC-1",)),
                    ("B", (), ("src/shared.py",), ("AC-1",)),
                ]
            ),
            A=("src/shared.py",),
            B=("src/shared.py",),
        )
        judge, seen = _serializing_judge()
        outcome = self._refine(decomposition, judge=judge)

        self.assertEqual(outcome.status, "clean")
        self.assertEqual(
            [repair.kind for repair in outcome.applied], ["serialize"],
            "two runs that both declared intent on the same file were not "
            "serialized; one of them lost a grant it said it needed",
        )
        self.assertEqual(len(seen), 1, "the judge was skipped on real contention")
        runs = {run["id"]: run for run in outcome.decomposition["runs"]}
        self.assertEqual(runs["B"]["depends_on"], ["A"])
        self.assertEqual(runs["A"]["allowed_paths"], ["src/shared.py"])
        self.assertEqual(runs["B"]["allowed_paths"], ["src/shared.py"])

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
        high = [
            warning for warning in prepared.warnings
            if warning["severity"] == "high"
        ]
        self.assertEqual(len(high), 1)
        # This plan declares no path intents anywhere, so admission also
        # reports that once for the whole plan -- advisory, never blocking.
        self.assertEqual(
            [warning["kind"] for warning in prepared.warnings
             if warning["severity"] != "high"],
            ["plan-declares-no-path-intents"],
        )

        with self.assertRaisesRegex(
            PlanApprovalError, "unacknowledged high-severity admission warnings"
        ):
            self._issue(prepared)

        digest = warning_identity(high[0])
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
            [
                warning for warning in prepared.warnings
                if warning["severity"] == "high"
            ],
            [],
            "the refined decomposition still carries high admission warnings",
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
