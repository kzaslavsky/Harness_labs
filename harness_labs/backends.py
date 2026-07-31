"""Swappable text backends for the minimal executor."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from .text_executor import TextBackendError


class PoemBackend:
    """The original deterministic poem backend."""

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        subject = context.get("subject", "the operator")
        return (
            f"For {subject}, who keeps the systems bright,\n"
            "And turns uncertain signals into light,\n"
            "May every careful command find its way,\n"
            "And quiet, well-run engines mark the day."
        )


@dataclass(frozen=True)
class CodexExecBackend:
    """Generate text with an isolated, read-only `codex exec` subprocess."""

    model: str = "gpt-5.6-terra"
    reasoning: Literal["low", "medium"] = "low"
    executable: str = "codex"
    timeout_seconds: float | None = None

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        codex = shutil.which(self.executable)
        if codex is None:
            raise TextBackendError(f"Codex executable not found: {self.executable}")

        try:
            context_json = json.dumps(context, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise TextBackendError(f"context is not JSON-serializable: {exc}") from exc

        prompt = (
            "Act only as a text-generation backend. Do not use tools. "
            "Perform the task using the supplied context. Return only the requested "
            "text, with no preface, explanation, quotation marks, or markdown fence.\n\n"
            f"Task:\n{task}\n\nContext:\n{context_json}\n"
        )

        with tempfile.TemporaryDirectory(prefix="harness-codex-text-") as temporary:
            working_directory = Path(temporary)
            output_path = working_directory / "last-message.txt"
            argv = [
                codex,
                "exec",
                "-C",
                str(working_directory),
                "--ignore-user-config",
                "--strict-config",
                "--disable",
                "multi_agent",
                "--ephemeral",
                "--skip-git-repo-check",
                "-m",
                self.model,
                "-c",
                f'model_reasoning_effort="{self.reasoning}"',
                "-c",
                'approval_policy="never"',
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "-o",
                str(output_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    argv,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise TextBackendError("Codex execution timed out") from exc
            except OSError as exc:
                raise TextBackendError(f"Codex execution failed to start: {exc}") from exc

            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise TextBackendError(
                    f"Codex exited with status {completed.returncode}: {detail}"
                )
            if not output_path.is_file():
                raise TextBackendError("Codex did not write its final message")

            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                raise TextBackendError("Codex returned an empty final message")
            return text
