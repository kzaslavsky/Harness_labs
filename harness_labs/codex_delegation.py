"""Codex adapters for the bounded parent/reader-child composition."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .attempts import TaskAttempt, TaskResult
from .text_executor import InMemoryReferenceStore


class CodexDelegationError(RuntimeError):
    """Raised when a Codex delegation turn violates its contract."""


@dataclass(frozen=True)
class _CodexTurn:
    final_message: str
    thread_id: str
    item_types: tuple[str, ...]
    commands: tuple["_CommandEvidence", ...]


@dataclass(frozen=True)
class _CommandEvidence:
    command: str
    exit_code: int


@dataclass(frozen=True)
class CodexFileReaderExecutor:
    """Run a Codex child that must use its shell to read one granted file."""

    store: InMemoryReferenceStore
    model: str = "gpt-5.6-terra"
    reasoning: str = "low"
    executable: str = "codex"
    timeout_seconds: float | None = None

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        try:
            task = self.store.resolve(attempt.task_ref)
            context = self.store.resolve(attempt.context_ref)
            grant = self.store.resolve(attempt.grant_ref)
        except KeyError as exc:
            return self._failed(attempt, f"unresolved reference: {exc.args[0]}")

        if not isinstance(task, str) or not task.strip():
            return self._failed(attempt, "task must resolve to non-empty text")
        if not isinstance(context, Mapping) or not isinstance(grant, Mapping):
            return self._failed(attempt, "reader context and grant must be mappings")
        if "read_file" not in grant.get("capabilities", ()):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "read_file capability is required"},
            )

        raw_path = context.get("path")
        allowed_paths = grant.get("paths", ())
        if not isinstance(raw_path, str) or not isinstance(allowed_paths, (list, tuple)):
            return self._failed(attempt, "reader path grant is invalid")
        try:
            path = Path(raw_path).resolve(strict=True)
            granted = {Path(value).resolve(strict=True) for value in allowed_paths}
        except (OSError, TypeError) as exc:
            return self._failed(attempt, f"reader path cannot be resolved: {exc}")
        if path not in granted:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "requested file is not granted"},
            )
        if not path.is_file():
            return self._failed(attempt, "granted path is not a regular file")

        with tempfile.TemporaryDirectory(prefix="harness-codex-reader-") as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / path.name).symlink_to(path)
            output_path = Path(temporary) / "reader-result.txt"
            prompt = (
                "You are a file-reader child. You must use the shell tool to read "
                "the authorized file in your working directory, then return only "
                "its exact contents with no commentary or markdown.\n\n"
                f"Task: {task}\nAuthorized file: {path.name}"
            )
            try:
                turn = _invoke_codex(
                    executable=self.executable,
                    model=self.model,
                    reasoning=self.reasoning,
                    timeout_seconds=self.timeout_seconds,
                    workspace=workspace,
                    output_path=output_path,
                    prompt=prompt,
                    disabled_features=(
                        "apps",
                        "browser_use",
                        "browser_use_external",
                        "computer_use",
                        "image_generation",
                        "multi_agent",
                    ),
                    ephemeral=True,
                )
            except CodexDelegationError as exc:
                return self._failed(attempt, str(exc))

        successful_reads = tuple(
            command
            for command in turn.commands
            if command.exit_code == 0 and path.name in command.command
        )
        if not successful_reads:
            return self._failed(attempt, "reader child did not perform a file read")
        expected = path.read_text(encoding="utf-8").strip()
        actual = turn.final_message.strip()
        if actual != expected:
            return self._failed(attempt, "reader child did not return exact file contents")

        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        content_digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": actual},
            evidence=(
                f"file:sha256:{file_digest}",
                f"content:sha256:{content_digest}",
                "codex-tool:command_execution",
            ),
        )

    @staticmethod
    def _failed(attempt: TaskAttempt, error: str) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="failed",
            payload={"error": error},
        )


def _invoke_codex(
    *,
    executable: str,
    model: str,
    reasoning: str,
    timeout_seconds: float | None,
    workspace: Path,
    output_path: Path,
    prompt: str,
    disabled_features: tuple[str, ...],
    ephemeral: bool,
    schema_path: Path | None = None,
    resume_thread_id: str | None = None,
) -> _CodexTurn:
    codex = shutil.which(executable)
    if codex is None:
        raise CodexDelegationError(f"Codex executable not found: {executable}")

    if resume_thread_id is None:
        argv = [codex, "exec", "-C", str(workspace)]
    else:
        argv = [codex, "exec", "resume"]
    argv.extend(["--ignore-user-config", "--strict-config"])
    for feature in disabled_features:
        argv.extend(["--disable", feature])
    if ephemeral:
        argv.append("--ephemeral")
    argv.extend(
        [
            "--skip-git-repo-check",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "-c",
            'approval_policy="never"',
        ]
    )
    if resume_thread_id is None:
        argv.extend(["--sandbox", "read-only"])
    else:
        argv.extend(["-c", 'sandbox_mode="read-only"'])
    argv.append("--json")
    if schema_path is not None:
        argv.extend(["--output-schema", str(schema_path)])
    argv.extend(["-o", str(output_path)])
    if resume_thread_id is not None:
        argv.append(resume_thread_id)
    argv.append("-")

    try:
        completed = subprocess.run(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            cwd=workspace,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodexDelegationError("Codex execution timed out") from exc
    except OSError as exc:
        raise CodexDelegationError(f"Codex execution failed to start: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr.strip() or completed.stdout.strip())[-1000:]
        raise CodexDelegationError(
            f"Codex exited with status {completed.returncode}: {detail}"
        )
    if not output_path.is_file():
        raise CodexDelegationError("Codex did not write its final message")

    thread_ids: list[str] = []
    item_types: list[str] = []
    commands: list[_CommandEvidence] = []
    turn_completed = False
    try:
        for line in completed.stdout.splitlines():
            event = json.loads(line)
            if event.get("type") == "thread.started":
                thread_ids.append(event["thread_id"])
            elif event.get("type") == "turn.completed":
                turn_completed = True
            elif event.get("type") in {"item.started", "item.completed"}:
                item_type = event.get("item", {}).get("type")
                if isinstance(item_type, str):
                    item_types.append(item_type)
                if (
                    event.get("type") == "item.completed"
                    and item_type == "command_execution"
                ):
                    command = event["item"].get("command")
                    exit_code = event["item"].get("exit_code")
                    if not isinstance(command, str) or not isinstance(exit_code, int):
                        raise CodexDelegationError(
                            "Codex command evidence is incomplete"
                        )
                    commands.append(_CommandEvidence(command, exit_code))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CodexDelegationError("Codex returned invalid JSONL events") from exc
    if not turn_completed or len(set(thread_ids)) != 1:
        raise CodexDelegationError("Codex process evidence is incomplete")

    final_message = output_path.read_text(encoding="utf-8").strip()
    if not final_message:
        raise CodexDelegationError("Codex returned an empty final message")
    return _CodexTurn(
        final_message=final_message,
        thread_id=thread_ids[0],
        item_types=tuple(item_types),
        commands=tuple(commands),
    )
