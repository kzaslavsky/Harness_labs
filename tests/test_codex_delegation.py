"""Boundary tests for Codex parent/reader-child adapters."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs import (
    CodexDelegatingBackend,
    CodexDelegationError,
    CodexFileReaderExecutor,
    InMemoryReferenceStore,
    TaskAttempt,
    TaskResult,
)


THREAD_ID = "019fb7de-a1b3-7d63-8a94-fc9bcf3985f7"


def events(*item_types: str) -> str:
    rows = [{"type": "thread.started", "thread_id": THREAD_ID}]
    rows.extend(
        {
            "type": "item.completed",
            "item": {
                "id": f"item-{index}",
                "type": item_type,
                **(
                    {
                        "command": "/bin/zsh -lc 'cat treasure_chest.txt'",
                        "exit_code": 0,
                    }
                    if item_type == "command_execution"
                    else {}
                ),
            },
        }
        for index, item_type in enumerate(item_types)
    )
    rows.append({"type": "turn.completed", "usage": {}})
    return "\n".join(json.dumps(row) for row in rows)


class CodexDelegatingBackendTests(unittest.TestCase):
    @patch("harness_labs.codex_delegation.shutil.which", return_value="/fake/codex")
    @patch("harness_labs.codex_delegation.subprocess.run")
    def test_parent_has_no_execution_tools_and_resumes_same_thread(
        self,
        run_mock,
        _which_mock,
    ) -> None:
        calls: list[list[str]] = []

        def complete(argv, **kwargs):
            calls.append(argv)
            output_path = Path(argv[argv.index("-o") + 1])
            if "resume" in argv:
                output_path.write_text(
                    '{"action":"finish","answer":"there is booty here"}',
                    encoding="utf-8",
                )
            else:
                output_path.write_text(
                    json.dumps(
                        {
                            "action": "request_child",
                            "role": "file_reader",
                            "objective": "Read treasure_chest.txt",
                        }
                    ),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(
                argv,
                0,
                events("agent_message"),
                "",
            )

        run_mock.side_effect = complete
        backend = CodexDelegatingBackend()

        request = backend.request_child(
            "Output what is in treasure_chest.txt",
            {},
            ("file_reader",),
        )
        answer = backend.finish(
            "Output what is in treasure_chest.txt",
            {},
            TaskResult(
                attempt_id="treasure/child-1",
                status="succeeded",
                payload={"text": "there is booty here"},
            ),
        )

        self.assertEqual(request.role, "file_reader")
        self.assertEqual(answer, "there is booty here")
        self.assertEqual(backend.thread_id, THREAD_ID)
        self.assertEqual(
            backend.item_types,
            ("agent_message", "agent_message"),
        )
        first, second = calls
        self.assertEqual(first[:3], ["/fake/codex", "exec", "-C"])
        self.assertIn("shell_tool", first)
        self.assertIn("unified_exec", first)
        self.assertEqual(first[first.index("--sandbox") + 1], "read-only")
        self.assertEqual(second[:3], ["/fake/codex", "exec", "resume"])
        self.assertIn(THREAD_ID, second)
        self.assertIn("shell_tool", second)
        self.assertIn("unified_exec", second)

    @patch("harness_labs.codex_delegation.shutil.which", return_value="/fake/codex")
    @patch("harness_labs.codex_delegation.subprocess.run")
    def test_parent_rejects_any_tool_event(self, run_mock, _which_mock) -> None:
        def complete(argv, **kwargs):
            output_path = Path(argv[argv.index("-o") + 1])
            output_path.write_text(
                '{"action":"request_child","role":"file_reader",'
                '"objective":"Read treasure_chest.txt"}',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                argv,
                0,
                events("command_execution", "agent_message"),
                "",
            )

        run_mock.side_effect = complete

        with self.assertRaisesRegex(CodexDelegationError, "tool items"):
            CodexDelegatingBackend().request_child(
                "Output what is in treasure_chest.txt",
                {},
                ("file_reader",),
            )


class CodexFileReaderExecutorTests(unittest.TestCase):
    def test_repository_treasure_fixture_has_expected_contents(self) -> None:
        treasure = Path(__file__).resolve().parent.parent / "treasure_chest.txt"

        self.assertEqual(
            treasure.read_text(encoding="utf-8").strip(),
            "there is booty here",
        )

    @patch("harness_labs.codex_delegation.shutil.which", return_value="/fake/codex")
    @patch("harness_labs.codex_delegation.subprocess.run")
    def test_reader_must_execute_command_against_granted_file(
        self,
        run_mock,
        _which_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            treasure = Path(temporary) / "treasure_chest.txt"
            treasure.write_text("there is booty here\n", encoding="utf-8")
            store = InMemoryReferenceStore(
                {
                    "task:read": "Read treasure_chest.txt",
                    "context:treasure": {"path": str(treasure)},
                    "grant:treasure": {
                        "capabilities": ["read_file"],
                        "paths": [str(treasure)],
                    },
                }
            )
            attempt = TaskAttempt(
                attempt_id="treasure/child-1",
                task_ref="task:read",
                context_ref="context:treasure",
                grant_ref="grant:treasure",
                parent_attempt_id="treasure",
            )

            def complete(argv, **kwargs):
                workspace = Path(kwargs["cwd"])
                self.assertTrue((workspace / "treasure_chest.txt").is_symlink())
                self.assertNotIn("shell_tool", argv)
                self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
                output_path = Path(argv[argv.index("-o") + 1])
                output_path.write_text("there is booty here", encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    events("command_execution", "agent_message"),
                    "",
                )

            run_mock.side_effect = complete

            result = CodexFileReaderExecutor(store).execute(attempt)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["text"], "there is booty here")
        self.assertTrue(result.evidence[0].startswith("file:sha256:"))

    @patch("harness_labs.codex_delegation.shutil.which", return_value="/fake/codex")
    @patch("harness_labs.codex_delegation.subprocess.run")
    def test_reader_rejects_answer_without_command_evidence(
        self,
        run_mock,
        _which_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            treasure = Path(temporary) / "treasure_chest.txt"
            treasure.write_text("there is booty here\n", encoding="utf-8")
            store = InMemoryReferenceStore(
                {
                    "task:read": "Read treasure_chest.txt",
                    "context:treasure": {"path": str(treasure)},
                    "grant:treasure": {
                        "capabilities": ["read_file"],
                        "paths": [str(treasure)],
                    },
                }
            )
            attempt = TaskAttempt(
                attempt_id="treasure/child-1",
                task_ref="task:read",
                context_ref="context:treasure",
                grant_ref="grant:treasure",
                parent_attempt_id="treasure",
            )

            def complete(argv, **kwargs):
                output_path = Path(argv[argv.index("-o") + 1])
                output_path.write_text("there is booty here", encoding="utf-8")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    events("agent_message"),
                    "",
                )

            run_mock.side_effect = complete
            result = CodexFileReaderExecutor(store).execute(attempt)

        self.assertEqual(result.status, "failed")
        self.assertIn("did not perform a file read", result.payload["error"])


if __name__ == "__main__":
    unittest.main()
