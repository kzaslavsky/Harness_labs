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


if __name__ == "__main__":
    unittest.main()
