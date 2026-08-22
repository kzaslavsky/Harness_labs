"""Acceptance tests for repository-bound, operator-attested PlanGraph admission."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from harness_labs.plangraph.plan_approval import (
    ACTIVE_DECISION_NOTICE,
    CRITERION_GRANT_MISMATCH_WARNING,
    IMPACT_ANALYSIS_UNSUPPORTED_NOTICE,
    OPERATOR_APPROVAL_PROTOCOL,
    REQUIRED_PATHS_IMPACT_WARNING,
    PlanApprovalAdmission,
    PlanApprovalError,
    issue_receipt,
    prepare_approval,
    warning_identity,
)
from harness_labs.plangraph.plan_graph import (
    PlanGraph,
    PlanGraphError,
    register_plan_graph,
)

# Pinned warning_identity hex digests for the three pre-existing warning
# kinds (AC-EM-20), captured from pre-change behaviour against the exact
# fixtures used by SiblingOverlapWarningTests and UnclaimedGrantWarningTests
# below. warning_identity hashes only kind/severity/runs/paths, so adding
# REQUIRED_PATHS_IMPACT_WARNING and gates["notices"] must not move these.
SIBLING_OVERLAP_WARNING_IDENTITY = (
    "5563b73096c149ace21395e9d94c148139697c4d821a167712eea30d2601dc73"
)
UNCLAIMED_GRANT_WARNING_IDENTITY = (
    "ddff2430e0741c7aa98eee83771ec84788584f5d80c16bb815e6f7349f9f7d26"
)
NO_DECLARED_INTENT_WARNING_IDENTITY = (
    "053566bff9a94139733c3b287d30203f6376e881167eec9dccdb33d2238963eb"
)


class PlanApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Plan Approval Test")
        (self.repository / ".harness").mkdir()
        self._write_json(
            self.repository / ".harness" / "repository.json",
            {
                "protocol": "harness-repository-identity/1",
                "repository_id": "plan-approval-test-repository",
            },
        )
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "plan.md").write_text(
            "Build feature.txt. AC-1: feature works.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepare_issue_and_validate_share_one_identity(self) -> None:
        decomposition = self._commit_decomposition(self._canonical_plan())
        prepared, receipt = self._approve(decomposition)

        admission = PlanApprovalAdmission(
            repository=self.repository, receipt_path=receipt
        )
        approved = admission.validate()
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="approval-identity",
            decomposition=approved.decomposition,
            base_commit=approved.base_commit,
            repository_id=approved.repository_id,
        )
        graph = PlanGraph(
            self.repository,
            registration,
            lambda request: None,
            run_root=self.root / "runs",
            approval_validator=admission.approval_validator(),
        )

        self.assertEqual(prepared.plan_graph_digest, graph._identity_digest())
        self.assertEqual(approved.plan_graph_digest, graph._identity_digest())
        self.assertEqual(approved.decomposition_path, "decomposition.json")

    def test_graph_created_command_path_requires_ordered_declared_producer(self) -> None:
        payload = self._canonical_plan()
        producer = payload["runs"][0]
        producer["allowed_paths"] = ["scripts/generated.py"]
        producer["path_intents"] = [
            {"path": "scripts/generated.py", "action": "create"}
        ]
        consumer = {
            "id": "B",
            "objective": "Check generated script",
            "plan_sections": ["2"],
            "criteria": ["AC-2"],
            "depends_on": ["A"],
            "allowed_paths": ["consumer.txt"],
            "path_intents": [{"path": "consumer.txt", "action": "create"}],
            "verification_argv": [sys.executable, "scripts/generated.py"],
            "verification_timeout_seconds": 30,
            "verification_required_paths": [
                {
                    "path": "scripts/generated.py",
                    "availability": "created_by",
                    "producer_run_id": "A",
                }
            ],
        }
        payload["runs"].append(consumer)
        payload["plan_sections"]["2"] = "Check generated script. AC-2: check works."
        payload["acceptance_criteria"]["AC-2"] = "check works."
        decomposition = self._commit_decomposition(payload)
        self._approve(decomposition)

        invalid_root = self.root / "invalid"
        invalid_root.mkdir()
        payload["runs"][1]["depends_on"] = []
        (self.repository / "decomposition.json").write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._git("add", "decomposition.json")
        self._git("commit", "-m", "break ordering")
        with self.assertRaisesRegex(PlanGraphError, "before producer"):
            prepare_approval(
                repository=self.repository,
                decomposition_path=self.repository / "decomposition.json",
                output_directory=invalid_root,
            )

    def test_changed_host_executable_invalidates_receipt(self) -> None:
        tool = self.root / "tool"
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        payload = self._canonical_plan()
        payload["runs"][0]["verification_argv"] = [str(tool)]
        decomposition = self._commit_decomposition(payload)
        _, receipt = self._approve(decomposition)
        admission = PlanApprovalAdmission(
            repository=self.repository, receipt_path=receipt
        )
        admission.validate()

        tool.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        with self.assertRaisesRegex(PlanApprovalError, "identity changed"):
            admission.validate()

    def test_production_cli_launches_exact_scope_and_fails_closed_on_tamper(self) -> None:
        decomposition = self._commit_decomposition(self._canonical_plan())
        prepared, receipt = self._approve(decomposition)
        request_path = self.root / "request.json"
        launcher = self.root / "launcher.py"
        launcher.write_text(
            "import json, pathlib, sys\n"
            "request = json.load(sys.stdin)\n"
            f"pathlib.Path({str(request_path)!r}).write_text(json.dumps(request))\n"
            "print(json.dumps({\n"
            " 'status': 'succeeded', 'candidate_commit': request['base_commit'],\n"
            " 'plan_graph_id': request['plan_graph_id'],\n"
            " 'plan_node_id': request['plan_node_id'],\n"
            " 'feature_run_id': request['feature_run_id'],\n"
            " 'run_dir': request['run_dir']}))\n",
            encoding="utf-8",
        )
        runner = Path(__file__).resolve().parents[1] / "scripts" / "run_plan_graph.py"
        command = [
            sys.executable,
            str(runner),
            "run",
            "--repository",
            str(self.repository),
            "--decomposition",
            str(decomposition),
            "--approval-receipt",
            str(receipt),
            "--launcher-command",
            sys.executable,
            str(launcher),
            "--run-root",
            str(self.root / "runs"),
            "--graph-attempt-id",
            "approved-graph",
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(request["run"]["allowed_paths"], ["feature.txt"])
        self.assertEqual(request["run"]["verification_timeout_seconds"], 30.0)
        checkpoint = json.loads(
            (self.root / "runs" / "approved-graph" / "checkpoint.json").read_text()
        )
        self.assertEqual(
            checkpoint["state"]["approval"]["subject_sha256"],
            prepared.subject_sha256,
        )

        request_path.unlink()
        subject = json.loads(prepared.subject_path.read_text(encoding="utf-8"))
        subject["repository"]["identity"]["id"] = "tampered"
        prepared.subject_path.write_text(json.dumps(subject), encoding="utf-8")
        rejected = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("admission failed", rejected.stderr)
        self.assertFalse(request_path.exists(), "resume mismatch must not launch")

    def test_canonical_plan_rejects_unapproved_constructor_and_overrides(self) -> None:
        decomposition = self._commit_decomposition(self._canonical_plan())
        prepared, receipt = self._approve(decomposition)
        approved = PlanApprovalAdmission(
            repository=self.repository, receipt_path=receipt
        ).validate()
        registration = register_plan_graph(
            repository=self.repository,
            logical_graph_id="approval-unapproved",
            decomposition=approved.decomposition,
            base_commit=approved.base_commit,
            repository_id=approved.repository_id,
        )
        with self.assertRaisesRegex(PlanGraphError, "requires an approval receipt"):
            PlanGraph(
                self.repository,
                registration,
                lambda request: None,
                run_root=self.root / "runs-a",
            )

    def test_nonapproved_receipt_and_missing_evidence_fail_closed(self) -> None:
        decomposition = self._commit_decomposition(self._canonical_plan())
        prepared, receipt = self._approve(decomposition)
        original = receipt.read_text(encoding="utf-8")
        payload = json.loads(original)
        payload["status"] = "blocked"
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        admission = PlanApprovalAdmission(
            repository=self.repository, receipt_path=receipt
        )
        with self.assertRaisesRegex(PlanApprovalError, "not approved"):
            admission.validate()

        receipt.write_text(original, encoding="utf-8")
        prepared.gate_evidence_path.unlink()
        with self.assertRaisesRegex(PlanApprovalError, "could not read gate evidence"):
            admission.validate()

    def test_repository_identity_is_stable_across_clone_paths(self) -> None:
        decomposition = self._commit_decomposition(self._canonical_plan())
        _, receipt = self._approve(decomposition)
        clone = self.root / "other-checkout"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.repository), str(clone)],
            check=True,
        )

        approved = PlanApprovalAdmission(
            repository=clone, receipt_path=receipt
        ).validate()

        self.assertEqual(approved.repository_id, "plan-approval-test-repository")

    def test_approval_cli_prepares_and_issues_receipt(self) -> None:
        decomposition = self._commit_decomposition(self._canonical_plan())
        approval_directory = self.root / "cli-approval"
        script = Path(__file__).resolve().parents[1] / "scripts" / "approve_plan.py"
        prepared = subprocess.run(
            [
                sys.executable,
                str(script),
                "prepare",
                str(decomposition),
                "--repository",
                str(self.repository),
                "--output-directory",
                str(approval_directory),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        preparation = json.loads(prepared.stdout)
        operator = approval_directory / "operator.json"
        self._write_json(
            operator,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": preparation["subject_sha256"],
                "actor": "cli-operator",
                "approved_at": "2026-08-10T00:00:00Z",
                "statement": "Approve exact CLI subject.",
            },
        )
        receipt = approval_directory / "receipt.json"
        issued = subprocess.run(
            [
                sys.executable,
                str(script),
                "issue",
                "--repository",
                str(self.repository),
                "--subject",
                preparation["subject"],
                "--gate-evidence",
                preparation["gate_evidence"],
                "--operator-approval",
                str(operator),
                "--receipt",
                str(receipt),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        self.assertEqual(
            PlanApprovalAdmission(
                repository=self.repository, receipt_path=receipt
            ).validate().subject_sha256,
            preparation["subject_sha256"],
        )

    def test_cli_prepare_names_unclaimed_grants_without_blocking(self) -> None:
        """The author's chance to declare or drop before the refiner decides."""

        plan = self._canonical_plan()
        plan["runs"][0]["allowed_paths"] = ["feature.txt", "docs"]
        decomposition = self._commit_decomposition(plan)
        approval_directory = self.root / "cli-surplus"
        prepared = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts" / "approve_plan.py"),
                "prepare",
                str(decomposition),
                "--repository",
                str(self.repository),
                "--output-directory",
                str(approval_directory),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        preparation = json.loads(prepared.stdout)
        self.assertEqual(preparation["unclaimed_grants"], {"A": ["docs"]})
        self.assertEqual(preparation["high_severity_warnings"], 0)
        # Advisory: nothing here stands between the operator and a receipt.
        self.assertTrue(self._approve(decomposition)[1].exists())

    def _canonical_plan(self) -> dict[str, object]:
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Build feature.txt. AC-1: feature works."},
            "acceptance_criteria": {"AC-1": "feature works."},
            "runs": [
                {
                    "id": "A",
                    "objective": "Build feature.txt",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": ["feature.txt"],
                    "path_intents": [
                        {"path": "feature.txt", "action": "create"}
                    ],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                }
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def _commit_decomposition(self, payload: dict[str, object]) -> Path:
        decomposition = self.repository / "decomposition.json"
        decomposition.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._git("add", ".")
        self._git("commit", "-m", "add plan")
        return decomposition

    def _approve(self, decomposition: Path):
        output = self.root / f"approval-{len(list(self.root.glob('approval-*')))}"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        operator = output / "operator.json"
        self._write_json(
            operator,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": prepared.subject_sha256,
                "actor": "test-operator",
                "approved_at": "2026-08-10T00:00:00Z",
                "statement": "I approve this exact subject.",
            },
        )
        receipt = output / "receipt.json"
        issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator,
            receipt_path=receipt,
        )
        return prepared, receipt

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


class SiblingOverlapWarningTests(unittest.TestCase):
    """Admission-time static prediction of controller-join conflicts."""

    @staticmethod
    def _plan(runs):
        from harness_labs.plangraph.plan_graph import PlanGraphPlan, PlanRun

        return PlanGraphPlan(
            plan="PLAN.md",
            base_commit="0" * 40,
            runs=tuple(
                PlanRun(
                    id=run_id,
                    objective=f"objective {run_id}",
                    plan_sections=(run_id,),
                    criteria=(),
                    depends_on=tuple(depends_on),
                    allowed_paths=tuple(allowed_paths),
                )
                for run_id, depends_on, allowed_paths in runs
            ),
            plan_sections={},
            acceptance_criteria={},
        )

    def test_unordered_siblings_with_shared_paths_warn(self) -> None:
        from harness_labs.plangraph.plan_approval import _sibling_overlap_warnings

        plan = self._plan(
            [
                ("WP-A", (), ("src/palette.py", "tests")),
                ("WP-B", (), ("src/palette.py", "tests")),
            ]
        )
        warnings = _sibling_overlap_warnings(plan)
        self.assertEqual(len(warnings), 1)
        record = warnings[0]
        self.assertEqual(record["kind"], "sibling-allowed-path-overlap")
        self.assertEqual(record["runs"], ["WP-A", "WP-B"])
        self.assertEqual(record["paths"], ["src/palette.py", "tests"])
        # AC-EM-20: identity of this pre-existing warning kind is unchanged
        # by the impact-analysis/decision-notice wiring added in EM-D1.
        self.assertEqual(warning_identity(record), SIBLING_OVERLAP_WARNING_IDENTITY)

    def test_dependency_ordered_runs_do_not_warn(self) -> None:
        from harness_labs.plangraph.plan_approval import _sibling_overlap_warnings

        plan = self._plan(
            [
                ("WP-A", (), ("src/palette.py",)),
                ("WP-B", ("WP-A",), ("src/palette.py",)),
                ("WP-C", ("WP-B",), ("src/palette.py",)),
            ]
        )
        self.assertEqual(_sibling_overlap_warnings(plan), [])

    def test_directory_prefix_counts_as_overlap(self) -> None:
        from harness_labs.plangraph.plan_approval import _sibling_overlap_warnings

        plan = self._plan(
            [
                ("WP-A", (), ("tests",)),
                ("WP-B", (), ("tests/test_palette.py",)),
                ("WP-C", (), ("docs",)),
            ]
        )
        warnings = _sibling_overlap_warnings(plan)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["runs"], ["WP-A", "WP-B"])
        self.assertEqual(warnings[0]["paths"], ["tests/test_palette.py"])

    def test_gate_evidence_accepts_optional_warnings_and_rejects_bad_shape(
        self,
    ) -> None:
        from harness_labs.plangraph.decomposition_conformance import CONFORMANCE_PROTOCOL
        from harness_labs.plangraph.plan_approval import (
            PlanApprovalError,
            _validate_gate_evidence,
        )

        base = {
            "protocol": "plan-approval-gates/1",
            "status": "passed",
            "subject_sha256": "a" * 64,
            "plan_graph_digest": "b" * 64,
            "host_path": "/usr/bin",
            "host_executables": [],
            "checked_at": "2026-08-15T00:00:00+00:00",
            "conformance_report": {
                "protocol": CONFORMANCE_PROTOCOL,
                "conformance_aware": False,
                "enforced": False,
                "findings": [],
                "proposals": [],
                "overrides_applied": [],
            },
        }
        _validate_gate_evidence(base)
        _validate_gate_evidence(
            {
                **base,
                "warnings": [
                    {"kind": "sibling-allowed-path-overlap", "runs": ["A", "B"]}
                ],
            }
        )
        with self.assertRaises(PlanApprovalError):
            _validate_gate_evidence({**base, "warnings": [{"runs": ["A", "B"]}]})
        with self.assertRaises(PlanApprovalError):
            _validate_gate_evidence({**base, "warnings": "not-a-list"})
        # "notices" is likewise optional and no acknowledgement gate scans it.
        _validate_gate_evidence(
            {**base, "notices": [{"kind": ACTIVE_DECISION_NOTICE, "decisions": []}]}
        )
        with self.assertRaises(PlanApprovalError):
            _validate_gate_evidence({**base, "notices": [{"decisions": []}]})
        with self.assertRaises(PlanApprovalError):
            _validate_gate_evidence({**base, "notices": "not-a-list"})


class UnclaimedGrantWarningTests(unittest.TestCase):
    """Admission-time report of grants a run never claimed to need.

    Intent-aware narrowing in the refinement loop keys off ``path_intents``,
    not the objective, so a plan that under-declares intent has grants
    silently dropped and the run discovers it as a write failure mid-flight.
    These warnings are the author's chance to declare or drop first.
    """

    @staticmethod
    def _plan(runs):
        from harness_labs.plangraph.plan_graph import PathIntent, PlanGraphPlan, PlanRun

        return PlanGraphPlan(
            plan="PLAN.md",
            base_commit="0" * 40,
            runs=tuple(
                PlanRun(
                    id=run_id,
                    objective=f"objective {run_id}",
                    plan_sections=(run_id,),
                    criteria=(),
                    allowed_paths=tuple(allowed_paths),
                    path_intents=tuple(
                        PathIntent(path=path, action="modify") for path in intents
                    ),
                )
                for run_id, allowed_paths, intents in runs
            ),
            plan_sections={},
            acceptance_criteria={},
        )

    def test_uncovered_grants_are_named_exactly(self) -> None:
        from harness_labs.plangraph.plan_approval import _unclaimed_grant_warnings

        plan = self._plan(
            [
                ("WP-A", ("src/palette.py", "src/lanes.py", "tests"), ("src/palette.py",)),
                ("WP-B", ("docs/plan.md",), ("docs/plan.md",)),
            ]
        )
        warnings = _unclaimed_grant_warnings(plan)
        self.assertEqual(len(warnings), 1)
        record = warnings[0]
        self.assertEqual(record["kind"], "run-grants-exceed-declared-intents")
        self.assertEqual(record["runs"], ["WP-A"])
        self.assertEqual(record["paths"], ["src/lanes.py", "tests"])
        self.assertEqual((record["granted"], record["claimed"]), (3, 1))
        # Advisory by design: a high severity here would be picked up by
        # ``issue_receipt``'s acknowledgement backstop and by the refinement
        # loop's actionable selection, neither of which can act on it.
        self.assertEqual(record["severity"], "info")
        # AC-EM-20: identity of this pre-existing warning kind is unchanged
        # by the impact-analysis/decision-notice wiring added in EM-D1.
        self.assertEqual(warning_identity(record), UNCLAIMED_GRANT_WARNING_IDENTITY)

    def test_a_run_declaring_no_intents_is_not_reported(self) -> None:
        from harness_labs.plangraph.plan_approval import _unclaimed_grant_warnings

        plan = self._plan(
            [
                ("WP-A", ("src/palette.py", "tests"), ()),
                ("WP-B", ("docs/plan.md", "tests"), ("docs/plan.md",)),
            ]
        )
        warnings = _unclaimed_grant_warnings(plan)
        # WP-A carries no evidence either way; only WP-B, which did declare,
        # is held to what it declared.
        self.assertEqual([record["runs"] for record in warnings], [["WP-B"]])

    def test_a_directory_grant_is_covered_by_an_intent_beneath_it(self) -> None:
        from harness_labs.plangraph.plan_approval import _unclaimed_grant_warnings

        plan = self._plan(
            [("WP-A", ("src/web", "tests"), ("src/web/palette.py", "tests"))]
        )
        self.assertEqual(_unclaimed_grant_warnings(plan), [])

    def test_a_plan_with_no_intents_anywhere_warns_once(self) -> None:
        from harness_labs.plangraph.plan_approval import _unclaimed_grant_warnings

        plan = self._plan(
            [
                ("WP-A", ("src/palette.py", "tests"), ()),
                ("WP-B", ("src/lanes.py", "tests"), ()),
                ("WP-C", ("docs/plan.md",), ()),
            ]
        )
        warnings = _unclaimed_grant_warnings(plan)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["kind"], "plan-declares-no-path-intents")
        self.assertEqual(warnings[0]["runs"], ["WP-A", "WP-B", "WP-C"])
        self.assertEqual(warnings[0]["severity"], "info")
        # AC-EM-20: identity of this pre-existing warning kind is unchanged
        # by the impact-analysis/decision-notice wiring added in EM-D1.
        self.assertEqual(
            warning_identity(warnings[0]), NO_DECLARED_INTENT_WARNING_IDENTITY
        )

    def test_the_widest_surplus_is_reported_first(self) -> None:
        from harness_labs.plangraph.plan_approval import _unclaimed_grant_warnings

        plan = self._plan(
            [
                ("WP-A", ("src/a.py", "tests"), ("src/a.py",)),
                (
                    "WP-B",
                    ("src/b.py", "src/c.py", "src/d.py", "tests"),
                    ("src/b.py",),
                ),
            ]
        )
        warnings = _unclaimed_grant_warnings(plan)
        self.assertEqual(
            [record["runs"] for record in warnings], [["WP-B"], ["WP-A"]],
            "a run claiming one of four grants is the stronger signal and "
            "should be read first",
        )


class ImpactAndDecisionAdmissionTests(PlanApprovalTests):
    """Admission-time wiring of impact analysis and decision notices
    (EM-D1). Both signals derive purely from git blobs at ``base_commit``,
    which ``PlanApprovalTests.setUp`` already provides via a real git
    repository.
    """

    def _impact_plan(self, allowed_paths: list[str]) -> dict[str, object]:
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Modify impact_target.py. AC-1: works."},
            "acceptance_criteria": {"AC-1": "works."},
            "runs": [
                {
                    "id": "A",
                    "objective": "Modify impact_target.py",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": list(allowed_paths),
                    "path_intents": [
                        {"path": "impact_target.py", "action": "modify"}
                    ],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                }
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def _operator_approval(
        self, prepared, *, statement: str, acknowledgements=()
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol": OPERATOR_APPROVAL_PROTOCOL,
            "subject_sha256": prepared.subject_sha256,
            "actor": "test-operator",
            "approved_at": "2026-08-10T00:00:00Z",
            "statement": statement,
        }
        if acknowledgements:
            payload["warning_acknowledgements"] = list(acknowledgements)
        return payload

    def test_missing_python_importer_emits_high_warning_gated_on_acknowledgement(
        self,
    ) -> None:
        """AC-EM-14."""

        (self.repository / "impact_target.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.repository / "impact_importer.py").write_text(
            "import impact_target\n\n\ndef use():\n    return impact_target.VALUE\n",
            encoding="utf-8",
        )
        payload = self._impact_plan(["impact_target.py"])
        decomposition = self._commit_decomposition(payload)
        output = self.root / "impact-gap"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        gates = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        impact_warnings = [
            w for w in gates.get("warnings") or ()
            if w["kind"] == REQUIRED_PATHS_IMPACT_WARNING
        ]
        self.assertEqual(len(impact_warnings), 1)
        warning = impact_warnings[0]
        self.assertEqual(warning["severity"], "high")
        self.assertEqual(warning["runs"], ["A"])
        self.assertEqual(warning["paths"], ["impact_importer.py"])
        self.assertIn(
            {"path": "impact_importer.py", "edge_kind": "imported_by"},
            warning["missing"],
        )

        # Refuse an approval that does not acknowledge the warning.
        unacknowledged = output / "operator-no-ack.json"
        self._write_json(
            unacknowledged,
            self._operator_approval(
                prepared, statement="Approve without acknowledging the gap."
            ),
        )
        receipt = output / "receipt.json"
        with self.assertRaisesRegex(
            PlanApprovalError, "unacknowledged high-severity"
        ):
            issue_receipt(
                repository=self.repository,
                subject_path=prepared.subject_path,
                gate_evidence_path=prepared.gate_evidence_path,
                operator_approval_path=unacknowledged,
                receipt_path=receipt,
            )

        # Accept an approval that carries the acknowledgement.
        acknowledged = output / "operator-ack.json"
        self._write_json(
            acknowledged,
            self._operator_approval(
                prepared,
                statement="Approve, importer gap acknowledged.",
                acknowledgements=[
                    {
                        "warning_sha256": warning_identity(warning),
                        "reason": "importer update tracked separately",
                    }
                ],
            ),
        )
        issued = issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=acknowledged,
            receipt_path=receipt,
        )
        self.assertTrue(issued.exists())

    def test_distinct_missing_path_sets_carry_distinct_warning_identities(
        self,
    ) -> None:
        """AC-EM-14: two different gaps must carry two different identities."""

        (self.repository / "impact_target.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.repository / "impact_importer_one.py").write_text(
            "import impact_target\n", encoding="utf-8"
        )
        (self.repository / "impact_importer_two.py").write_text(
            "import impact_target\n", encoding="utf-8"
        )
        first_payload = self._impact_plan(["impact_target.py"])
        first_decomposition = self._commit_decomposition(first_payload)
        first_prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=first_decomposition,
            output_directory=self.root / "impact-gap-one",
        )
        first_gates = json.loads(
            first_prepared.gate_evidence_path.read_text(encoding="utf-8")
        )
        first_warning = next(
            w for w in first_gates["warnings"]
            if w["kind"] == REQUIRED_PATHS_IMPACT_WARNING
        )
        self.assertEqual(
            first_warning["paths"],
            ["impact_importer_one.py", "impact_importer_two.py"],
        )

        second_payload = self._impact_plan(
            ["impact_target.py", "impact_importer_one.py"]
        )
        second_decomposition = self._commit_decomposition(second_payload)
        second_prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=second_decomposition,
            output_directory=self.root / "impact-gap-two",
        )
        second_gates = json.loads(
            second_prepared.gate_evidence_path.read_text(encoding="utf-8")
        )
        second_warning = next(
            w for w in second_gates["warnings"]
            if w["kind"] == REQUIRED_PATHS_IMPACT_WARNING
        )
        self.assertEqual(second_warning["paths"], ["impact_importer_two.py"])

        self.assertNotEqual(first_warning["paths"], second_warning["paths"])
        self.assertNotEqual(
            warning_identity(first_warning), warning_identity(second_warning)
        )

    def test_directory_grant_covers_missing_path_by_prefix_containment(
        self,
    ) -> None:
        """AC-EM-14: a run granted a directory must not see its own
        writable files inside that directory reported as an impact gap --
        coverage is decided by containment against allowed_paths, not by
        whether the neighborhood path is spelled out verbatim there."""

        (self.repository / "impact_dir").mkdir()
        (self.repository / "impact_dir" / "target.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.repository / "impact_dir" / "importer.py").write_text(
            "import impact_dir.target\n", encoding="utf-8"
        )
        payload = {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {"1": "Modify impact_dir/target.py. AC-1: works."},
            "acceptance_criteria": {"AC-1": "works."},
            "runs": [
                {
                    "id": "A",
                    "objective": "Modify impact_dir/target.py",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": ["impact_dir"],
                    "path_intents": [
                        {"path": "impact_dir/target.py", "action": "modify"}
                    ],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                }
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }
        decomposition = self._commit_decomposition(payload)
        output = self.root / "impact-dir-grant"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        gates = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        impact_warnings = [
            w for w in gates.get("warnings") or ()
            if w["kind"] == REQUIRED_PATHS_IMPACT_WARNING
        ]
        self.assertEqual(
            impact_warnings, [],
            "run A already holds write access to impact_dir/importer.py via "
            "its directory grant on impact_dir; exact-membership matching "
            "against allowed_paths must not report it as a missing path",
        )

    def test_non_python_intent_yields_unsupported_notice_not_a_warning(
        self,
    ) -> None:
        """AC-EM-15."""

        decomposition = self._commit_decomposition(self._canonical_plan())
        output = self.root / "impact-notice"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        gates = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        impact_warnings = [
            w for w in gates.get("warnings") or ()
            if w["kind"] == REQUIRED_PATHS_IMPACT_WARNING
        ]
        self.assertEqual(impact_warnings, [])
        notices = gates.get("notices") or []
        unsupported = [
            n for n in notices if n["kind"] == IMPACT_ANALYSIS_UNSUPPORTED_NOTICE
        ]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["run"], "A")
        self.assertEqual(unsupported[0]["path"], "feature.txt")
        self.assertTrue(unsupported[0]["reason"])

        # Approval succeeds with no acknowledgement at all: notices never
        # reach the acknowledgement gate.
        operator = output / "operator.json"
        self._write_json(
            operator,
            self._operator_approval(prepared, statement="Approve; only notices."),
        )
        receipt = output / "receipt.json"
        issued = issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator,
            receipt_path=receipt,
        )
        self.assertTrue(issued.exists())

    def test_active_decision_notice_lists_covering_decision_and_excludes_superseded(
        self,
    ) -> None:
        """AC-EM-18."""

        decisions_directory = self.repository / "docs" / "decisions"
        decisions_directory.mkdir(parents=True, exist_ok=True)
        (decisions_directory / "0001-old.md").write_text(
            "# 0001 — Old\n\n"
            "Status: superseded\n"
            "Concerns-paths: feature.txt\n\n"
            "## Context\n\nBody.\n",
            encoding="utf-8",
        )
        (decisions_directory / "0002-new.md").write_text(
            "# 0002 — New\n\n"
            "Status: accepted\n"
            "Supersedes: 0001-old\n"
            "Concerns-paths: feature.txt\n\n"
            "## Context\n\nBody.\n",
            encoding="utf-8",
        )
        (decisions_directory / "0003-unrelated.md").write_text(
            "# 0003 — Unrelated\n\n"
            "Status: accepted\n"
            "Concerns-paths: some/other/path.py\n\n"
            "## Context\n\nBody.\n",
            encoding="utf-8",
        )
        decomposition = self._commit_decomposition(self._canonical_plan())
        output = self.root / "decision-notice"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        gates = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        decision_notices = [
            n for n in gates.get("notices") or ()
            if n["kind"] == ACTIVE_DECISION_NOTICE
        ]
        self.assertEqual(len(decision_notices), 1)
        ids = {entry["id"] for entry in decision_notices[0]["decisions"]}
        self.assertEqual(ids, {"0002-new"})

        # No acknowledgement gate scans notices -- approval succeeds without
        # acknowledging anything.
        operator = output / "operator.json"
        self._write_json(
            operator,
            self._operator_approval(prepared, statement="Approve; decision notice only."),
        )
        receipt = output / "receipt.json"
        issued = issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator,
            receipt_path=receipt,
        )
        self.assertTrue(issued.exists())

    def test_issue_rederives_byte_identical_evidence_ignoring_working_tree_mutation(
        self,
    ) -> None:
        """AC-EM-25."""

        (self.repository / "impact_target.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.repository / "impact_importer.py").write_text(
            "import impact_target\n", encoding="utf-8"
        )
        (self.repository / "impact_dependency.py").write_text(
            "OTHER = 2\n", encoding="utf-8"
        )
        decisions_directory = self.repository / "docs" / "decisions"
        decisions_directory.mkdir(parents=True, exist_ok=True)
        (decisions_directory / "0001-covers-target.md").write_text(
            "# 0001 — Covers target\n\n"
            "Status: accepted\n"
            "Concerns-paths: impact_target.py\n\n"
            "## Context\n\nBody.\n",
            encoding="utf-8",
        )
        # An existing campaign journal the mutation below appends to, rather
        # than a file the test itself invents in place.
        journal = self.repository / "campaign-journal.jsonl"
        journal.write_text('{"kind": "campaign_started"}\n', encoding="utf-8")

        payload = self._impact_plan(["impact_target.py"])
        decomposition = self._commit_decomposition(payload)
        output = self.root / "determinism"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        gates_before = json.loads(
            prepared.gate_evidence_path.read_text(encoding="utf-8")
        )
        warning = next(
            w for w in gates_before["warnings"]
            if w["kind"] == REQUIRED_PATHS_IMPACT_WARNING
        )
        self.assertEqual(warning["paths"], ["impact_importer.py"])
        decision_notices = [
            n for n in gates_before["notices"] if n["kind"] == ACTIVE_DECISION_NOTICE
        ]
        self.assertEqual(len(decision_notices), 1)

        # Mutate impact_target.py -- itself under the run's granted path --
        # to import a committed-but-ungranted module. If admission read the
        # working tree instead of the git blob at base_commit, this would
        # add impact_dependency.py to the warning's missing set and change
        # the notice too.
        (self.repository / "impact_target.py").write_text(
            "import impact_dependency\n\nVALUE = 1\n", encoding="utf-8"
        )
        # Append a record to the pre-existing campaign journal: admission
        # consumes no journal, so this must be inert either way.
        with journal.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "finding_opened"}\n')

        operator = output / "operator.json"
        self._write_json(
            operator,
            self._operator_approval(
                prepared,
                statement="Approve, importer gap acknowledged.",
                acknowledgements=[
                    {
                        "warning_sha256": warning_identity(warning),
                        "reason": "importer update tracked separately",
                    }
                ],
            ),
        )
        receipt = output / "receipt.json"
        issued = issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=operator,
            receipt_path=receipt,
        )
        self.assertTrue(issued.exists())


class CriterionGrantWarningTests(unittest.TestCase):
    """Static detection of criteria testing logic outside the testing
    node's grant — the SI-06 defect class: AC-SI06-3 required a close-out
    emitter in ``improvement_loop.py`` (SI-05's sealed grant) while SI-06
    could only write tests and docs, blocking the graph twice mid-flight.
    """

    BASE_PATHS = (
        "harness_labs/core/decision_registry.py",
        "docs/decisions/TEMPLATE.md",
        "docs/plan.md",
    )

    @staticmethod
    def _plan(runs, acceptance_criteria):
        from harness_labs.plangraph.plan_graph import PlanGraphPlan, PlanRun

        return PlanGraphPlan(
            plan="docs/plan.md",
            base_commit="0" * 40,
            runs=tuple(
                PlanRun(
                    id=run_id,
                    objective=f"objective {run_id}",
                    plan_sections=(run_id,),
                    criteria=tuple(criteria),
                    depends_on=tuple(depends_on),
                    allowed_paths=tuple(allowed_paths),
                )
                for run_id, depends_on, allowed_paths, criteria in runs
            ),
            plan_sections={},
            acceptance_criteria=dict(acceptance_criteria),
        )

    @staticmethod
    def _warnings(plan, base_paths):
        from harness_labs.plangraph.plan_approval import (
            _criterion_grant_warnings,
        )

        return _criterion_grant_warnings(plan, base_paths)

    def _si_shaped_plan(self, close_criterion_text):
        return self._plan(
            runs=[
                (
                    "IMPL",
                    [],
                    [
                        "harness_labs/graphrun/improvement_loop.py",
                        "scripts/self_improve.py",
                        "tests/test_loop.py",
                    ],
                    ["AC-IMPL-1"],
                ),
                (
                    "CLOSE",
                    ["IMPL"],
                    [
                        "docs/development/agent-guide.md",
                        "docs/improvement",
                        ".gitignore",
                        "tests/test_closeout.py",
                    ],
                    ["AC-CLOSE-1"],
                ),
            ],
            acceptance_criteria={
                "AC-IMPL-1": (
                    "self_improve.py round synthesizes a decomposition and "
                    "dispatch refuses to launch without a receipt."
                ),
                "AC-CLOSE-1": close_criterion_text,
            },
        )

    def test_test_only_run_symbol_reference_warns_with_candidate_homes(
        self,
    ) -> None:
        """The AC-SI06-3 shape: the criterion never names the foreign file
        literally — a dotted production symbol plus a run that can only
        write tests and docs is the detectable signature."""

        plan = self._si_shaped_plan(
            "closing a successful fixture campaign emits a decision-record "
            "draft from docs/decisions/TEMPLATE.md and a test asserts "
            "decision_registry.active_decisions_for_paths returns the new "
            "record for those paths once accepted."
        )
        warnings = self._warnings(plan, self.BASE_PATHS)
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["kind"], CRITERION_GRANT_MISMATCH_WARNING)
        self.assertEqual(warning["severity"], "high")
        self.assertEqual(warning["runs"], ["CLOSE"])
        self.assertEqual(
            warning["paths"], ["harness_labs/core/decision_registry.py"]
        )
        self.assertEqual(
            warning["criteria"],
            [
                {
                    "criterion": "AC-CLOSE-1",
                    "path": "harness_labs/core/decision_registry.py",
                    "writable_homes": [],
                }
            ],
        )
        self.assertEqual(
            warning["dependency_production_grants"],
            [
                "harness_labs/graphrun/improvement_loop.py",
                "scripts/self_improve.py",
            ],
        )

    def test_criterion_naming_another_runs_grant_warns_with_home(self) -> None:
        plan = self._si_shaped_plan(
            "closing a campaign invokes the emitter in "
            "harness_labs/graphrun/improvement_loop.py and a test asserts "
            "the record lands."
        )
        warnings = self._warnings(plan, self.BASE_PATHS)
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["runs"], ["CLOSE"])
        self.assertEqual(
            warning["criteria"],
            [
                {
                    "criterion": "AC-CLOSE-1",
                    "path": "harness_labs/graphrun/improvement_loop.py",
                    "writable_homes": ["IMPL"],
                }
            ],
        )
        # A literal foreign-grant reference is flagged for its home, not
        # for homelessness, so no candidate-home listing is attached.
        self.assertNotIn("dependency_production_grants", warning)

    def test_production_capable_run_foreign_grant_reference_still_warns(
        self,
    ) -> None:
        plan = self._plan(
            runs=[
                ("A", [], ["pkg/alpha.py"], ["AC-A"]),
                ("B", [], ["pkg/beta.py"], ["AC-B"]),
            ],
            acceptance_criteria={
                "AC-A": "pkg/alpha.py works.",
                "AC-B": "pkg/beta.py consumes pkg/alpha.py output.",
            },
        )
        warnings = self._warnings(plan, self.BASE_PATHS)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["runs"], ["B"])
        self.assertEqual(warnings[0]["paths"], ["pkg/alpha.py"])
        self.assertEqual(
            warnings[0]["criteria"][0]["writable_homes"], ["A"]
        )

    def test_benign_references_do_not_warn(self) -> None:
        """Own-grant paths, tests/ paths, docs with no writable home,
        homeless production symbols referenced by a production-capable run,
        and unresolvable or class-cased tokens all stay silent."""

        plan = self._plan(
            runs=[
                (
                    "A",
                    [],
                    ["pkg/alpha.py", "tests/test_alpha.py"],
                    ["AC-A"],
                ),
            ],
            acceptance_criteria={
                "AC-A": (
                    "pkg/alpha.py folds decision_registry.active_decisions"
                    "_for_paths under docs/decisions/TEMPLATE.md, seeds "
                    "logs/improvement/campaigns/ state, reads "
                    "ConvergenceLedger.open_findings, and "
                    "tests/test_alpha.py passes."
                ),
            },
        )
        self.assertEqual(self._warnings(plan, self.BASE_PATHS), [])

    def test_test_only_run_with_no_production_reference_does_not_warn(
        self,
    ) -> None:
        plan = self._si_shaped_plan(
            "the agent guide exists in docs/development/agent-guide.md and "
            "docs/improvement holds the committed artifacts."
        )
        self.assertEqual(self._warnings(plan, self.BASE_PATHS), [])

    def test_ambiguous_bare_module_reference_is_dropped(self) -> None:
        plan = self._si_shaped_plan(
            "closing a campaign updates util.helper bookkeeping."
        )
        base = self.BASE_PATHS + ("pkg/util.py", "other/util.py")
        self.assertEqual(self._warnings(plan, base), [])


class CriterionGrantAdmissionTests(PlanApprovalTests):
    """End-to-end admission wiring of the criterion-grant gate: the SI-06
    shape reproduced in a real git fixture must surface as a high warning
    in gate evidence and gate ``issue_receipt`` on acknowledgement."""

    def _testing_node_payload(self) -> dict[str, object]:
        return {
            "protocol": "plan-graph-plan/1",
            "plan": "docs/plan.md",
            "plan_sections": {
                "1": "Build logic_module.py. AC-1: logic builds.",
                "2": "Close out. AC-2: close-out promotes.",
            },
            "acceptance_criteria": {
                "AC-1": "logic_module.py builds the campaign state.",
                "AC-2": (
                    "closing a successful campaign emits a record and a "
                    "test asserts core_registry.serve returns it."
                ),
            },
            "runs": [
                {
                    "id": "IMPL",
                    "objective": "Build the logic module",
                    "plan_sections": ["1"],
                    "criteria": ["AC-1"],
                    "depends_on": [],
                    "allowed_paths": ["logic_module.py"],
                    "path_intents": [
                        {"path": "logic_module.py", "action": "create"}
                    ],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
                {
                    "id": "CLOSE",
                    "objective": "Close out with tests and docs only",
                    "plan_sections": ["2"],
                    "criteria": ["AC-2"],
                    "depends_on": ["IMPL"],
                    "allowed_paths": [
                        "docs/closeout.md",
                        "tests/test_closeout.py",
                    ],
                    "path_intents": [
                        {"path": "docs/closeout.md", "action": "create"},
                        {"path": "tests/test_closeout.py", "action": "create"},
                    ],
                    "verification_argv": [sys.executable, "-c", "pass"],
                    "verification_timeout_seconds": 30,
                    "verification_required_paths": [],
                },
            ],
            "functionality_tests": [],
            "referenced_artifacts": [],
        }

    def test_testing_node_criterion_gates_receipt_on_acknowledgement(
        self,
    ) -> None:
        (self.repository / "core_registry.py").write_text(
            "def serve():\n    return []\n", encoding="utf-8"
        )
        decomposition = self._commit_decomposition(self._testing_node_payload())
        output = self.root / "criterion-grant"
        prepared = prepare_approval(
            repository=self.repository,
            decomposition_path=decomposition,
            output_directory=output,
        )
        gates = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        findings = [
            warning
            for warning in gates.get("warnings") or ()
            if warning["kind"] == CRITERION_GRANT_MISMATCH_WARNING
        ]
        self.assertEqual(len(findings), 1)
        warning = findings[0]
        self.assertEqual(warning["severity"], "high")
        self.assertEqual(warning["runs"], ["CLOSE"])
        self.assertEqual(warning["paths"], ["core_registry.py"])
        self.assertEqual(
            warning["dependency_production_grants"], ["logic_module.py"]
        )

        unacknowledged = output / "operator-no-ack.json"
        self._write_json(
            unacknowledged,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": prepared.subject_sha256,
                "actor": "test-operator",
                "approved_at": "2026-08-10T00:00:00Z",
                "statement": "Approve without acknowledging the mismatch.",
            },
        )
        receipt = output / "receipt.json"
        with self.assertRaisesRegex(
            PlanApprovalError, "unacknowledged high-severity"
        ):
            issue_receipt(
                repository=self.repository,
                subject_path=prepared.subject_path,
                gate_evidence_path=prepared.gate_evidence_path,
                operator_approval_path=unacknowledged,
                receipt_path=receipt,
            )

        acknowledged = output / "operator-ack.json"
        self._write_json(
            acknowledged,
            {
                "protocol": OPERATOR_APPROVAL_PROTOCOL,
                "subject_sha256": prepared.subject_sha256,
                "actor": "test-operator",
                "approved_at": "2026-08-10T00:00:00Z",
                "statement": "Approve, criterion re-homing tracked separately.",
                "warning_acknowledgements": [
                    {
                        "warning_sha256": warning_identity(warning),
                        "reason": "close-out emitter lands with IMPL",
                    }
                ],
            },
        )
        issued = issue_receipt(
            repository=self.repository,
            subject_path=prepared.subject_path,
            gate_evidence_path=prepared.gate_evidence_path,
            operator_approval_path=acknowledged,
            receipt_path=receipt,
        )
        self.assertTrue(issued.exists())

    def test_criteria_inside_own_grants_admit_without_warning(self) -> None:
        payload = self._testing_node_payload()
        payload["acceptance_criteria"]["AC-2"] = (
            "docs/closeout.md records the outcome and "
            "tests/test_closeout.py passes."
        )
        decomposition = self._commit_decomposition(payload)
        prepared, receipt = self._approve(decomposition)
        gates = json.loads(prepared.gate_evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                warning
                for warning in gates.get("warnings") or ()
                if warning["kind"] == CRITERION_GRANT_MISMATCH_WARNING
            ],
            [],
        )
        self.assertTrue(receipt.exists())


class DecisionHeaderParityTests(unittest.TestCase):
    """AC-EM-18's "same header shape decision_registry parses" clause:
    admission must not carry its own copy of the ADR header parser that can
    drift from decision_registry's, including on the wrapped multi-line
    continuation-line case."""

    def test_admission_reuses_decision_registry_header_parser_exactly(
        self,
    ) -> None:
        from harness_labs.core.decision_registry import _parse_header_block
        from harness_labs.plangraph.plan_approval import _parse_decision_header

        # Same function, not a parity-tested copy: a rename or behavior
        # change in decision_registry is felt here automatically.
        self.assertIs(_parse_decision_header, _parse_header_block)

        wrapped_header = (
            "# 0009 — Wrapped header\n\n"
            "Status: accepted\n"
            "Concerns-paths: harness_labs/plangraph/plan_approval.py,\n"
            "  harness_labs/plangraph/impact_analysis.py\n"
            "Supersedes: 0001-old\n\n"
            "## Context\n\nBody.\n"
        )
        parsed = _parse_decision_header(wrapped_header)
        self.assertEqual(parsed, _parse_header_block(wrapped_header))
        self.assertEqual(
            parsed["Concerns-paths"],
            "harness_labs/plangraph/plan_approval.py, "
            "harness_labs/plangraph/impact_analysis.py",
        )
        self.assertEqual(parsed["Supersedes"], "0001-old")


if __name__ == "__main__":
    unittest.main()
