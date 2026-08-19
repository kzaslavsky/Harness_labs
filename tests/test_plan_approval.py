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
    OPERATOR_APPROVAL_PROTOCOL,
    PlanApprovalAdmission,
    PlanApprovalError,
    issue_receipt,
    prepare_approval,
)
from harness_labs.plangraph.plan_graph import (
    PlanGraph,
    PlanGraphError,
    register_plan_graph,
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


if __name__ == "__main__":
    unittest.main()
