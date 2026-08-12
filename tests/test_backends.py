"""Tests for swappable text backends."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from io import BytesIO
from urllib.error import URLError
from pathlib import Path
from unittest.mock import patch

from harness_labs import (
    AuditActor,
    AuditJournal,
    ClaudePrintBackend,
    CodexExecBackend,
    OmlxBackend,
    PoemBackend,
    TextBackendError,
)


class BackendTests(unittest.TestCase):
    def test_poem_backend_remains_available(self) -> None:
        poem = PoemBackend().generate(
            "write a poem about the operator",
            {"subject": "the operator"},
        )

        self.assertIn("operator", poem.lower())
        self.assertGreaterEqual(len(poem.splitlines()), 2)

    @patch("harness_labs.backends.shutil.which", return_value="/fake/codex")
    @patch("harness_labs.backends.subprocess.run")
    def test_codex_backend_invokes_isolated_read_only_cli(
        self,
        run_mock,
        which_mock,
    ) -> None:
        def complete(argv, **kwargs):
            output_path = Path(argv[argv.index("-o") + 1])
            output_path.write_text("A generated poem.", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        run_mock.side_effect = complete

        text = CodexExecBackend().generate(
            "write a poem about the operator",
            {"subject": "the operator"},
        )

        self.assertEqual(text, "A generated poem.")
        argv = run_mock.call_args.args[0]
        prompt = run_mock.call_args.kwargs["input"]
        self.assertIn("--ephemeral", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("write a poem about the operator", prompt)
        self.assertIn('"subject": "the operator"', prompt)
        which_mock.assert_called_once_with("codex")

    @patch("harness_labs.backends.shutil.which", return_value="/fake/codex")
    @patch("harness_labs.backends.subprocess.run")
    def test_codex_backend_reports_nonzero_exit(self, run_mock, _which_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            ["/fake/codex"],
            7,
            "",
            "backend unavailable",
        )

        with self.assertRaisesRegex(TextBackendError, "backend unavailable"):
            CodexExecBackend().generate("task", {})

    @patch("harness_labs.backends.shutil.which", return_value="/fake/claude")
    @patch("harness_labs.backends.subprocess.run")
    def test_claude_backend_invokes_isolated_toolless_cli(
        self,
        run_mock,
        which_mock,
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            ["/fake/claude"],
            0,
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "A generated poem.",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 5,
                        "cache_creation_input_tokens": 100,
                        "output_tokens": 50,
                    },
                    "total_cost_usd": 0.01,
                }
            ),
            "",
        )

        backend = ClaudePrintBackend()
        text = backend.generate(
            "write a poem about the operator",
            {"subject": "the operator"},
        )

        self.assertEqual(text, "A generated poem.")
        argv = run_mock.call_args.args[0]
        prompt = run_mock.call_args.kwargs["input"]
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("write a poem about the operator", prompt)
        self.assertIn('"subject": "the operator"', prompt)
        self.assertEqual(backend.last_usage.input_tokens, 115)
        self.assertEqual(backend.last_usage.cached_input_tokens, 5)
        self.assertEqual(backend.last_usage.output_tokens, 50)
        which_mock.assert_called_once_with("claude")

    @patch("harness_labs.backends.shutil.which", return_value="/fake/claude")
    @patch("harness_labs.backends.subprocess.run")
    def test_claude_backend_reports_error_envelope(
        self, run_mock, _which_mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            ["/fake/claude"],
            0,
            json.dumps(
                {
                    "is_error": True,
                    "result": "Not logged in · Please run /login",
                }
            ),
            "",
        )

        with self.assertRaisesRegex(TextBackendError, "Not logged in"):
            ClaudePrintBackend().generate("task", {})

    @patch("harness_labs.backends.shutil.which", return_value="/fake/claude")
    @patch("harness_labs.backends.subprocess.run")
    def test_claude_backend_reports_nonzero_exit(
        self, run_mock, _which_mock
    ) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            ["/fake/claude"],
            3,
            "",
            "backend unavailable",
        )

        with self.assertRaisesRegex(TextBackendError, "backend unavailable"):
            ClaudePrintBackend().generate("task", {})

    @patch("harness_labs.backends.urllib.request.urlopen")
    def test_omlx_backend_calls_qwen_over_loopback(self, urlopen_mock) -> None:
        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        urlopen_mock.return_value = Response(
            b'{"choices":[{"message":{"content":"A local poem."}}],'
            b'"usage":{"prompt_tokens":120,"completion_tokens":8,'
            b'"prompt_tokens_details":{"cached_tokens":64}}}'
        )

        backend = OmlxBackend()
        text = backend.generate(
            "write a poem about the operator",
            {"subject": "the operator"},
        )

        self.assertEqual(text, "A local poem.")
        self.assertEqual(backend.last_usage.input_tokens, 120)
        self.assertEqual(backend.last_usage.cached_input_tokens, 64)
        self.assertEqual(backend.last_usage.output_tokens, 8)
        request = urlopen_mock.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "Qwen3.5-4B-MLX-4bit")
        self.assertFalse(body["stream"])
        self.assertEqual(
            body["chat_template_kwargs"],
            {"enable_thinking": False},
        )
        self.assertIn("write a poem about the operator", body["messages"][1]["content"])

    def test_omlx_backend_refuses_non_loopback_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            OmlxBackend(endpoint="https://example.com/v1/chat/completions")

    @patch("harness_labs.backends.urllib.request.urlopen")
    def test_omlx_audit_retains_exact_request_and_response(
        self, urlopen_mock
    ) -> None:
        class Response(BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        raw_response = b'{"choices":[{"message":{"content":"audited"}}]}'
        urlopen_mock.return_value = Response(raw_response)
        with tempfile.TemporaryDirectory() as temporary:
            journal = AuditJournal(
                Path(temporary) / "run",
                "run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="component",
            )
            backend = OmlxBackend(audit=journal)

            self.assertEqual(backend.generate("task", {"value": 1}), "audited")

            rows = [
                json.loads(line)
                for line in journal.events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            transport = next(
                row for row in rows if row["event_type"] == "backend_transport"
            )
            artifacts = [
                (journal.run_dir / item["path"]).read_bytes()
                for item in transport["artifacts"]
            ]
            self.assertEqual(artifacts[0], urlopen_mock.call_args.args[0].data)
            self.assertEqual(artifacts[1], raw_response)

    @patch("harness_labs.backends.urllib.request.urlopen")
    def test_omlx_backend_reports_connection_failure(self, urlopen_mock) -> None:
        urlopen_mock.side_effect = URLError("connection refused")

        with self.assertRaisesRegex(TextBackendError, "connection refused"):
            OmlxBackend().generate("task", {})


if __name__ == "__main__":
    unittest.main()
