"""Codex adapters for the bounded parent/reader-child composition."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic_ns
from typing import Mapping

from .attempts import TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
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


@dataclass
class CodexFileReaderExecutor:
    """Run a Codex child that must use its shell to read one granted file."""

    store: InMemoryReferenceStore
    model: str = "gpt-5.6-terra"
    reasoning: str = "low"
    executable: str = "codex"
    timeout_seconds: float | None = None
    keep_alive: bool = False
    audit: AuditJournal | None = field(default=None, repr=False)
    _temporary: tempfile.TemporaryDirectory[str] | None = field(
        default=None, init=False, repr=False
    )
    _workspace: Path | None = field(default=None, init=False, repr=False)
    _thread_id: str | None = field(default=None, init=False, repr=False)
    _attempt_id: str | None = field(default=None, init=False, repr=False)

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

        temporary = tempfile.TemporaryDirectory(prefix="harness-codex-reader-")
        workspace = Path(temporary.name) / "workspace"
        workspace.mkdir()
        (workspace / path.name).symlink_to(path)
        output_path = Path(temporary.name) / "reader-result.txt"
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
                disabled_features=_READER_DISABLED_FEATURES,
                ephemeral=not self.keep_alive,
                audit=self.audit,
                attempt_id=attempt.attempt_id,
            )
        except CodexDelegationError as exc:
            temporary.cleanup()
            return self._failed(attempt, str(exc))

        successful_reads = tuple(
            command
            for command in turn.commands
            if command.exit_code == 0 and path.name in command.command
        )
        if not successful_reads:
            temporary.cleanup()
            return self._failed(attempt, "reader child did not perform a file read")
        expected = path.read_text(encoding="utf-8").strip()
        actual = turn.final_message.strip()
        if actual != expected:
            temporary.cleanup()
            return self._failed(attempt, "reader child did not return exact file contents")

        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        content_digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
        result = TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={
                "text": actual,
                "session_id": turn.thread_id,
                "backend_id": "codex-exec",
                "model": self.model,
            },
            evidence=(
                f"file:sha256:{file_digest}",
                f"content:sha256:{content_digest}",
                "codex-tool:command_execution",
            ),
        )
        if self.keep_alive:
            self._temporary = temporary
            self._workspace = workspace
            self._thread_id = turn.thread_id
            self._attempt_id = attempt.attempt_id
        else:
            temporary.cleanup()
        return result

    def send(self, attempt: TaskAttempt, message: str) -> TaskResult:
        if (
            not self.keep_alive
            or self._temporary is None
            or self._workspace is None
            or self._thread_id is None
            or self._attempt_id != attempt.attempt_id
        ):
            return self._failed(attempt, "Codex child session is not active")
        output_path = Path(self._temporary.name) / "follow-up-result.txt"
        prompt = (
            "Continue as the same file-reader child. Answer the operator's "
            "follow-up directly in one concise sentence. Explain which granted "
            "capability enabled your earlier answer.\n\n"
            f"Operator message: {message}"
        )
        try:
            turn = _invoke_codex(
                executable=self.executable,
                model=self.model,
                reasoning=self.reasoning,
                timeout_seconds=self.timeout_seconds,
                workspace=self._workspace,
                output_path=output_path,
                prompt=prompt,
                disabled_features=_READER_DISABLED_FEATURES,
                ephemeral=False,
                resume_thread_id=self._thread_id,
                audit=self.audit,
                attempt_id=attempt.attempt_id,
            )
        except CodexDelegationError as exc:
            return self._failed(attempt, str(exc))
        if turn.thread_id != self._thread_id:
            return self._failed(attempt, "Codex child thread identity changed")
        text = turn.final_message.strip()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={
                "text": text,
                "session_id": turn.thread_id,
                "backend_id": "codex-exec",
                "model": self.model,
            },
            evidence=(
                "codex-session:resumed",
                f"content:sha256:{digest}",
            ),
        )

    def close(self) -> None:
        thread_id = self._thread_id
        attempt_id = self._attempt_id
        workspace = str(self._workspace) if self._workspace is not None else None
        if self._temporary is not None:
            self._temporary.cleanup()
        self._temporary = None
        self._workspace = None
        self._thread_id = None
        self._attempt_id = None
        if self.audit is not None and thread_id is not None:
            self.audit.append(
                "child_session_terminated",
                status="succeeded",
                payload={
                    "thread_id": thread_id,
                    "controller_handle_active": False,
                    "process_alive": False,
                    "workspace": workspace,
                    "workspace_removed": (
                        workspace is None or not Path(workspace).exists()
                    ),
                    "provider_thread_deleted": False,
                    "termination_scope": "controller_handle_and_workspace",
                },
                actor=AuditActor(
                    attempt_id or "codex-child",
                    "file_reader",
                    parent_id=(
                        attempt_id.rsplit("/child-", 1)[0]
                        if attempt_id and "/child-" in attempt_id
                        else None
                    ),
                ),
                attempt_id=attempt_id,
                session_id=thread_id,
                backend_id="codex-exec",
            )

    @staticmethod
    def _failed(attempt: TaskAttempt, error: str) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="failed",
            payload={"error": error},
        )


_READER_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "multi_agent",
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
    audit: AuditJournal | None = None,
    attempt_id: str | None = None,
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

    started_ns = monotonic_ns()
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
    if audit is not None:
        prompt_artifact = audit.write_artifact(
            "codex-child-prompt",
            prompt,
            media_type="text/plain",
        )
        stdout_artifact = audit.write_artifact(
            "codex-child-stdout",
            completed.stdout,
            media_type="application/x-ndjson",
        )
        stderr_artifact = audit.write_artifact(
            "codex-child-stderr",
            completed.stderr,
            media_type="text/plain",
        )
        executable_identity = {
            "path": codex,
            "sha256": _file_sha256(Path(codex)),
        }
        executable_artifact = audit.write_artifact(
            "codex-child-executable-identity",
            executable_identity,
        )
        audit.append(
            "backend_transport",
            status="succeeded" if completed.returncode == 0 else "failed",
            payload={
                "transport": "codex-exec",
                "argv": argv,
                "cwd": str(workspace),
                "returncode": completed.returncode,
                "model": model,
                "reasoning": reasoning,
                "executable": executable_identity,
                "resume_thread_id": resume_thread_id,
                "ephemeral": ephemeral,
            },
            actor=AuditActor(
                attempt_id or "codex-child",
                "file_reader",
                parent_id=(
                    attempt_id.rsplit("/child-", 1)[0]
                    if attempt_id and "/child-" in attempt_id
                    else None
                ),
            ),
            attempt_id=attempt_id,
            session_id=resume_thread_id,
            backend_id="codex-exec",
            duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
            artifacts=(
                prompt_artifact,
                stdout_artifact,
                stderr_artifact,
                executable_artifact,
            ),
        )
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
    turn = _CodexTurn(
        final_message=final_message,
        thread_id=thread_ids[0],
        item_types=tuple(item_types),
        commands=tuple(commands),
    )
    if audit is not None:
        final_artifact = audit.write_artifact(
            "codex-child-final-message",
            final_message,
            media_type="text/plain",
        )
        commands_artifact = audit.write_artifact(
            "codex-child-command-receipts",
            [
                {"command": command.command, "exit_code": command.exit_code}
                for command in commands
            ],
        )
        audit.append(
            "backend_turn_completed",
            status="succeeded",
            payload={
                "thread_id": turn.thread_id,
                "item_types": list(turn.item_types),
                "command_count": len(turn.commands),
            },
            actor=AuditActor(
                attempt_id or "codex-child",
                "file_reader",
                parent_id=(
                    attempt_id.rsplit("/child-", 1)[0]
                    if attempt_id and "/child-" in attempt_id
                    else None
                ),
            ),
            attempt_id=attempt_id,
            session_id=turn.thread_id,
            backend_id="codex-exec",
            artifacts=(final_artifact, commands_artifact),
        )
    return turn


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
