"""Tests for the live semantic worker boundary without invoking a model."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.attempts import TaskAttempt
from harness_labs.controller_evidence import EvidenceCatalog
from harness_labs.controller_live import CodexSemanticTaskExecutor
from harness_labs.controller_results import validate_semantic_result


class CodexSemanticTaskExecutorTests(unittest.TestCase):
    def test_repository_change_policy_requires_writable_sandbox(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace-write"):
            CodexSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Build.",
                require_repository_change=True,
            )
        with self.assertRaisesRegex(ValueError, "preflight"):
            CodexSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Verify.",
                require_preflight_success=True,
            )

    def test_preflight_and_model_output_become_hashed_semantic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            evidence = EvidenceCatalog()
            task = {
                "id": "inspect",
                "objective": "Inspect the repository",
                "context": json.dumps({"artifact_kind": "inspection-report"}),
                "details_schema": "inspection-details/1",
                "acceptance_criteria": ["inspected"],
                "required_capabilities": ["repo.read"],
            }
            attempt = TaskAttempt(
                "inspect/attempt-1",
                "task:inspect",
                "context:inspect",
                "profile:inspector",
            )
            raw = {
                "summary": "Inspection complete.",
                "deliverable_markdown": "# Inspection\nEvidence-backed result.",
                "details_json": json.dumps({"head": "abc"}),
                "claims": [
                    {
                        "id": "head",
                        "statement": "The head is abc.",
                        "kind": "observed",
                    }
                ],
                "findings": [],
                "recommendations": ["Keep it bounded."],
                "unresolved_questions": [],
                "satisfied_criteria": ["inspected"],
            }
            prompts: list[str] = []

            def run(argv, **kwargs):
                if argv[0] == "safe-check":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout="CHECK PASSED\n",
                        stderr="",
                    )
                prompts.append(kwargs["input"])
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout='{"type":"turn.completed"}\n',
                    stderr="",
                )

            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Inspect precisely.",
                preflight_argv=("safe-check",),
            )
            with (
                patch("harness_labs.controller_live.shutil.which", return_value="codex"),
                patch("harness_labs.controller_live.subprocess.run", side_effect=run),
            ):
                result = executor.execute(attempt)

            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result,
                expected_details_schema="inspection-details/1",
            )
            self.assertEqual(semantic.summary, "Inspection complete.")
            self.assertEqual(
                {item["kind"] for item in semantic.artifacts},
                {"inspection-report", "verified-command-output"},
            )
            self.assertEqual(
                semantic.criterion_coverage[0]["criterion_id"],
                "inspected",
            )
            self.assertIn("CHECK PASSED", prompts[0])
            for ref in result.evidence[:2]:
                self.assertTrue(evidence.contains(ref))

    def test_writable_worker_uses_workspace_sandbox_and_requires_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            task = {
                "id": "build",
                "objective": "Build the feature",
                "context": json.dumps({"artifact_kind": "implementation-summary"}),
                "details_schema": "implementation-details/1",
                "acceptance_criteria": ["built"],
                "required_capabilities": ["repo.write"],
            }
            raw = {
                "summary": "Built.",
                "deliverable_markdown": "# Build\nComplete.",
                "details_json": json.dumps({"files": ["index.html"]}),
                "claims": [],
                "findings": [],
                "recommendations": [],
                "unresolved_questions": [],
                "satisfied_criteria": ["built"],
            }
            statuses = iter(["", "?? index.html\n"])

            def run(argv, **kwargs):
                if argv[:2] == ["git", "status"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=next(statuses), stderr=""
                    )
                self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                EvidenceCatalog(),
                "Build precisely.",
                sandbox="workspace-write",
                require_repository_change=True,
            )
            with (
                patch("harness_labs.controller_live.shutil.which", return_value="codex"),
                patch("harness_labs.controller_live.subprocess.run", side_effect=run),
            ):
                result = executor.execute(
                    TaskAttempt(
                        "build/attempt-1",
                        "task:build",
                        "context:build",
                        "profile:builder",
                    )
                )
            self.assertEqual(result.status, "succeeded", result.payload)


if __name__ == "__main__":
    unittest.main()
