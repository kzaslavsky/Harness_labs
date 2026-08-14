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
from harness_labs.controller_live import (
    CodexSemanticTaskExecutor,
    select_dirty_baseline_receipt,
)
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
        with self.assertRaisesRegex(ValueError, "writable_paths"):
            CodexSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Build.",
                sandbox="workspace-write",
            )
        with self.assertRaisesRegex(ValueError, "required and forbidden"):
            CodexSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Verify.",
                sandbox="workspace-write",
                writable_paths=("tests",),
                require_repository_change=True,
                forbid_repository_change=True,
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
                schema = json.loads(
                    Path(argv[argv.index("--output-schema") + 1]).read_text(
                        encoding="utf-8"
                    )
                )
                finding_schema = schema["properties"]["findings"]["items"]
                self.assertEqual(
                    set(finding_schema["required"]),
                    set(finding_schema["properties"]),
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
                patch(
                    "harness_labs.controller_live.shutil.which", return_value="codex"
                ),
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

            def run(argv, **kwargs):
                self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            evidence = EvidenceCatalog()
            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Build precisely.",
                sandbox="workspace-write",
                require_repository_change=True,
                writable_paths=("index.html",),
            )
            snapshots = (
                {
                    "head": "abc",
                    "branch": "feature",
                    "changed_paths": [],
                    "files": {},
                },
                {
                    "head": "abc",
                    "branch": "feature",
                    "changed_paths": ["index.html"],
                    "files": {
                        "index.html": {
                            "kind": "file",
                            "sha256": "deadbeef",
                            "size_bytes": 10,
                        }
                    },
                },
            )
            with (
                patch(
                    "harness_labs.controller_live.shutil.which", return_value="codex"
                ),
                patch("harness_labs.controller_live.subprocess.run", side_effect=run),
                patch(
                    "harness_labs.controller_live.workspace_snapshot",
                    side_effect=snapshots,
                ),
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
            semantic = validate_semantic_result(
                result,
                expected_details_schema="implementation-details/1",
            )
            self.assertEqual(
                {item["kind"] for item in semantic.artifacts},
                {"implementation-summary", "workspace-change-receipt"},
            )

    def test_writable_worker_fails_when_change_escapes_grant(self) -> None:
        task = {
            "id": "build",
            "objective": "Build",
            "context": "{}",
            "details_schema": "implementation/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Build.",
            sandbox="workspace-write",
            writable_paths=("src",),
        )
        snapshots = (
            {"head": "abc", "branch": "feature", "changed_paths": [], "files": {}},
            {
                "head": "abc",
                "branch": "feature",
                "changed_paths": ["AGENTS.md"],
                "files": {"AGENTS.md": {"kind": "file", "sha256": "bad"}},
            },
        )
        raw = {
            "summary": "Built.",
            "deliverable_markdown": "Built.",
            "details_json": "{}",
            "claims": [],
            "findings": [],
            "recommendations": [],
            "unresolved_questions": [],
            "satisfied_criteria": [],
        }

        def run(argv, **kwargs):
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(json.dumps(raw), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("harness_labs.controller_live.shutil.which", return_value="codex"),
            patch("harness_labs.controller_live.subprocess.run", side_effect=run),
            patch(
                "harness_labs.controller_live.workspace_snapshot",
                side_effect=snapshots,
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            result = executor.execute(
                TaskAttempt(
                    "build/attempt-1",
                    "task:build",
                    "context:build",
                    "profile:builder",
                )
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("outside its grant", result.payload["error"])

    def test_writable_verifier_fails_if_it_changes_an_allowed_path(self) -> None:
        task = {
            "id": "verify",
            "objective": "Verify",
            "context": "{}",
            "details_schema": "review-fix-verify/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Verify only.",
            sandbox="workspace-write",
            writable_paths=("tests",),
            forbid_repository_change=True,
        )
        snapshots = (
            {"head": "abc", "branch": "feature", "changed_paths": [], "files": {}},
            {
                "head": "abc",
                "branch": "feature",
                "changed_paths": ["tests/test_feature.py"],
                "files": {
                    "tests/test_feature.py": {"kind": "file", "sha256": "changed"}
                },
            },
        )
        raw = {
            "summary": "Verified.",
            "deliverable_markdown": "Verified.",
            "details_json": '{"verified_finding_keys": []}',
            "claims": [],
            "findings": [],
            "recommendations": [],
            "unresolved_questions": [],
            "satisfied_criteria": [],
        }

        def run(argv, **kwargs):
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(json.dumps(raw), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("harness_labs.controller_live.shutil.which", return_value="codex"),
            patch("harness_labs.controller_live.subprocess.run", side_effect=run),
            patch(
                "harness_labs.controller_live.workspace_snapshot",
                side_effect=snapshots,
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            result = executor.execute(
                TaskAttempt(
                    "verify/attempt-1",
                    "task:verify",
                    "context:verify",
                    "profile:verifier",
                )
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("verifier changed repository paths", result.payload["error"])

    def test_fixer_can_use_dirty_baseline_and_receipt_records_only_its_delta(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            task = {
                "id": "fix",
                "objective": "Fix the reviewed finding",
                "context": "{}",
                "details_schema": "review-fix-fix/1",
                "acceptance_criteria": [],
                "required_capabilities": ["repo.write"],
            }
            raw = {
                "summary": "Fixed.",
                "deliverable_markdown": "Fixed the requested finding.",
                "details_json": json.dumps(
                    {"addressed_finding_keys": ["feature.txt:wrong-value"]}
                ),
                "claims": [],
                "findings": [],
                "recommendations": [],
                "unresolved_questions": [],
                "satisfied_criteria": [],
            }
            before = {
                "head": "abc",
                "branch": "feature",
                "changed_paths": ["feature.txt", "other.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "before"},
                    "other.txt": {"kind": "file", "sha256": "unchanged"},
                },
            }
            after = {
                "head": "abc",
                "branch": "feature",
                "changed_paths": ["feature.txt", "other.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "after"},
                    "other.txt": {"kind": "file", "sha256": "unchanged"},
                },
            }

            def run(argv, **kwargs):
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps(raw), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            evidence = EvidenceCatalog()
            prior_receipt = evidence.add(
                kind="workspace-change-receipt",
                content={
                    "protocol": "workspace-change-receipt/2",
                    "changed_paths": ["feature.txt", "other.txt"],
                    "files": before["files"],
                },
                media_type="application/json",
                producer_task_id="prior-attempt",
            )
            executor = CodexSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Fix only the ledger item.",
                sandbox="workspace-write",
                require_repository_change=True,
                writable_paths=("feature.txt",),
                dirty_baseline_grant={"receipt_ref": prior_receipt.ref},
            )
            with (
                patch(
                    "harness_labs.controller_live.shutil.which", return_value="codex"
                ),
                patch("harness_labs.controller_live.subprocess.run", side_effect=run),
                patch(
                    "harness_labs.controller_live.workspace_snapshot",
                    side_effect=(before, after),
                ),
            ):
                result = executor.execute(
                    TaskAttempt(
                        "fix/attempt-1",
                        "task:fix",
                        "context:fix",
                        "profile:fixer",
                    )
                )

            self.assertEqual(result.status, "succeeded", result.payload)
            semantic = validate_semantic_result(
                result,
                expected_details_schema="review-fix-fix/1",
            )
            receipt = next(
                item
                for item in semantic.artifacts
                if item["kind"] == "workspace-change-receipt"
            )
            content = json.loads(evidence.open(receipt["ref"]))
            self.assertEqual(content["worker_changed_paths"], ["feature.txt"])
            self.assertEqual(
                content["baseline_changed_paths"],
                ["feature.txt", "other.txt"],
            )

    def test_grant_refused_when_dirty_content_does_not_match_the_receipt(self):
        task = {
            "id": "fix",
            "objective": "Fix",
            "context": "{}",
            "details_schema": "review-fix-fix/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }
        evidence = EvidenceCatalog()
        prior_receipt = evidence.add(
            kind="workspace-change-receipt",
            content={
                "protocol": "workspace-change-receipt/2",
                "changed_paths": ["feature.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "receipted-content"}
                },
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            dirty_baseline_grant={"receipt_ref": prior_receipt.ref},
        )
        snapshots = (
            {
                "head": "abc",
                "branch": "feature",
                "changed_paths": ["feature.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "different-content"}
                },
            },
            {
                "head": "abc",
                "branch": "feature",
                "changed_paths": ["feature.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "different-content"}
                },
            },
        )

        def run(argv, **kwargs):
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(
                json.dumps(
                    {
                        "summary": "Verified.",
                        "deliverable_markdown": "Verified.",
                        "details_json": "{}",
                        "claims": [],
                        "findings": [],
                        "recommendations": [],
                        "unresolved_questions": [],
                        "satisfied_criteria": [],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("harness_labs.controller_live.shutil.which", return_value="codex"),
            patch("harness_labs.controller_live.subprocess.run", side_effect=run),
            patch(
                "harness_labs.controller_live.workspace_snapshot",
                side_effect=snapshots,
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            result = executor.execute(
                TaskAttempt(
                    "fix/attempt-1",
                    "task:fix",
                    "context:fix",
                    "profile:fixer",
                )
            )
        self.assertEqual(
            result.status,
            "failed",
            "a dirty path whose current content diverges from what the "
            "receipt attests must still be refused, even though its name "
            "matches a receipted path",
        )
        error = str(result.payload.get("error", ""))
        self.assertIn("dirty-baseline grant refused", error)
        self.assertIn("feature.txt", error)

    def test_worker_cannot_mint_its_own_deliverable_as_a_workspace_change_receipt(
        self,
    ) -> None:
        task = {
            "id": "inspect",
            "objective": "Inspect",
            "context": json.dumps({"artifact_kind": "workspace-change-receipt"}),
            "details_schema": "inspection/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read"],
        }
        evidence = EvidenceCatalog()
        executor = CodexSemanticTaskExecutor(
            task,
            Path("."),
            evidence,
            "Inspect only.",
        )
        raw = {
            "summary": "Inspected.",
            "deliverable_markdown": json.dumps(
                {"changed_paths": ["anything.txt"]}
            ),
            "details_json": "{}",
            "claims": [],
            "findings": [],
            "recommendations": [],
            "unresolved_questions": [],
            "satisfied_criteria": [],
        }

        def run(argv, **kwargs):
            output = Path(argv[argv.index("-o") + 1])
            output.write_text(json.dumps(raw), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("harness_labs.controller_live.shutil.which", return_value="codex"),
            patch("harness_labs.controller_live.subprocess.run", side_effect=run),
            patch(
                "harness_labs.controller_live.workspace_snapshot",
                side_effect=(
                    {"head": "abc", "branch": "feature", "changed_paths": [], "files": {}},
                    {"head": "abc", "branch": "feature", "changed_paths": [], "files": {}},
                ),
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            result = executor.execute(
                TaskAttempt(
                    "inspect/attempt-1",
                    "task:inspect",
                    "context:inspect",
                    "profile:inspector",
                )
            )
        self.assertEqual(result.status, "succeeded", result.payload)
        semantic = validate_semantic_result(
            result,
            expected_details_schema="inspection/1",
        )
        kinds = {item["kind"] for item in semantic.artifacts}
        self.assertNotIn("workspace-change-receipt", kinds)
        self.assertIn("inspection/1-report", kinds)


class SelectDirtyBaselineReceiptTests(unittest.TestCase):
    """The shared receipt-selection helper CB3-03's dispatch chokepoint uses."""

    def test_prefers_the_tightest_covering_receipt(self) -> None:
        evidence = EvidenceCatalog()
        wide = evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt", "extra.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "same"},
                    "extra.txt": {"kind": "file", "sha256": "same"},
                },
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        tight = evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt"],
                "files": {"feature.txt": {"kind": "file", "sha256": "same"}},
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        receipt_ref, failure = select_dirty_baseline_receipt(
            evidence=evidence,
            dirty_paths=["feature.txt"],
            dirty_files={"feature.txt": {"kind": "file", "sha256": "same"}},
        )
        self.assertIsNone(failure)
        self.assertEqual(receipt_ref, tight.ref)
        self.assertNotEqual(receipt_ref, wide.ref)

    def test_never_unions_two_partially_covering_receipts(self) -> None:
        evidence = EvidenceCatalog()
        evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["a.txt"],
                "files": {"a.txt": {"kind": "file", "sha256": "a"}},
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["b.txt"],
                "files": {"b.txt": {"kind": "file", "sha256": "b"}},
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        receipt_ref, failure = select_dirty_baseline_receipt(
            evidence=evidence,
            dirty_paths=["a.txt", "b.txt"],
            dirty_files={
                "a.txt": {"kind": "file", "sha256": "a"},
                "b.txt": {"kind": "file", "sha256": "b"},
            },
        )
        self.assertIsNone(receipt_ref)
        self.assertIsNotNone(failure)
        self.assertFalse(failure.ok)

    def test_selection_is_independent_of_catalog_order(self) -> None:
        forward = EvidenceCatalog()
        backward = EvidenceCatalog()
        tight_forward = forward.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt"],
                "files": {"feature.txt": {"kind": "file", "sha256": "same"}},
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        wide_forward = forward.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt", "extra.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "same"},
                    "extra.txt": {"kind": "file", "sha256": "same"},
                },
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        wide_backward = backward.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt", "extra.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "same"},
                    "extra.txt": {"kind": "file", "sha256": "same"},
                },
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        tight_backward = backward.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt"],
                "files": {"feature.txt": {"kind": "file", "sha256": "same"}},
            },
            media_type="application/json",
            producer_task_id="prior",
        )
        dirty_files = {"feature.txt": {"kind": "file", "sha256": "same"}}
        forward_ref, _ = select_dirty_baseline_receipt(
            evidence=forward, dirty_paths=["feature.txt"], dirty_files=dirty_files
        )
        backward_ref, _ = select_dirty_baseline_receipt(
            evidence=backward, dirty_paths=["feature.txt"], dirty_files=dirty_files
        )
        self.assertEqual(forward_ref, tight_forward.ref)
        self.assertEqual(backward_ref, tight_backward.ref)
        self.assertNotEqual(forward_ref, wide_forward.ref)
        self.assertNotEqual(backward_ref, wide_backward.ref)

    def test_no_dirty_paths_selects_nothing(self) -> None:
        evidence = EvidenceCatalog()
        receipt_ref, failure = select_dirty_baseline_receipt(
            evidence=evidence, dirty_paths=[], dirty_files={}
        )
        self.assertIsNone(receipt_ref)
        self.assertIsNone(failure)


if __name__ == "__main__":
    unittest.main()
