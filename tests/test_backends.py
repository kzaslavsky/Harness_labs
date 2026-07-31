"""Tests for swappable text backends."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_labs import CodexExecBackend, PoemBackend, TextBackendError


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


if __name__ == "__main__":
    unittest.main()
