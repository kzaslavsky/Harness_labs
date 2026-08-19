"""Unit and admission-integration tests for the S1-S10 conformance analyzer."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness_labs.plangraph.decomposition_conformance import (
    CONFORMANCE_PROTOCOL,
    GRADE_PROPOSAL,
    MAX_FAN_IN,
    S1_WRITABLE_PATH_OVERLAP,
    S2_DIRECTORY_GRANT,
    S3_SERIALIZATION_PROPOSAL,
    S4_GRANT_OUTSIDE_OBJECTIVE,
    S5_MISSING_OBSERVABLE,
    S6_UNREACHABLE_OBSERVABLE,
    S7_EXIT_CHECK_OUTSIDE_GRANTS,
    S8_GATE_LARGER_THAN_CRITERIA,
    S9_FAN_IN_JOIN_PROPOSAL,
    S10_NODE_COUNT,
    DecompositionConformanceError,
    analyze_decomposition,
    conformance_judge,
    intermediate_join_proposal,
    is_conformance_aware,
    parse_observable,
    validate_conformance_report,
)
from harness_labs.plangraph.plan_approval import (
    OPERATOR_APPROVAL_PROTOCOL,
    PlanApprovalError,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_graph import (
    PathIntent,
    PlanGraphPlan,
    PlanRun,
    RequiredPath,
    VerificationGate,
)
from harness_labs.plangraph.plan_refinement import refine_repository_decomposition


def _run(
    run_id: str,
    *,
    allowed_paths=(),
    depends_on=(),
    path_intents=(),
    criteria=(),
    verification_argv=(),
    verification_required_paths=(),
    verification_gates=(),
) -> PlanRun:
    return PlanRun(
        id=run_id,
        objective=f"objective {run_id}",
        plan_sections=(run_id,),
        criteria=tuple(criteria),
        depends_on=tuple(depends_on),
        allowed_paths=tuple(allowed_paths),
        path_intents=tuple(path_intents),
        verification_argv=tuple(verification_argv),
        verification_required_paths=tuple(verification_required_paths),
        verification_gates=tuple(verification_gates),
    )


def _plan(runs) -> PlanGraphPlan:
    return PlanGraphPlan(
        plan="PLAN.md",
        base_commit="0" * 40,
        runs=tuple(runs),
        plan_sections={},
        acceptance_criteria={},
    )


class ParseObservableTests(unittest.TestCase):
    def test_parses_a_well_formed_annotation(self) -> None:
        text = 'the button renders. OBSERVABLE:{"kind": "file", "referent": "app/index.html"}'
        self.assertEqual(
            parse_observable(text), {"kind": "file", "referent": "app/index.html"}
        )

    def test_absent_marker_is_none(self) -> None:
        self.assertIsNone(parse_observable("plain prose, no annotation at all"))

    def test_invalid_json_is_none(self) -> None:
        self.assertIsNone(parse_observable("text OBSERVABLE:{not json at all}"))

    def test_unknown_kind_is_none(self) -> None:
        self.assertIsNone(
            parse_observable('text OBSERVABLE:{"kind": "screenshot", "referent": "x"}')
        )

    def test_empty_referent_is_none(self) -> None:
        self.assertIsNone(
            parse_observable('text OBSERVABLE:{"kind": "file", "referent": ""}')
        )

    def test_non_string_input_is_none(self) -> None:
        self.assertIsNone(parse_observable(None))


class ConformanceAwarenessTests(unittest.TestCase):
    def test_true_when_any_criterion_declares_an_observable(self) -> None:
        canonical = {
            "acceptance_criteria": {
                "AC-1": "plain prose",
                "AC-2": 'x OBSERVABLE:{"kind": "command", "referent": "pytest"}',
            }
        }
        self.assertTrue(is_conformance_aware(canonical))

    def test_false_without_any_observable(self) -> None:
        canonical = {"acceptance_criteria": {"AC-1": "plain prose"}}
        self.assertFalse(is_conformance_aware(canonical))

    def test_false_without_an_acceptance_criteria_mapping(self) -> None:
        self.assertFalse(is_conformance_aware({}))


class S1AndS3Tests(unittest.TestCase):
    def test_s1_blocks_file_granularity_overlap_when_enforced(self) -> None:
        plan = _plan(
            [
                _run("A", allowed_paths=["src/shared.py"]),
                _run("B", allowed_paths=["src/shared.py"]),
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(len(report.block_violations), 1)
        self.assertEqual(report.block_violations[0].kind, S1_WRITABLE_PATH_OVERLAP)
        self.assertEqual(report.block_violations[0].runs, ("A", "B"))

    def test_s1_blocks_directory_granularity_overlap(self) -> None:
        plan = _plan(
            [
                _run("A", allowed_paths=["tests"], path_intents=[PathIntent(path="tests", action="create")]),
                _run("B", allowed_paths=["tests/test_x.py"]),
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertIn(
            S1_WRITABLE_PATH_OVERLAP,
            [finding.kind for finding in report.block_violations],
        )

    def test_s1_dependency_ordered_runs_do_not_violate(self) -> None:
        plan = _plan(
            [
                _run("A", allowed_paths=["src/shared.py"]),
                _run("B", allowed_paths=["src/shared.py"], depends_on=["A"]),
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(report.block_violations, ())

    def test_s1_finding_is_present_but_unenforced_by_default_on_a_legacy_plan(self) -> None:
        plan = _plan(
            [
                _run("A", allowed_paths=["src/shared.py"]),
                _run("B", allowed_paths=["src/shared.py"]),
            ]
        )
        report = analyze_decomposition(plan, {})
        self.assertFalse(report.conformance_aware)
        self.assertFalse(report.enforced)
        self.assertEqual(report.block_violations, ())
        overlap = next(f for f in report.findings if f.kind == S1_WRITABLE_PATH_OVERLAP)
        self.assertFalse(overlap.enforced)

    def test_s3_proposes_serialization_alongside_every_s1_finding(self) -> None:
        plan = _plan(
            [
                _run("A", allowed_paths=["src/shared.py"]),
                _run("B", allowed_paths=["src/shared.py"]),
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        proposal = next(f for f in report.findings if f.kind == S3_SERIALIZATION_PROPOSAL)
        self.assertEqual(proposal.grade, GRADE_PROPOSAL)
        self.assertEqual(proposal.proposal["repair"], "serialize")
        self.assertEqual(proposal.proposal["dependency"], "A")
        self.assertEqual(proposal.proposal["dependent"], "B")
        # Proposals never block, even when enforced.
        self.assertNotIn(proposal.kind, {item.kind for item in report.block_violations})
        self.assertIn(proposal.as_mapping(), report.as_mapping()["proposals"])


class S2Tests(unittest.TestCase):
    def test_blocks_a_directory_grant(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src"])])
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations], [S2_DIRECTORY_GRANT]
        )

    def test_allows_a_file_shaped_grant(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src/a.py"])])
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(report.block_violations, ())

    def test_allows_an_extensionless_grant_with_a_matching_create_intent(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["scripts/mytool"],
                    path_intents=[PathIntent(path="scripts/mytool", action="create")],
                )
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertFalse(any(f.kind == S2_DIRECTORY_GRANT for f in report.findings))

    def test_extensionless_grant_without_a_create_intent_still_blocks(self) -> None:
        plan = _plan([_run("A", allowed_paths=["scripts/mytool"])])
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations], [S2_DIRECTORY_GRANT]
        )


class S4Tests(unittest.TestCase):
    def test_blocks_a_grant_not_covered_by_declared_intent(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py", "src/b.py"],
                    path_intents=[PathIntent(path="src/a.py", action="modify")],
                )
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations],
            [S4_GRANT_OUTSIDE_OBJECTIVE],
        )
        self.assertEqual(report.block_violations[0].paths, ("src/b.py",))

    def test_silent_when_the_run_declares_no_intents_at_all(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src/a.py", "src/b.py"])])
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertFalse(any(f.kind == S4_GRANT_OUTSIDE_OBJECTIVE for f in report.findings))

    def test_silent_when_every_grant_is_covered(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py"],
                    path_intents=[PathIntent(path="src/a.py", action="modify")],
                )
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertFalse(any(f.kind == S4_GRANT_OUTSIDE_OBJECTIVE for f in report.findings))


class S5AndS6Tests(unittest.TestCase):
    def test_s5_blocks_a_criterion_with_no_observable(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src/a.py"], criteria=["AC-1"])])
        canonical = {"acceptance_criteria": {"AC-1": "plain prose, no annotation"}}
        report = analyze_decomposition(plan, canonical, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations], [S5_MISSING_OBSERVABLE]
        )
        self.assertEqual(report.block_violations[0].criterion, "AC-1")

    def test_s6_blocks_a_referent_outside_the_nodes_grants(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py"],
                    criteria=["AC-1"],
                    verification_argv=[sys.executable, "-c", "pass", "src/other.py"],
                )
            ]
        )
        canonical = {
            "acceptance_criteria": {
                "AC-1": 'works. OBSERVABLE:{"kind": "file", "referent": "src/other.py"}'
            }
        }
        report = analyze_decomposition(plan, canonical, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations],
            [S6_UNREACHABLE_OBSERVABLE],
        )

    def test_s6_blocks_a_referent_unreachable_from_verification_argv(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py"],
                    criteria=["AC-1"],
                    verification_argv=[sys.executable, "-c", "pass"],
                )
            ]
        )
        canonical = {
            "acceptance_criteria": {
                "AC-1": 'works. OBSERVABLE:{"kind": "file", "referent": "src/a.py"}'
            }
        }
        report = analyze_decomposition(plan, canonical, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations],
            [S6_UNREACHABLE_OBSERVABLE],
        )

    def test_clean_when_referent_is_granted_and_reachable(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py"],
                    criteria=["AC-1"],
                    verification_argv=[sys.executable, "src/a.py"],
                )
            ]
        )
        canonical = {
            "acceptance_criteria": {
                "AC-1": 'works. OBSERVABLE:{"kind": "file", "referent": "src/a.py"}'
            }
        }
        report = analyze_decomposition(plan, canonical, enforce=True)
        self.assertEqual(report.block_violations, ())

    def test_non_file_kind_only_checks_argv_reachability(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py"],
                    criteria=["AC-1"],
                    verification_argv=["pytest", "tests/test_a.py::test_ok"],
                )
            ]
        )
        canonical = {
            "acceptance_criteria": {
                "AC-1": (
                    'works. OBSERVABLE:{"kind": "test_id", '
                    '"referent": "tests/test_a.py::test_ok"}'
                )
            }
        }
        report = analyze_decomposition(plan, canonical, enforce=True)
        self.assertEqual(report.block_violations, ())

    def test_a_gate_tuples_argv_also_counts_toward_reachability(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["src/a.py"],
                    criteria=["AC-1"],
                    verification_gates=[
                        VerificationGate(name="g1", argv=["pytest", "src/a.py"])
                    ],
                )
            ]
        )
        canonical = {
            "acceptance_criteria": {
                "AC-1": 'works. OBSERVABLE:{"kind": "file", "referent": "src/a.py"}'
            }
        }
        report = analyze_decomposition(plan, canonical, enforce=True)
        self.assertEqual(report.block_violations, ())


class S7Tests(unittest.TestCase):
    def test_blocks_a_required_path_from_a_non_ancestor_producer(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["gen.py"],
                    path_intents=[PathIntent(path="gen.py", action="create")],
                ),
                _run(
                    "B",
                    allowed_paths=["b.txt"],
                    verification_required_paths=[
                        RequiredPath(path="gen.py", availability="created_by", producer_run_id="A")
                    ],
                ),
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(
            [finding.kind for finding in report.block_violations],
            [S7_EXIT_CHECK_OUTSIDE_GRANTS],
        )

    def test_allows_a_required_path_inherited_from_an_ancestor(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["gen.py"],
                    path_intents=[PathIntent(path="gen.py", action="create")],
                ),
                _run(
                    "B",
                    allowed_paths=["b.txt"],
                    depends_on=["A"],
                    verification_required_paths=[
                        RequiredPath(path="gen.py", availability="created_by", producer_run_id="A")
                    ],
                ),
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(report.block_violations, ())

    def test_ignores_base_availability_required_paths(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["a.py"],
                    verification_required_paths=[
                        RequiredPath(path="README.md", availability="base")
                    ],
                )
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertEqual(report.block_violations, ())


class S8Tests(unittest.TestCase):
    def test_warns_when_the_gate_tuple_exceeds_the_criteria_set(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["a.py"],
                    criteria=["AC-1"],
                    verification_gates=[
                        VerificationGate(name="g1", argv=["true"]),
                        VerificationGate(name="g2", argv=["true"]),
                    ],
                )
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        warn = [f for f in report.findings if f.kind == S8_GATE_LARGER_THAN_CRITERIA]
        self.assertEqual(len(warn), 1)
        self.assertTrue(warn[0].enforced)
        entries = report.warning_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["severity"], "high")
        self.assertEqual(entries[0]["kind"], S8_GATE_LARGER_THAN_CRITERIA)

    def test_unenforced_gate_size_finding_does_not_appear_as_a_warning(self) -> None:
        plan = _plan(
            [
                _run(
                    "A",
                    allowed_paths=["a.py"],
                    criteria=["AC-1"],
                    verification_gates=[
                        VerificationGate(name="g1", argv=["true"]),
                        VerificationGate(name="g2", argv=["true"]),
                    ],
                )
            ]
        )
        report = analyze_decomposition(plan, {})
        self.assertEqual(report.warning_entries(), [])

    def test_silent_for_a_flat_single_gate_run(self) -> None:
        plan = _plan(
            [
                _run(
                    "A", allowed_paths=["a.py"], criteria=["AC-1"], verification_argv=["true"]
                )
            ]
        )
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertFalse(any(f.kind == S8_GATE_LARGER_THAN_CRITERIA for f in report.findings))


class S9Tests(unittest.TestCase):
    def _fan_in_plan(self, terminal: bool) -> PlanGraphPlan:
        leaves = [_run(name, allowed_paths=[f"{name}.py"]) for name in ("A", "B", "C", "D")]
        join = _run("E", allowed_paths=["e.py"], depends_on=["A", "B", "C", "D"])
        runs = [*leaves, join]
        if not terminal:
            runs.append(_run("F", allowed_paths=["f.py"], depends_on=["E"]))
        return _plan(runs)

    def test_proposes_an_intermediate_join_for_an_over_fanned_non_sink_node(self) -> None:
        plan = self._fan_in_plan(terminal=False)
        report = analyze_decomposition(plan, {})
        proposals = [f for f in report.findings if f.kind == S9_FAN_IN_JOIN_PROPOSAL]
        self.assertEqual(len(proposals), 1)
        finding = proposals[0]
        self.assertEqual(finding.runs, ("E",))
        self.assertEqual(finding.grade, GRADE_PROPOSAL)
        proposal = finding.proposal
        self.assertEqual(len(proposal["remaining_dependencies"]) + 1, MAX_FAN_IN)
        self.assertEqual(
            sorted(proposal["grouped_dependencies"] + proposal["remaining_dependencies"]),
            ["A", "B", "C", "D"],
        )
        self.assertEqual(
            sorted(proposal["rewired_depends_on"]),
            sorted(proposal["remaining_dependencies"] + [proposal["proposed_intermediate_node_id"]]),
        )
        # Proposals never block, regardless of enforcement.
        self.assertEqual(analyze_decomposition(plan, {}, enforce=True).block_violations, ())

    def test_exempts_the_plans_sink_join_node(self) -> None:
        plan = self._fan_in_plan(terminal=True)
        report = analyze_decomposition(plan, {})
        self.assertFalse(any(f.kind == S9_FAN_IN_JOIN_PROPOSAL for f in report.findings))

    def test_intermediate_join_proposal_is_pure_and_realizable(self) -> None:
        run = _run("E", depends_on=["A", "B", "C", "D"])
        proposal = intermediate_join_proposal(run)
        # The run object itself is never touched.
        self.assertEqual(run.depends_on, ("A", "B", "C", "D"))
        # Applying the proposal by hand (as an operator recommit would)
        # brings both the target and the new intermediate node's fan-in
        # to at most MAX_FAN_IN.
        revised_target_depends_on = proposal["rewired_depends_on"]
        revised_intermediate_depends_on = proposal["grouped_dependencies"]
        self.assertLessEqual(len(revised_target_depends_on), MAX_FAN_IN)
        self.assertLessEqual(len(revised_intermediate_depends_on), MAX_FAN_IN)


class S10Tests(unittest.TestCase):
    def test_warns_above_the_node_count_guideline(self) -> None:
        plan = _plan([_run(f"N{i}", allowed_paths=[f"f{i}.py"]) for i in range(9)])
        report = analyze_decomposition(plan, {}, enforce=True)
        warn = [f for f in report.findings if f.kind == S10_NODE_COUNT]
        self.assertEqual(len(warn), 1)
        self.assertEqual(len(warn[0].runs), 9)
        self.assertTrue(warn[0].enforced)
        self.assertEqual(report.warning_entries()[0]["severity"], "high")

    def test_silent_at_the_guideline(self) -> None:
        plan = _plan([_run(f"N{i}", allowed_paths=[f"f{i}.py"]) for i in range(8)])
        report = analyze_decomposition(plan, {}, enforce=True)
        self.assertFalse(any(f.kind == S10_NODE_COUNT for f in report.findings))


class OverrideTests(unittest.TestCase):
    def test_node_scoped_override_suppresses_exactly_its_finding(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src"]), _run("B", allowed_paths=["b.py"])])
        report = analyze_decomposition(
            plan,
            {},
            enforce=True,
            overrides=[
                {
                    "scope": "node",
                    "target": "A",
                    "kind": S2_DIRECTORY_GRANT,
                    "reason": "legacy directory grant, reviewed by hand",
                }
            ],
        )
        finding = next(f for f in report.findings if f.kind == S2_DIRECTORY_GRANT)
        self.assertTrue(finding.overridden)
        self.assertFalse(finding.enforced)
        self.assertEqual(finding.override_reason, "legacy directory grant, reviewed by hand")
        self.assertEqual(report.block_violations, ())
        self.assertEqual(len(report.overrides_applied), 1)

    def test_override_does_not_reach_a_different_node(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src"]), _run("B", allowed_paths=["other"])])
        report = analyze_decomposition(
            plan,
            {},
            enforce=True,
            overrides=[
                {"scope": "node", "target": "A", "kind": S2_DIRECTORY_GRANT, "reason": "A only"}
            ],
        )
        self.assertEqual([finding.runs for finding in report.block_violations], [("B",)])

    def test_criterion_scoped_override_suppresses_s5(self) -> None:
        plan = _plan([_run("A", allowed_paths=["a.py"], criteria=["AC-1"])])
        canonical = {"acceptance_criteria": {"AC-1": "no observable here"}}
        report = analyze_decomposition(
            plan,
            canonical,
            enforce=True,
            overrides=[
                {
                    "scope": "criterion",
                    "target": "AC-1",
                    "kind": S5_MISSING_OBSERVABLE,
                    "reason": "legacy criterion, reviewed by hand",
                }
            ],
        )
        self.assertEqual(report.block_violations, ())

    def test_override_requires_a_non_empty_reason(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src"])])
        with self.assertRaises(DecompositionConformanceError):
            analyze_decomposition(
                plan,
                {},
                enforce=True,
                overrides=[
                    {"scope": "node", "target": "A", "kind": S2_DIRECTORY_GRANT, "reason": "  "}
                ],
            )

    def test_override_rejects_an_unrecognized_kind(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src"])])
        with self.assertRaises(DecompositionConformanceError):
            analyze_decomposition(
                plan,
                {},
                enforce=True,
                overrides=[
                    {"scope": "node", "target": "A", "kind": "not-a-real-kind", "reason": "x"}
                ],
            )

    def test_override_rejects_a_missing_field(self) -> None:
        plan = _plan([_run("A", allowed_paths=["src"])])
        with self.assertRaises(DecompositionConformanceError):
            analyze_decomposition(
                plan,
                {},
                enforce=True,
                overrides=[{"scope": "node", "target": "A", "reason": "x"}],
            )


class ValidateConformanceReportTests(unittest.TestCase):
    def _valid(self) -> dict:
        plan = _plan([_run("A", allowed_paths=["a.py"])])
        return analyze_decomposition(plan, {}).as_mapping()

    def test_accepts_a_real_report(self) -> None:
        validate_conformance_report(self._valid())

    def test_rejects_a_non_object(self) -> None:
        with self.assertRaises(DecompositionConformanceError):
            validate_conformance_report("not-an-object")

    def test_rejects_the_wrong_protocol(self) -> None:
        report = self._valid()
        report["protocol"] = "wrong-protocol/1"
        with self.assertRaises(DecompositionConformanceError):
            validate_conformance_report(report)

    def test_rejects_a_missing_key(self) -> None:
        report = self._valid()
        del report["findings"]
        with self.assertRaises(DecompositionConformanceError):
            validate_conformance_report(report)

    def test_rejects_a_malformed_finding(self) -> None:
        report = self._valid()
        report["findings"] = [{"kind": "nonsense"}]
        with self.assertRaises(DecompositionConformanceError):
            validate_conformance_report(report)


class _GitRepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Conformance Test")
        (self.repository / ".harness").mkdir()
        self._write(
            self.repository / ".harness" / "repository.json",
            {
                "protocol": "harness-repository-identity/1",
                "repository_id": "conformance-test-repository",
            },
        )
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "plan.md").write_text(
            "shared work\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _commit(self, payload: dict, message: str = "add plan") -> Path:
        decomposition = self.repository / "decomposition.json"
        self._write(decomposition, payload)
        self._git("add", ".")
        self._git("commit", "-m", message)
        return decomposition

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    @staticmethod
    def _write(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


class AdmissionIntegrationTests(_GitRepositoryFixture):
    """AC-CC07-1a/1b/3: real ``prepare_approval``/``issue_receipt`` behavior."""

    def _run_payload(self, run_id: str, criterion_id: str) -> dict:
        return {
            "id": run_id,
            "objective": f"edit the shared module ({run_id})",
            "plan_sections": ["1"],
            "criteria": [criterion_id],
            "depends_on": [],
            "allowed_paths": ["shared/x.py"],
            "path_intents": [],
            "verification_argv": [
                sys.executable, "-c",
                "import pathlib; pathlib.Path('shared/x.py')",
            ],
            "verification_timeout_seconds": 30,
            "verification_required_paths": [],
        }

    def _conformance_aware_overlap_plan(self) -> dict:
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "shared work"},
            "acceptance_criteria": {
                "AC-A": (
                    'A edits the shared module. '
                    'OBSERVABLE:{"kind": "file", "referent": "shared/x.py"}'
                ),
                "AC-B": (
                    'B edits the shared module. '
                    'OBSERVABLE:{"kind": "file", "referent": "shared/x.py"}'
                ),
            },
            "runs": [self._run_payload("A", "AC-A"), self._run_payload("B", "AC-B")],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def _legacy_overlap_plan(self) -> dict:
        plan = self._conformance_aware_overlap_plan()
        plan["acceptance_criteria"] = {"AC-A": "A edits the shared module.", "AC-B": "B edits the shared module."}
        return plan

    def test_conformance_aware_plan_blocks_admission_on_a_real_s1_violation(self) -> None:
        decomposition = self._commit(self._conformance_aware_overlap_plan())
        with self.assertRaisesRegex(PlanApprovalError, S1_WRITABLE_PATH_OVERLAP):
            prepare_approval(
                repository=self.repository,
                decomposition_path=decomposition,
                output_directory=self.root / "out",
            )

    def test_legacy_plan_stays_unenforced_by_default(self) -> None:
        decomposition = self._commit(self._legacy_overlap_plan())
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "out",
        )
        self.assertFalse(prepared.conformance_report["conformance_aware"])
        self.assertFalse(prepared.conformance_report["enforced"])
        kinds = {finding["kind"] for finding in prepared.conformance_report["findings"]}
        self.assertIn(S1_WRITABLE_PATH_OVERLAP, kinds)

    def test_explicit_enforce_forces_blocking_on_a_legacy_plan(self) -> None:
        decomposition = self._commit(self._legacy_overlap_plan())
        with self.assertRaisesRegex(PlanApprovalError, S1_WRITABLE_PATH_OVERLAP):
            prepare_approval(
                repository=self.repository,
                decomposition_path=decomposition,
                output_directory=self.root / "out",
                enforce=True,
            )

    def test_node_override_carries_a_conformance_aware_plan_through_to_a_receipt(self) -> None:
        decomposition = self._commit(self._conformance_aware_overlap_plan())
        overrides = [
            {
                "scope": "node",
                "target": "A",
                "kind": S1_WRITABLE_PATH_OVERLAP,
                "reason": "A and B are hand-verified to keep disjoint hunks",
            }
        ]
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "out",
            overrides=overrides,
        )
        self.assertEqual(len(prepared.conformance_report["overrides_applied"]), 1)
        overlap = next(
            f for f in prepared.conformance_report["findings"]
            if f["kind"] == S1_WRITABLE_PATH_OVERLAP
        )
        self.assertTrue(overlap["overridden"])
        self.assertFalse(overlap["enforced"])

        # The pre-existing sibling-overlap admission warning is unrelated to
        # this override and still needs its own acknowledgment.
        acknowledgements = [
            {"warning_sha256": warning_identity(warning), "reason": "reviewed"}
            for warning in prepared.warnings
            if warning["severity"] == "high"
        ]
        operator = self.root / "out" / "operator.json"
        self._write(
            operator,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": prepared.subject_sha256,
                "actor": "test-operator",
                "approved_at": "2026-08-18T00:00:00Z",
                "statement": "I approve this exact subject.",
                "warning_acknowledgements": acknowledgements,
            },
        )
        receipt = self.root / "out" / "receipt.json"
        issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator,
            receipt_path=receipt,
            overrides=overrides,
        )
        self.assertTrue(receipt.exists())

    def test_issue_refuses_when_overrides_do_not_match_the_pinned_gate_evidence(self) -> None:
        decomposition = self._commit(self._conformance_aware_overlap_plan())
        overrides = [
            {
                "scope": "node",
                "target": "A",
                "kind": S1_WRITABLE_PATH_OVERLAP,
                "reason": "A and B are hand-verified to keep disjoint hunks",
            }
        ]
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "out",
            overrides=overrides,
        )
        operator = self.root / "out" / "operator.json"
        self._write(
            operator,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": prepared.subject_sha256,
                "actor": "test-operator",
                "approved_at": "2026-08-18T00:00:00Z",
                "statement": "I approve this exact subject.",
            },
        )
        receipt = self.root / "out" / "receipt.json"
        # Issuing without repeating the same overrides recomputes a
        # different (still-blocking) conformance report.
        with self.assertRaisesRegex(PlanApprovalError, S1_WRITABLE_PATH_OVERLAP):
            issue_receipt(
                repository=self.repository,
                subject_path=prepared.subject_path,
                gate_evidence_path=prepared.gate_evidence_path,
                operator_approval_path=operator,
                receipt_path=receipt,
            )

    def test_conformance_report_is_hash_bound_through_the_receipt(self) -> None:
        decomposition = self._commit(self._legacy_overlap_plan())
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "out",
        )
        operator = self.root / "out" / "operator.json"
        self._write(
            operator,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": prepared.subject_sha256,
                "actor": "test-operator",
                "approved_at": "2026-08-18T00:00:00Z",
                "statement": "I approve this exact subject.",
            },
        )
        gate_evidence = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        gate_evidence["conformance_report"]["conformance_aware"] = True
        prepared.gate_evidence_path.write_text(
            json.dumps(gate_evidence, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(PlanApprovalError, "conformance_report"):
            issue_receipt(
                repository=self.repository,
                subject_path=prepared.subject_path,
                gate_evidence_path=prepared.gate_evidence_path,
                operator_approval_path=operator,
                receipt_path=self.root / "out" / "receipt.json",
            )


class ConformanceJudgeAdapterTests(_GitRepositoryFixture):
    """AC-CC07-2: the S3 proposal reaching a refinement outcome via the
    existing ``plan_refinement`` judge injection point, never in-place."""

    def _contested_overlap_plan(self) -> dict:
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

    def test_judge_serializes_genuine_contention_and_reaches_a_recommitted_receipt(self) -> None:
        decomposition = self._commit(self._contested_overlap_plan())
        original_bytes = decomposition.read_bytes()

        outcome = refine_repository_decomposition(
            repository=self.repository,
            decomposition_path=decomposition,
            judge=conformance_judge(),
        )
        self.assertTrue(outcome.revised)
        serialize_repairs = [repair for repair in outcome.applied if repair.kind == "serialize"]
        self.assertEqual(len(serialize_repairs), 1)
        self.assertEqual(serialize_repairs[0].decided_by, "judge")
        self.assertIn("S3", serialize_repairs[0].reason)

        # The committed decomposition on disk is never mutated by refinement.
        self.assertEqual(decomposition.read_bytes(), original_bytes)

        # The operator recommits the revised decomposition themselves.
        self._write(decomposition, outcome.decomposition)
        self._git("add", ".")
        self._git("commit", "-m", "recommit refined plan")
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=self.root / "approved",
        )
        self.assertEqual(
            [w for w in prepared.warnings if w["severity"] == "high"], []
        )

    def test_judge_refuses_an_unrecognized_request(self) -> None:
        judge = conformance_judge()
        with self.assertRaises(DecompositionConformanceError):
            judge({"protocol": "not-the-real-protocol"})


if __name__ == "__main__":
    unittest.main()
