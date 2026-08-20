"""Tests for plan synthesis (DTR-LK-SYN): ledger open findings -> decomposition JSON."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from harness_labs.plangraph.convergence_ledger import ConvergenceLedger
from harness_labs.plangraph.decomposition_conformance import parse_observable
from harness_labs.plangraph.plan_graph_contract import (
    PlanGraphContractError,
    canonical_plan_graph_payload,
)
from harness_labs.plangraph.plan_synthesis import (
    DEFAULT_JOIN_RUN_ID,
    PlanSynthesisError,
    PlanSynthesisResult,
    plan_synthesis,
)


def _finding(file: str, subject: str, required_paths=None, **overrides) -> dict:
    finding = {
        "file": file, "subject": subject,
        "required_paths": list(required_paths or [file]), "confidence": "S",
    }
    finding.update(overrides)
    return finding


def _audit(digest: str, findings) -> dict:
    return {
        "digest": digest, "findings": list(findings),
        "verdicts": [], "confirmed_good": [], "capture_coverage": {},
    }


class _LedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = ConvergenceLedger(Path(self.temporary.name) / "ledger.jsonl")
        self.ledger.open_campaign(
            domain="ui-fidelity",
            target={"kind": "design-doc", "digest": "d" * 8, "snapshot_path": "target.md"},
            base_commit="0" * 40,
        )

    def _synthesis_kwargs(self, **overrides) -> dict:
        kwargs = {
            "plan_path": "docs/plan.md",
            "plan_section_id": "1",
            "plan_section_heading": "## Section 1",
        }
        kwargs.update(overrides)
        return kwargs


class _StubOpenFindingsLedger:
    """Exposes only ``open_findings()`` -- proves ``plan_synthesis`` never
    calls anything else on a ledger-shaped object (never re-folds the
    journal itself)."""

    def __init__(self, findings) -> None:
        self._findings = tuple(findings)

    def open_findings(self):
        return self._findings


class PlanSynthesisContractTests(_LedgerFixture):
    def test_result_is_a_plan_synthesis_result(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        self.assertIsInstance(result, PlanSynthesisResult)

    def test_payload_round_trips_canonical_plan_graph_payload_unchanged(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        self.assertEqual(
            canonical_plan_graph_payload(result.decomposition), result.decomposition
        )

    def test_no_top_level_key_is_invented_beyond_the_closed_set(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        self.assertEqual(
            set(result.decomposition),
            {
                "protocol", "plan", "plan_sections", "acceptance_criteria",
                "runs", "functionality_tests", "referenced_artifacts",
            },
        )

    def test_every_criterion_parses_via_parse_observable(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        acceptance_criteria = result.decomposition["acceptance_criteria"]
        self.assertTrue(acceptance_criteria)
        for text in acceptance_criteria.values():
            observable = parse_observable(text)
            self.assertIsNotNone(observable, text)
            self.assertIn(observable["kind"], {"file", "test_id", "selector", "command"})
            self.assertTrue(observable["referent"])

    def test_every_run_carries_a_path_intent_for_every_grant(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1", required_paths=["a/x.py", "a/y.py"]),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        for run in result.decomposition["runs"]:
            intent_paths = {intent["path"] for intent in run["path_intents"]}
            self.assertEqual(intent_paths, set(run["allowed_paths"]))
            self.assertTrue(run["path_intents"])

    def test_allowed_paths_equal_the_union_of_owned_findings_required_paths(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1", required_paths=["a/x.py"]),
            _finding("a/x.py", "s2", required_paths=["a/x.py", "a/y.py"]),
            _finding("b/z.py", "s3", required_paths=["b/z.py"]),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        join_id = result.join_run_id
        for run in result.decomposition["runs"]:
            if run["id"] == join_id:
                continue
            owned = result.findings_by_run[run["id"]]
            expected = {path for finding in owned for path in finding["required_paths"]}
            self.assertEqual(set(run["allowed_paths"]), expected)

    def test_join_regression_node_id_resolves_the_synthesized_join_run(self) -> None:
        from scripts.run_convergence_campaign import join_regression_node_id

        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"), _finding("c/z.py", "s3"),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        self.assertEqual(
            join_regression_node_id(result.decomposition["runs"]), result.join_run_id
        )
        self.assertEqual(result.join_run_id, DEFAULT_JOIN_RUN_ID)

    def test_validate_round_grants_passes(self) -> None:
        from scripts.run_convergence_campaign import validate_round_grants

        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        # Must not raise.
        validate_round_grants(result.decomposition, result.findings_by_run, result.join_run_id)


class PlanSynthesisGroupingTests(_LedgerFixture):
    """Ownership derives from ``required_paths`` alone."""

    def test_findings_sharing_a_required_path_share_one_run(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1", required_paths=["a/x.py"]),
            _finding("a/x.py", "s2", required_paths=["a/x.py", "a/y.py"]),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        repair_runs = [
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        ]
        self.assertEqual(len(repair_runs), 1)
        self.assertEqual(len(result.findings_by_run[repair_runs[0]["id"]]), 2)

    def test_findings_with_disjoint_required_paths_land_in_different_runs(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        repair_runs = [
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        ]
        self.assertEqual(len(repair_runs), 2)
        for run in repair_runs:
            self.assertEqual(len(result.findings_by_run[run["id"]]), 1)

    def test_a_transitive_chain_of_shared_paths_groups_into_one_run(self) -> None:
        """A shares a path with B, B shares a (different) path with C: all
        three land in the same run even though A and C share nothing
        directly -- ownership is the connected component, not a pairwise
        check."""

        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1", required_paths=["a/x.py", "shared/one.py"]),
            _finding("shared/one.py", "s2", required_paths=["shared/one.py", "shared/two.py"]),
            _finding("shared/two.py", "s3", required_paths=["shared/two.py"]),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        repair_runs = [
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        ]
        self.assertEqual(len(repair_runs), 1)
        self.assertEqual(len(result.findings_by_run[repair_runs[0]["id"]]), 3)

    def test_grouping_is_stable_regardless_of_category_or_severity(self) -> None:
        """Two findings sharing a path still group together even when every
        other attribute differs -- ownership derives from ``required_paths``
        alone, per DTR-LK-SYN."""

        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1", required_paths=["a/x.py"], category="perf", severity="minor"),
            _finding("a/x.py", "s2", required_paths=["a/x.py"], category="security", severity="critical"),
        ]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        repair_runs = [
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        ]
        self.assertEqual(len(repair_runs), 1)


class PlanSynthesisNeverReFoldsTests(unittest.TestCase):
    """``plan_synthesis`` calls exactly one accessor on the ledger it is
    given: :meth:`ConvergenceLedger.open_findings`."""

    def test_a_ledger_shaped_object_exposing_only_open_findings_is_sufficient(self) -> None:
        stub = _StubOpenFindingsLedger([
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ])
        result = plan_synthesis(
            stub, plan_path="docs/plan.md", plan_section_id="1",
            plan_section_heading="## Section 1",
        )
        self.assertEqual(
            canonical_plan_graph_payload(result.decomposition), result.decomposition
        )


class PlanSynthesisErrorTests(_LedgerFixture):
    def test_raises_when_the_ledger_has_no_open_findings(self) -> None:
        with self.assertRaises(PlanSynthesisError):
            plan_synthesis(self.ledger, **self._synthesis_kwargs())

    def test_raises_on_an_unrecognized_observable_kind(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        with self.assertRaises(PlanSynthesisError):
            plan_synthesis(
                self.ledger, **self._synthesis_kwargs(observable_kind="not-a-real-kind")
            )

    def test_raises_when_join_run_id_collides_with_a_repair_run_id(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        with self.assertRaises(PlanSynthesisError):
            plan_synthesis(self.ledger, **self._synthesis_kwargs(join_run_id="repair-1"))

    def test_a_closed_or_excluded_finding_is_not_planned(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ]))
        self.ledger.record_ruling(
            ("a/x.py", "s1"), disposition="waive", statement="not a real bug",
        )
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        owned_files = {
            finding["file"]
            for findings in result.findings_by_run.values()
            for finding in findings
        }
        self.assertEqual(owned_files, {"b/y.py"})


# ---------------------------------------------------------------------------
# DTR-LK-SYN: verification_argv is PATH-relative and overridable
# ---------------------------------------------------------------------------


class PlanSynthesisVerificationArgvTests(_LedgerFixture):
    def test_default_verification_argv_uses_path_relative_python3(self) -> None:
        """Not ``sys.executable``: a synthesized run's recorded gate evidence
        (``plan_approval._executable_evidence``) must resolve via PATH like
        every committed decomposition in this repository, not pin approval
        to this host's own interpreter file."""

        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        for run in result.decomposition["runs"]:
            self.assertEqual(run["verification_argv"][0], "python3")

    def test_default_verification_argv_is_a_real_referent_existence_check(self) -> None:
        """Not a vacuous gate: the default command's exit code depends on
        whether its own observable referent actually exists on disk, so --
        unlike a bare ``pass`` -- it can fail."""

        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        run = next(
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        )
        argv = run["verification_argv"]

        with tempfile.TemporaryDirectory() as missing_cwd:
            missing = subprocess.run(argv, cwd=missing_cwd)
            self.assertNotEqual(missing.returncode, 0)

        with tempfile.TemporaryDirectory() as present_cwd:
            referent = Path(present_cwd) / "a" / "x.py"
            referent.parent.mkdir(parents=True)
            referent.write_text("", encoding="utf-8")
            present = subprocess.run(argv, cwd=present_cwd)
            self.assertEqual(present.returncode, 0)

    def test_verification_argv_builder_overrides_every_synthesized_run(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1"), _finding("b/y.py", "s2"),
        ]))

        def builder(referent: str) -> list:
            return ["python3", "-c", f"print({referent!r})"]

        result = plan_synthesis(
            self.ledger, verification_argv_builder=builder, **self._synthesis_kwargs()
        )
        for run in result.decomposition["runs"]:
            self.assertEqual(run["verification_argv"][0], "python3")
            self.assertIn("print(", run["verification_argv"][2])
        # A caller-supplied builder that keeps the referent reachable still
        # satisfies S5/S6, so the payload still round-trips unchanged.
        self.assertEqual(
            canonical_plan_graph_payload(result.decomposition), result.decomposition
        )


# ---------------------------------------------------------------------------
# DTR-LK-SYN: path_intents actions resolve against the real base_commit
# ---------------------------------------------------------------------------


class _GitRepoLedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "repo"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "plan-synthesis-tests@example.com")
        self._git("config", "user.name", "Plan Synthesis Tests")
        (self.repository / "a").mkdir()
        (self.repository / "a" / "x.py").write_text("existing\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "seed")
        self.base_commit = self._git("rev-parse", "HEAD")

        self.ledger = ConvergenceLedger(Path(self.temporary.name) / "ledger.jsonl")
        self.ledger.open_campaign(
            domain="ui-fidelity",
            target={"kind": "design-doc", "digest": "d" * 8, "snapshot_path": "target.md"},
            base_commit=self.base_commit,
        )

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True, capture_output=True, check=True,
        )
        return completed.stdout.strip()

    def _synthesis_kwargs(self, **overrides) -> dict:
        kwargs = {
            "plan_path": "docs/plan.md",
            "plan_section_id": "1",
            "plan_section_heading": "## Section 1",
        }
        kwargs.update(overrides)
        return kwargs


class PlanSynthesisPathIntentTests(_GitRepoLedgerFixture):
    def test_defaults_to_the_static_guess_without_repository_context(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        result = plan_synthesis(self.ledger, **self._synthesis_kwargs())
        repair = next(
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        )
        self.assertEqual(
            {intent["path"]: intent["action"] for intent in repair["path_intents"]},
            {"a/x.py": "modify"},
        )
        join = next(
            run for run in result.decomposition["runs"] if run["id"] == result.join_run_id
        )
        self.assertEqual(join["path_intents"][0]["action"], "create")

    def test_repository_and_base_commit_resolve_the_real_intent_per_path(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [
            _finding("a/x.py", "s1", required_paths=["a/x.py", "a/new.py"]),
        ]))
        result = plan_synthesis(
            self.ledger, repository=self.repository, base_commit=self.base_commit,
            **self._synthesis_kwargs(),
        )
        repair = next(
            run for run in result.decomposition["runs"] if run["id"] != result.join_run_id
        )
        intents = {intent["path"]: intent["action"] for intent in repair["path_intents"]}
        self.assertEqual(intents, {"a/x.py": "modify", "a/new.py": "create"})
        # A resolved intent is still a valid intent for AC-LK-5's "every
        # grant has a path intent" and the payload still round-trips.
        self.assertEqual(
            canonical_plan_graph_payload(result.decomposition), result.decomposition
        )

    def test_the_join_report_is_a_create_at_round_one_and_a_modify_once_it_exists(
        self,
    ) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        first = plan_synthesis(
            self.ledger, repository=self.repository, base_commit=self.base_commit,
            **self._synthesis_kwargs(),
        )
        join = next(
            run for run in first.decomposition["runs"] if run["id"] == first.join_run_id
        )
        self.assertEqual(join["path_intents"][0]["action"], "create")

        report_path = self.repository / join["allowed_paths"][0]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("round 1 report\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "round 1 report")
        second_base_commit = self._git("rev-parse", "HEAD")

        second = plan_synthesis(
            self.ledger, repository=self.repository, base_commit=second_base_commit,
            **self._synthesis_kwargs(),
        )
        join_two = next(
            run for run in second.decomposition["runs"] if run["id"] == second.join_run_id
        )
        self.assertEqual(join_two["path_intents"][0]["action"], "modify")

    def test_repository_without_base_commit_is_refused(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        with self.assertRaises(PlanSynthesisError):
            plan_synthesis(self.ledger, repository=self.repository, **self._synthesis_kwargs())

    def test_base_commit_without_repository_is_refused(self) -> None:
        self.ledger.ingest_audit(_audit("d1", [_finding("a/x.py", "s1")]))
        with self.assertRaises(PlanSynthesisError):
            plan_synthesis(
                self.ledger, base_commit=self.base_commit, **self._synthesis_kwargs()
            )


if __name__ == "__main__":
    unittest.main()
