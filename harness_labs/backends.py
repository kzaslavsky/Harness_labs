"""Swappable text backends for the minimal executor."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

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


@dataclass(frozen=True)
class OmlxBackend:
    """Generate text through a loopback oMLX OpenAI-compatible server."""

    model: str = "Qwen3.5-4B-MLX-4bit"
    endpoint: str = "http://127.0.0.1:8100/v1/chat/completions"
    timeout_seconds: float = 120.0
    max_tokens: int = 256
    temperature: float = 0.2

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.path != "/v1/chat/completions"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "oMLX endpoint must be a loopback HTTP /v1/chat/completions URL"
            )
        if not self.model.strip():
            raise ValueError("oMLX model must be non-empty")
        if self.max_tokens < 1:
            raise ValueError("oMLX max_tokens must be positive")

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        try:
            context_json = json.dumps(context, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise TextBackendError(f"context is not JSON-serializable: {exc}") from exc

        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Act only as a text-generation backend. Perform the task "
                        "using the supplied context. Return only the requested text, "
                        "with no preface, explanation, quotation marks, or markdown "
                        "fence."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Task:\n{task}\n\nContext:\n{context_json}",
                },
            ],
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(512).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise TextBackendError(f"oMLX returned HTTP {exc.code}{suffix}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TextBackendError(f"oMLX request failed: {exc}") from exc

        try:
            payload = json.loads(response_body)
            text = payload["choices"][0]["message"]["content"].strip()
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise TextBackendError("oMLX returned an invalid chat completion") from exc
        if not text:
            raise TextBackendError("oMLX returned an empty chat completion")
        return text
