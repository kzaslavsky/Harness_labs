"""Swappable text backends for the minimal executor."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic_ns
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.agent_sessions import Usage
from harness_labs.core.usage import ModelPrice, parse_claude_result_usage, usage_payload
from harness_labs.core.text_executor import TextBackendError


@dataclass(frozen=True)
class PoemBackend:
    """The original deterministic poem backend."""

    audit: AuditJournal | None = field(default=None, compare=False, repr=False)

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        subject = context.get("subject", "the operator")
        text = (
            f"For {subject}, who keeps the systems bright,\n"
            "And turns uncertain signals into light,\n"
            "May every careful command find its way,\n"
            "And quiet, well-run engines mark the day."
        )
        if self.audit is not None:
            request = self.audit.write_artifact(
                "poem-backend-request",
                {"task": task, "context": dict(context)},
            )
            response = self.audit.write_artifact(
                "poem-backend-response",
                text,
                media_type="text/plain",
            )
            self.audit.append(
                "backend_transport",
                status="succeeded",
                payload={
                    "transport": "deterministic-python",
                    "implementation": f"{type(self).__module__}.{type(self).__name__}",
                },
                actor=AuditActor("poem", "backend"),
                backend_id="poem",
                duration_ms=0,
                artifacts=(request, response),
            )
        return text


@dataclass(frozen=True)
class CodexExecBackend:
    """Generate text with an isolated, read-only `codex exec` subprocess."""

    model: str = "gpt-5.6-terra"
    reasoning: Literal["low", "medium"] = "low"
    executable: str = "codex"
    timeout_seconds: float | None = None
    audit: AuditJournal | None = field(default=None, compare=False, repr=False)

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
        executable_artifact = None
        prompt_artifact = None
        if self.audit is not None:
            executable_artifact = self.audit.write_artifact(
                "codex-executable-identity",
                {"path": codex, "sha256": _file_sha256(Path(codex))},
            )
            prompt_artifact = self.audit.write_artifact(
                "codex-exec-prompt",
                prompt,
                media_type="text/plain",
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
            started_ns = monotonic_ns()
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
            if self.audit is not None:
                stdout_artifact = self.audit.write_artifact(
                    "codex-exec-stdout",
                    completed.stdout,
                    media_type="application/x-ndjson",
                )
                stderr_artifact = self.audit.write_artifact(
                    "codex-exec-stderr",
                    completed.stderr,
                    media_type="text/plain",
                )
                self.audit.append(
                    "backend_transport",
                    status="succeeded" if completed.returncode == 0 else "failed",
                    payload={
                        "transport": "codex-exec",
                        "model": self.model,
                        "reasoning": self.reasoning,
                        "executable_path": codex,
                        "executable_sha256": _file_sha256(Path(codex)),
                        "argv": argv,
                        "returncode": completed.returncode,
                    },
                    actor=AuditActor("codex-exec", "backend"),
                    backend_id="codex-exec",
                    duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
                    artifacts=(
                        prompt_artifact,
                        executable_artifact,
                        stdout_artifact,
                        stderr_artifact,
                    ),
                )

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
class ClaudePrintBackend:
    """Generate text with an isolated, tool-less `claude -p` subprocess."""

    model: str = "claude-sonnet-5"
    executable: str = "claude"
    timeout_seconds: float | None = None
    audit: AuditJournal | None = field(default=None, compare=False, repr=False)
    pricing: ModelPrice | None = field(default=None, compare=False, repr=False)
    _last_usage: Usage | None = field(
        default=None, init=False, compare=False, repr=False
    )

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        object.__setattr__(self, "_last_usage", None)
        claude = shutil.which(self.executable)
        if claude is None:
            raise TextBackendError(f"Claude executable not found: {self.executable}")

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
        executable_artifact = None
        prompt_artifact = None
        if self.audit is not None:
            executable_artifact = self.audit.write_artifact(
                "claude-executable-identity",
                {"path": claude, "sha256": _file_sha256(Path(claude))},
            )
            prompt_artifact = self.audit.write_artifact(
                "claude-print-prompt",
                prompt,
                media_type="text/plain",
            )

        with tempfile.TemporaryDirectory(prefix="harness-claude-text-") as temporary:
            argv = [
                claude,
                "-p",
                "--output-format",
                "json",
                "--model",
                self.model,
                "--tools",
                "",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--no-session-persistence",
            ]
            started_ns = monotonic_ns()
            try:
                completed = subprocess.run(
                    argv,
                    cwd=temporary,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise TextBackendError("Claude execution timed out") from exc
            except OSError as exc:
                raise TextBackendError(
                    f"Claude execution failed to start: {exc}"
                ) from exc

        payload: Mapping[str, Any] | None = None
        try:
            parsed = json.loads(completed.stdout)
            if isinstance(parsed, Mapping):
                payload = parsed
        except json.JSONDecodeError:
            payload = None
        normalized_usage = (
            parse_claude_result_usage(payload) if payload is not None else None
        )
        if normalized_usage is not None:
            object.__setattr__(self, "_last_usage", Usage(**normalized_usage))
        if self.audit is not None:
            stdout_artifact = self.audit.write_artifact(
                "claude-print-stdout",
                completed.stdout,
                media_type="application/json",
            )
            stderr_artifact = self.audit.write_artifact(
                "claude-print-stderr",
                completed.stderr,
                media_type="text/plain",
            )
            succeeded = (
                completed.returncode == 0
                and payload is not None
                and not payload.get("is_error", False)
            )
            self.audit.append(
                "backend_transport",
                status="succeeded" if succeeded else "failed",
                payload={
                    "transport": "claude-print",
                    "model": self.model,
                    "executable_path": claude,
                    "executable_sha256": _file_sha256(Path(claude)),
                    "argv": argv,
                    "returncode": completed.returncode,
                    "usage": (
                        usage_payload(
                            model=self.model,
                            pricing=self.pricing,
                            **normalized_usage,
                        )
                        if normalized_usage is not None
                        else None
                    ),
                },
                actor=AuditActor("claude-print", "backend"),
                backend_id="claude-print",
                duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
                artifacts=(
                    prompt_artifact,
                    executable_artifact,
                    stdout_artifact,
                    stderr_artifact,
                ),
            )

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise TextBackendError(
                f"Claude exited with status {completed.returncode}: {detail}"
            )
        if payload is None:
            raise TextBackendError("Claude did not return a JSON result envelope")
        result = payload.get("result")
        if payload.get("is_error", False):
            detail = str(result).strip() if isinstance(result, str) else ""
            suffix = f": {detail}" if detail else ""
            raise TextBackendError(f"Claude reported an execution error{suffix}")
        if not isinstance(result, str) or not result.strip():
            raise TextBackendError("Claude returned an empty result")
        return result.strip()

    @property
    def last_usage(self) -> Usage | None:
        return self._last_usage


@dataclass(frozen=True)
class OmlxBackend:
    """Generate text through a loopback oMLX OpenAI-compatible server."""

    model: str = "Qwen3.5-4B-MLX-4bit"
    endpoint: str = "http://127.0.0.1:8100/v1/chat/completions"
    timeout_seconds: float = 120.0
    max_tokens: int = 256
    temperature: float = 0.2
    audit: AuditJournal | None = field(default=None, compare=False, repr=False)
    pricing: ModelPrice | None = field(default=None, compare=False, repr=False)
    _last_usage: Usage | None = field(
        default=None, init=False, compare=False, repr=False
    )

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
        object.__setattr__(self, "_last_usage", None)
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
        request_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        request_artifact = None
        if self.audit is not None:
            request_artifact = self.audit.write_artifact(
                "omlx-http-request",
                request_body,
                media_type="application/json",
            )
        started_ns = monotonic_ns()
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                response_body = response.read()
                response_status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            self._record_transport(
                request_artifact,
                error_body,
                status="failed",
                status_code=exc.code,
                duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
            )
            detail = error_body[:512].decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise TextBackendError(f"oMLX returned HTTP {exc.code}{suffix}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if self.audit is not None:
                self.audit.append(
                    "backend_transport",
                    status="failed",
                    payload={
                        "transport": "openai-compatible-http",
                        "endpoint": self.endpoint,
                        "model": self.model,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    actor=AuditActor("omlx", "backend"),
                    backend_id="omlx",
                    duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
                    artifacts=(request_artifact,) if request_artifact else (),
                )
            raise TextBackendError(f"oMLX request failed: {exc}") from exc
        self._record_transport(
            request_artifact,
            response_body,
            status="succeeded",
            status_code=response_status,
            duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
        )

        try:
            payload = json.loads(response_body)
            text = payload["choices"][0]["message"]["content"].strip()
            normalized_usage = _openai_usage(payload.get("usage"))
            object.__setattr__(self, "_last_usage", normalized_usage)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise TextBackendError("oMLX returned an invalid chat completion") from exc
        if not text:
            raise TextBackendError("oMLX returned an empty chat completion")
        return text

    @property
    def last_usage(self) -> Usage | None:
        return self._last_usage

    def _record_transport(
        self,
        request_artifact,
        response_body: bytes,
        *,
        status: str,
        status_code: int,
        duration_ms: int,
    ) -> None:
        if self.audit is None:
            return
        response_artifact = self.audit.write_artifact(
            "omlx-http-response",
            response_body,
            media_type="application/json",
        )
        parsed_payload = None
        try:
            parsed_payload = json.loads(response_body)
        except (json.JSONDecodeError, TypeError):
            pass
        normalized_usage = (
            _openai_usage(parsed_payload.get("usage"))
            if isinstance(parsed_payload, Mapping)
            else None
        )
        self.audit.append(
            "backend_transport",
            status=status,
            payload={
                "transport": "openai-compatible-http",
                "endpoint": self.endpoint,
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "timeout_seconds": self.timeout_seconds,
                "http_status": status_code,
                "headers_recorded": ["Content-Type"],
                "usage": (
                    usage_payload(
                        model=self.model,
                        input_tokens=normalized_usage.input_tokens,
                        cached_input_tokens=normalized_usage.cached_input_tokens,
                        output_tokens=normalized_usage.output_tokens,
                        pricing=self.pricing,
                    )
                    if normalized_usage is not None
                    else None
                ),
            },
            actor=AuditActor("omlx", "backend"),
            backend_id="omlx",
            duration_ms=duration_ms,
            artifacts=(request_artifact, response_artifact),
        )


def _openai_usage(value: Any) -> Usage | None:
    if not isinstance(value, Mapping):
        return None
    details = value.get("prompt_tokens_details", {})
    cached = details.get("cached_tokens", 0) if isinstance(details, Mapping) else 0
    try:
        usage = Usage(
            input_tokens=int(value["prompt_tokens"]),
            cached_input_tokens=int(cached),
            output_tokens=int(value["completion_tokens"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if min(
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
    ) < 0 or usage.cached_input_tokens > usage.input_tokens:
        return None
    return usage


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
