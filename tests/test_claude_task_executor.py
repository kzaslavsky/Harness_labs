"""Tests for the Claude-backed semantic worker boundary without invoking a model."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs.core.attempts import TaskAttempt
from harness_labs.core.claude_task_executor import ClaudeSemanticTaskExecutor
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_results import validate_semantic_result


def _raw_result(**overrides):
    raw = {
        "summary": "Inspection complete.",
        "deliverable_markdown": "# Inspection\nEvidence-backed result.",
        "details_json": json.dumps({"head": "abc"}),
        "claims": [],
        "findings": [],
        "recommendations": [],
        "unresolved_questions": [],
        "satisfied_criteria": [],
    }
    raw.update(overrides)
    return raw


def _envelope(raw, **overrides):
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(raw),
        "structured_output": raw,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 100,
            "output_tokens": 50,
        },
        "total_cost_usd": 0.01,
        "permission_denials": [],
    }
    envelope.update(overrides)
    return envelope


def _snapshot(changed_paths=(), files=None):
    return {
        "head": "abc",
        "branch": "feature",
        "changed_paths": list(changed_paths),
        "files": dict(files or {}),
    }


class ClaudeSemanticTaskExecutorTests(unittest.TestCase):
    def test_repository_change_policy_requires_writable_sandbox(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace-write"):
            ClaudeSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Build.",
                require_repository_change=True,
            )
        with self.assertRaisesRegex(ValueError, "preflight"):
            ClaudeSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Verify.",
                require_preflight_success=True,
            )
        with self.assertRaisesRegex(ValueError, "writable_paths"):
            ClaudeSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Build.",
                sandbox="workspace-write",
            )
        with self.assertRaisesRegex(ValueError, "required and forbidden"):
            ClaudeSemanticTaskExecutor(
                {},
                Path("."),
                EvidenceCatalog(),
                "Verify.",
                sandbox="workspace-write",
                writable_paths=("tests",),
                require_repository_change=True,
                forbid_repository_change=True,
            )

    def test_read_only_worker_gets_no_shell_and_produces_semantic_evidence(
        self,
    ) -> None:
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
            raw = _raw_result(
                claims=[
                    {
                        "id": "head",
                        "statement": "The head is abc.",
                        "kind": "observed",
                    }
                ],
                satisfied_criteria=["inspected"],
            )
            prompts: list[str] = []
            argvs: list[list[str]] = []

            def run(argv, **kwargs):
                if argv[0] == "safe-check":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout="CHECK PASSED\n",
                        stderr="",
                    )
                argvs.append(argv)
                prompts.append(kwargs["input"])
                schema = json.loads(argv[argv.index("--json-schema") + 1])
                finding_schema = schema["properties"]["findings"]["items"]
                self.assertEqual(
                    set(finding_schema["required"]),
                    set(finding_schema["properties"]),
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(_envelope(raw)),
                    stderr="",
                )

            executor = ClaudeSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Inspect precisely.",
                preflight_argv=("safe-check",),
            )
            with (
                patch(
                    "harness_labs.core.claude_task_executor.shutil.which",
                    return_value="claude",
                ),
                patch(
                    "harness_labs.core.claude_task_executor.subprocess.run",
                    side_effect=run,
                ),
                patch(
                    "harness_labs.core.claude_task_executor.workspace_snapshot",
                    side_effect=(_snapshot(), _snapshot()),
                ),
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
            self.assertIn("CHECK PASSED", prompts[0])
            self.assertIn("model-backend:claude-print", result.evidence)
            argv = argvs[0]
            self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
            self.assertEqual(argv[argv.index("--output-format") + 1], "json")
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
            self.assertIn("-p", argv)
            self.assertIn("--no-session-persistence", argv)
            self.assertIn("--strict-mcp-config", argv)
            self.assertNotIn("--dangerously-skip-permissions", argv)
            for ref in result.evidence[:2]:
                self.assertTrue(evidence.contains(ref))

    def test_read_only_worker_fails_when_repository_state_changes(self) -> None:
        task = {
            "id": "inspect",
            "objective": "Inspect",
            "context": "{}",
            "details_schema": "inspection/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read"],
        }
        executor = ClaudeSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Inspect only.",
        )
        snapshots = (
            _snapshot(),
            _snapshot(
                changed_paths=["index.html"],
                files={"index.html": {"kind": "file", "sha256": "bad"}},
            ),
        )

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(_envelope(_raw_result())),
                stderr="",
            )

        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
                side_effect=snapshots,
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
        self.assertEqual(result.status, "failed")
        self.assertIn("read-only worker changed", result.payload["error"])

    def test_writable_worker_skips_permissions_and_requires_a_change(self) -> None:
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
            raw = _raw_result(
                summary="Built.",
                deliverable_markdown="# Build\nComplete.",
                details_json=json.dumps({"files": ["index.html"]}),
                satisfied_criteria=["built"],
            )

            def run(argv, **kwargs):
                self.assertEqual(
                    argv[argv.index("--tools") + 1],
                    "Read,Glob,Grep,Edit,Write,Bash",
                )
                self.assertIn("--dangerously-skip-permissions", argv)
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(_envelope(raw)),
                    stderr="",
                )

            evidence = EvidenceCatalog()
            executor = ClaudeSemanticTaskExecutor(
                task,
                repository,
                evidence,
                "Build precisely.",
                sandbox="workspace-write",
                require_repository_change=True,
                writable_paths=("index.html",),
            )
            snapshots = (
                _snapshot(),
                _snapshot(
                    changed_paths=["index.html"],
                    files={
                        "index.html": {
                            "kind": "file",
                            "sha256": "deadbeef",
                            "size_bytes": 10,
                        }
                    },
                ),
            )
            with (
                patch(
                    "harness_labs.core.claude_task_executor.shutil.which",
                    return_value="claude",
                ),
                patch(
                    "harness_labs.core.claude_task_executor.subprocess.run",
                    side_effect=run,
                ),
                patch(
                    "harness_labs.core.claude_task_executor.workspace_snapshot",
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
        executor = ClaudeSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Build.",
            sandbox="workspace-write",
            writable_paths=("src",),
        )
        snapshots = (
            _snapshot(),
            _snapshot(
                changed_paths=["AGENTS.md"],
                files={"AGENTS.md": {"kind": "file", "sha256": "bad"}},
            ),
        )

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(_envelope(_raw_result())),
                stderr="",
            )

        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
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

    def test_error_envelope_fails_the_attempt(self) -> None:
        task = {
            "id": "inspect",
            "objective": "Inspect",
            "context": "{}",
            "details_schema": "inspection/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read"],
        }
        executor = ClaudeSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Inspect.",
        )

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    _envelope(
                        _raw_result(),
                        is_error=True,
                        result="Not logged in",
                        structured_output=None,
                    )
                ),
                stderr="",
            )

        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
                side_effect=(_snapshot(), _snapshot()),
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
        self.assertEqual(result.status, "failed")
        self.assertIn("execution error", result.payload["error"])
        self.assertIn("Not logged in", result.payload["error"])

    def test_structured_output_falls_back_to_result_text(self) -> None:
        task = {
            "id": "inspect",
            "objective": "Inspect",
            "context": "{}",
            "details_schema": "inspection/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read"],
        }
        executor = ClaudeSemanticTaskExecutor(
            task,
            Path("."),
            EvidenceCatalog(),
            "Inspect.",
        )
        raw = _raw_result()

        def run(argv, **kwargs):
            envelope = _envelope(raw)
            del envelope["structured_output"]
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(envelope),
                stderr="",
            )

        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
                side_effect=(_snapshot(), _snapshot()),
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


class ClaudeSemanticTaskExecutorDirtyBaselineTests(unittest.TestCase):
    """AC-CB05-1 / AC-CB05-2: the Claude executor's own writable preflight."""

    def _run(self, executor: ClaudeSemanticTaskExecutor, snapshots):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(_envelope(_raw_result())),
                stderr="",
            )

        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
                side_effect=snapshots,
            ),
            patch.object(Path, "exists", return_value=True),
        ):
            return executor.execute(
                TaskAttempt(
                    "fix/attempt-1",
                    "task:fix",
                    "context:fix",
                    "profile:fixer",
                )
            )

    def _task(self) -> dict:
        return {
            "id": "fix",
            "objective": "Fix",
            "context": "{}",
            "details_schema": "review-fix-fix/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.write"],
        }

    def test_dirty_path_covered_by_named_receipt_is_accepted(self) -> None:
        evidence = EvidenceCatalog()
        receipt = evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt"],
                "files": {"feature.txt": {"kind": "file", "sha256": "same"}},
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        executor = ClaudeSemanticTaskExecutor(
            self._task(),
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            dirty_baseline_grant={"receipt_ref": receipt.ref},
        )
        files = {"feature.txt": {"kind": "file", "sha256": "same"}}
        snapshots = (
            _snapshot(changed_paths=["feature.txt"], files=files),
            _snapshot(changed_paths=["feature.txt"], files=files),
        )
        result = self._run(executor, snapshots)
        self.assertEqual(result.status, "succeeded", result.payload)

    def test_dirty_path_outside_receipted_change_set_is_refused_even_inside_writable_paths(
        self,
    ) -> None:
        evidence = EvidenceCatalog()
        receipt = evidence.add(
            kind="workspace-change-receipt",
            content={"changed_paths": ["covered.txt"]},
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        executor = ClaudeSemanticTaskExecutor(
            self._task(),
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("uncovered.txt",),
            dirty_baseline_grant={"receipt_ref": receipt.ref},
        )
        snapshots = (
            _snapshot(changed_paths=["uncovered.txt"]),
            _snapshot(changed_paths=["uncovered.txt"]),
        )
        result = self._run(executor, snapshots)
        self.assertEqual(
            result.status,
            "failed",
            "a dirty path outside the receipted change set must still be "
            "refused, even though it is one of the worker's own writable_paths",
        )
        error = str(result.payload.get("error", ""))
        self.assertIn("dirty-baseline grant refused", error)
        self.assertIn("uncovered.txt", error)

    def test_dirty_content_mismatch_is_refused_even_when_the_path_matches(
        self,
    ) -> None:
        evidence = EvidenceCatalog()
        receipt = evidence.add(
            kind="workspace-change-receipt",
            content={
                "changed_paths": ["feature.txt"],
                "files": {
                    "feature.txt": {"kind": "file", "sha256": "receipted-content"}
                },
            },
            media_type="application/json",
            producer_task_id="prior-attempt",
        )
        executor = ClaudeSemanticTaskExecutor(
            self._task(),
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            dirty_baseline_grant={"receipt_ref": receipt.ref},
        )
        files = {"feature.txt": {"kind": "file", "sha256": "different-content"}}
        snapshots = (
            _snapshot(changed_paths=["feature.txt"], files=files),
            _snapshot(changed_paths=["feature.txt"], files=files),
        )
        result = self._run(executor, snapshots)
        self.assertEqual(
            result.status,
            "failed",
            "a dirty path whose current content diverges from what the "
            "receipt attests must still be refused",
        )
        error = str(result.payload.get("error", ""))
        self.assertIn("dirty-baseline grant refused", error)
        self.assertIn("feature.txt", error)

    def test_dirty_baseline_without_any_grant_is_refused(self) -> None:
        evidence = EvidenceCatalog()
        executor = ClaudeSemanticTaskExecutor(
            self._task(),
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
        )
        snapshots = (
            _snapshot(changed_paths=["feature.txt"]),
            _snapshot(changed_paths=["feature.txt"]),
        )
        result = self._run(executor, snapshots)
        self.assertEqual(result.status, "failed")
        self.assertIn(
            "clean repository baseline", str(result.payload.get("error", ""))
        )

    def test_legacy_allow_dirty_baseline_flag_no_longer_bypasses_the_preflight(
        self,
    ) -> None:
        # Restored only so callers built against the prior constructor keep
        # working; it must never itself grant access to a dirty baseline.
        evidence = EvidenceCatalog()
        executor = ClaudeSemanticTaskExecutor(
            self._task(),
            Path("."),
            evidence,
            "Fix only.",
            sandbox="workspace-write",
            writable_paths=("feature.txt",),
            allow_dirty_baseline=True,
        )
        snapshots = (
            _snapshot(changed_paths=["feature.txt"]),
            _snapshot(changed_paths=["feature.txt"]),
        )
        result = self._run(executor, snapshots)
        self.assertEqual(result.status, "failed")
        self.assertIn(
            "clean repository baseline", str(result.payload.get("error", ""))
        )

    def test_worker_cannot_mint_its_own_deliverable_as_a_workspace_change_receipt(
        self,
    ) -> None:
        evidence = EvidenceCatalog()
        task = {
            "id": "inspect",
            "objective": "Inspect",
            "context": json.dumps({"artifact_kind": "workspace-change-receipt"}),
            "details_schema": "inspection/1",
            "acceptance_criteria": [],
            "required_capabilities": ["repo.read"],
        }
        executor = ClaudeSemanticTaskExecutor(
            task,
            Path("."),
            evidence,
            "Inspect only.",
        )
        raw = _raw_result(
            deliverable_markdown=json.dumps({"changed_paths": ["anything.txt"]})
        )

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(_envelope(raw)),
                stderr="",
            )

        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
                side_effect=(_snapshot(), _snapshot()),
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


class ClaudeImageAttachmentTests(unittest.TestCase):
    """The access grant that makes the Claude image path more than a suggestion.

    P4 told a Claude worker to open absolute artifact paths with its
    file-reading tool but granted it no access to the directory holding them.
    Probed against the installed CLI, a read-only `claude -p` worker asked to
    read a file outside its cwd answers "CANNOT"; with ``--add-dir`` for that
    directory it returns the file's contents. The prompt text alone therefore
    proved nothing, which is what these tests now pin.
    """

    def _execute(self, repository: Path, context: dict, sandbox: str) -> list[str]:
        task = {
            "id": "repair",
            "objective": "Repair the visual gate",
            "context": json.dumps(context),
            "details_schema": "repair/1",
            "acceptance_criteria": [],
            "required_capabilities": (
                ["repo.write"] if sandbox == "workspace-write" else ["repo.read"]
            ),
        }
        argvs: list[list[str]] = []
        prompts: list[str] = []

        def run(argv, **kwargs):
            argvs.append(list(argv))
            prompts.append(kwargs["input"])
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(_envelope(_raw_result())), stderr=""
            )

        snapshot = {
            "head": "abc",
            "branch": "feature",
            "changed_paths": [],
            "files": {},
        }
        executor = ClaudeSemanticTaskExecutor(
            task,
            repository,
            EvidenceCatalog(),
            "Repair precisely.",
            sandbox=sandbox,
        )
        with (
            patch(
                "harness_labs.core.claude_task_executor.shutil.which",
                return_value="claude",
            ),
            patch(
                "harness_labs.core.claude_task_executor.subprocess.run",
                side_effect=run,
            ),
            patch(
                "harness_labs.core.claude_task_executor.workspace_snapshot",
                return_value=snapshot,
            ),
        ):
            result = executor.execute(
                TaskAttempt(
                    "repair/attempt-1",
                    "task:repair",
                    "context:repair",
                    "profile:repairer",
                )
            )
        self.assertEqual(result.status, "succeeded", result.payload)
        self.assertEqual(len(argvs), 1)
        self.prompt = prompts[0]
        return argvs[0]

    def _images(self, root: Path) -> list[Path]:
        artifacts = root / "run" / "artifacts"
        artifacts.mkdir(parents=True)
        images = []
        for index in range(2):
            image = artifacts / f"{index:06d}-verification-failure-image.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
            images.append(image)
        return images

    def test_a_read_only_worker_is_granted_the_directory_holding_the_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            images = self._images(root)
            argv = self._execute(
                repository,
                {
                    "failed_verification": {
                        "image_artifacts": [{"path": str(i)} for i in images]
                    }
                },
                "read-only",
            )
            # One grant for the single directory the images share, not one per
            # file, and no --dangerously-skip-permissions on a read-only worker.
            granted = [
                argv[index + 1]
                for index, value in enumerate(argv)
                if value == "--add-dir"
            ]
            self.assertEqual(granted, [str(images[0].parent)])
            self.assertNotIn("--dangerously-skip-permissions", argv)
            # The prompt still has to name the paths it just granted access to.
            for image in images:
                self.assertIn(str(image), self.prompt)
            self.assertIn("file-reading tool", self.prompt)

    def test_a_round_without_images_grants_no_extra_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            (repository / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            argv = self._execute(repository, {"failed_verification": {}}, "read-only")
            self.assertNotIn("--add-dir", argv)


if __name__ == "__main__":
    unittest.main()
