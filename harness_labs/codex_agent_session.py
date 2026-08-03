"""Resident Codex app-server implementation of the AgentSession contract."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .audit import AuditActor, AuditJournal
from .agent_sessions import (
    BackendCapabilities,
    BackendFailure,
    FinalOutput,
    ModelEvent,
    ModelRequest,
    ToolCall,
    ToolResult,
    Usage,
    tool_result_json,
)


class CodexSessionError(RuntimeError):
    """Raised when the app-server transport violates the session contract."""


_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "code_mode",
    "code_mode_host",
    "computer_use",
    "image_generation",
    "multi_agent",
    "shell_tool",
    "unified_exec",
)

_ALLOWED_ITEM_TYPES = frozenset(
    {"agentMessage", "dynamicToolCall", "reasoning", "userMessage"}
)


@dataclass
class _SessionState:
    thread_id: str
    turn_id: str
    final_text: str = ""
    pending_request_id: int | str | None = None
    pending_call_id: str | None = None
    usage: Usage | None = None


@dataclass
class CodexAppServerSession:
    """Keep one Codex app-server process resident across child execution."""

    model: str = "gpt-5.6-terra"
    reasoning: str = "low"
    executable: str = "codex"
    timeout_seconds: float = 180.0
    persistent_rollout: bool = True
    audit: AuditJournal | None = field(default=None, repr=False)
    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _workspace: tempfile.TemporaryDirectory[str] | None = field(
        default=None, init=False, repr=False
    )
    _messages: queue.Queue[dict[str, Any] | BaseException] = field(
        default_factory=queue.Queue, init=False, repr=False
    )
    _stderr: deque[str] = field(
        default_factory=lambda: deque(maxlen=50), init=False, repr=False
    )
    _next_id: int = field(default=1, init=False, repr=False)
    _state: _SessionState | None = field(default=None, init=False, repr=False)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_sessions=True,
            native_tool_calls=True,
            resumable_sessions=self.persistent_rollout,
            cached_input_reporting=True,
            structured_output=True,
            experimental_tool_transport=True,
        )

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def open(self, request: ModelRequest) -> str:
        if self._process is not None:
            raise CodexSessionError("session is already open")
        codex = shutil.which(self.executable)
        if codex is None:
            raise CodexSessionError(f"Codex executable not found: {self.executable}")
        if not request.tools:
            raise CodexSessionError("Codex parent requires a controller tool")

        self._workspace = tempfile.TemporaryDirectory(
            prefix="harness-codex-session-"
        )
        workspace = Path(self._workspace.name)
        isolated_codex_home = workspace / "codex-home"
        isolated_codex_home.mkdir(mode=0o700)
        auth_file = Path.home() / ".codex" / "auth.json"
        if auth_file.is_file():
            (isolated_codex_home / "auth.json").symlink_to(auth_file)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(isolated_codex_home)
        argv = [codex, "app-server", "--stdio", "--strict-config"]
        for feature in _DISABLED_FEATURES:
            argv.extend(["--disable", feature])
        try:
            self._process = subprocess.Popen(
                argv,
                cwd=self._workspace.name,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            self._cleanup()
            raise CodexSessionError(f"Codex app-server failed to start: {exc}") from exc
        if self.audit is not None:
            identity = {
                "path": codex,
                "sha256": _file_sha256(Path(codex)),
                "argv": argv,
                "model": self.model,
                "reasoning": self.reasoning,
                "persistent_rollout": self.persistent_rollout,
                "pid": self._process.pid,
            }
            artifact = self.audit.write_artifact(
                "codex-app-server-identity",
                identity,
            )
            self.audit.append(
                "backend_process_started",
                status="started",
                payload=identity,
                actor=AuditActor("codex-app-server", "backend"),
                backend_id="codex-app-server",
                artifacts=(artifact,),
            )

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self._rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "harness-labs",
                        "title": "Harness Labs",
                        "version": "0.1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized", "params": {}})
            thread = self._rpc(
                "thread/start",
                {
                    "model": self.model,
                    "cwd": self._workspace.name,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": not self.persistent_rollout,
                    "baseInstructions": (
                        "You are a bounded parent task attempt. You cannot read files "
                        "or run commands. Use only controller-provided child tools when "
                        "the task requires delegated access. Use spawn_children once "
                        "when independent child work can run concurrently, and wait for "
                        "the complete ordered result array before collating. Never guess "
                        "missing evidence. When send_child_message is available and the "
                        "task requires a follow-up, send the exact requested message to "
                        "the same child_attempt_id before finishing. Preserve the initial "
                        "child answer as the final answer when the task says to do so."
                    ),
                    "dynamicTools": [
                        {
                            "type": "function",
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": dict(tool.input_schema),
                        }
                        for tool in request.tools
                    ],
                },
            )
            thread_id = _nested_string(thread, "thread", "id")
            turn = self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                f"Task:\n{request.task}\n\nContext:\n"
                                f"{json.dumps(request.context, sort_keys=True)}"
                            ),
                        }
                    ],
                    "effort": self.reasoning,
                    "outputSchema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["answer"],
                        "properties": {
                            "answer": {"type": "string", "minLength": 1}
                        },
                    },
                },
            )
            turn_id = _nested_string(turn, "turn", "id")
            self._state = _SessionState(thread_id=thread_id, turn_id=turn_id)
            if self.audit is not None:
                self.audit.append(
                    "backend_session_identified",
                    status="succeeded",
                    payload={"thread_id": thread_id, "turn_id": turn_id},
                    actor=AuditActor("codex-app-server", "backend"),
                    session_id=thread_id,
                    backend_id="codex-app-server",
                )
            return thread_id
        except Exception:
            self._cleanup()
            raise

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ) -> ModelEvent:
        state = self._require_state(session_id)
        if tool_result is not None:
            if (
                state.pending_request_id is None
                or state.pending_call_id != tool_result.call_id
            ):
                return BackendFailure("tool result does not match a pending call")
            self._send(
                {
                    "id": state.pending_request_id,
                    "result": {
                        "contentItems": [
                            {
                                "type": "inputText",
                                "text": tool_result_json(tool_result),
                            }
                        ],
                        "success": tool_result.success,
                    },
                }
            )
            state.pending_request_id = None
            state.pending_call_id = None
        elif state.pending_request_id is not None:
            return BackendFailure("pending tool call requires a result")

        while True:
            try:
                message = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty:
                return BackendFailure("Codex app-server timed out")
            if isinstance(message, BaseException):
                return BackendFailure(str(message))
            method = message.get("method")
            params = message.get("params", {})
            if method == "item/tool/call":
                if state.pending_request_id is not None:
                    return BackendFailure("Codex issued concurrent tool calls")
                try:
                    name = params["tool"]
                    call_id = params["callId"]
                    arguments = params["arguments"]
                except (KeyError, TypeError):
                    return BackendFailure("Codex returned an invalid tool call")
                if not isinstance(name, str) or not isinstance(call_id, str):
                    return BackendFailure("Codex returned an invalid tool identity")
                if not isinstance(arguments, Mapping):
                    return BackendFailure("Codex tool arguments must be an object")
                state.pending_request_id = message.get("id")
                state.pending_call_id = call_id
                return ToolCall(call_id, name, dict(arguments))
            if method == "item/completed":
                item = params.get("item", {})
                item_type = item.get("type")
                if item_type not in _ALLOWED_ITEM_TYPES:
                    return BackendFailure(
                        f"Codex emitted an unauthorized item type: {item_type}"
                    )
                if item_type == "agentMessage" and isinstance(item.get("text"), str):
                    state.final_text = item["text"]
                continue
            if method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage", {}).get("total", {})
                try:
                    state.usage = Usage(
                        input_tokens=int(usage["inputTokens"]),
                        cached_input_tokens=int(usage["cachedInputTokens"]),
                        output_tokens=int(usage["outputTokens"]),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
                continue
            if method == "turn/completed":
                status = params.get("turn", {}).get("status")
                if status != "completed":
                    return BackendFailure(f"Codex turn ended with status: {status}")
                try:
                    payload = json.loads(state.final_text)
                    answer = payload["answer"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    return BackendFailure("Codex final output violated its schema")
                if not isinstance(answer, str) or not answer.strip():
                    return BackendFailure("Codex returned an empty answer")
                return FinalOutput(
                    answer.strip(),
                    usage=state.usage,
                    evidence=(
                        "codex-transport:app-server",
                        "codex-session:resident",
                    ),
                )
            if method is not None and "id" in message:
                return BackendFailure(f"Codex requested an unauthorized method: {method}")

    def close(self, session_id: str) -> None:
        if self._state is not None and self._state.thread_id != session_id:
            raise CodexSessionError("unknown session identity")
        self._cleanup()

    def _rpc(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": dict(params)})
        while True:
            try:
                message = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise CodexSessionError(f"Codex timed out during {method}") from exc
            if isinstance(message, BaseException):
                raise CodexSessionError(str(message))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CodexSessionError(f"Codex rejected {method}: {message['error']}")
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise CodexSessionError(f"Codex returned an invalid {method} response")
            return result

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            detail = " | ".join(self._stderr)
            raise CodexSessionError(f"Codex app-server is not running: {detail}")
        encoded = json.dumps(message, separators=(",", ":")) + "\n"
        if self.audit is not None:
            artifact = self.audit.write_artifact(
                "codex-app-server-outbound",
                encoded,
                media_type="application/x-ndjson",
            )
            self.audit.append(
                "transport_message",
                status="sent",
                payload={
                    "direction": "outbound",
                    "method": message.get("method"),
                    "request_id": message.get("id"),
                },
                actor=AuditActor("codex-app-server", "backend"),
                session_id=self._state.thread_id if self._state else None,
                backend_id="codex-app-server",
                artifacts=(artifact,),
            )
        process.stdin.write(encoded)
        process.stdin.flush()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if line.strip():
                    message = json.loads(line)
                    if self.audit is not None:
                        artifact = self.audit.write_artifact(
                            "codex-app-server-inbound",
                            line,
                            media_type="application/x-ndjson",
                        )
                        self.audit.append(
                            "transport_message",
                            status="received",
                            payload={
                                "direction": "inbound",
                                "method": message.get("method"),
                                "request_id": message.get("id"),
                            },
                            actor=AuditActor("codex-app-server", "backend"),
                            session_id=(
                                self._state.thread_id if self._state else None
                            ),
                            backend_id="codex-app-server",
                            artifacts=(artifact,),
                        )
                    self._messages.put(message)
        except (json.JSONDecodeError, OSError, RuntimeError) as exc:
            self._messages.put(CodexSessionError(f"invalid app-server output: {exc}"))

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if line.strip():
                self._stderr.append(line.strip())

    def _require_state(self, session_id: str) -> _SessionState:
        if self._state is None or self._state.thread_id != session_id:
            raise CodexSessionError("unknown session identity")
        return self._state

    def _cleanup(self) -> None:
        process = self._process
        state = self._state
        workspace_path = self._workspace.name if self._workspace is not None else None
        self._process = None
        self._state = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        returncode = process.returncode if process is not None else None
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None
        if self.audit is not None and process is not None:
            stderr_artifact = self.audit.write_artifact(
                "codex-app-server-stderr",
                "\n".join(self._stderr),
                media_type="text/plain",
            )
            self.audit.append(
                "backend_process_terminated",
                status="succeeded" if returncode is not None else "failed",
                payload={
                    "pid": process.pid,
                    "returncode": returncode,
                    "process_alive": process.poll() is None,
                    "workspace": workspace_path,
                    "workspace_removed": (
                        workspace_path is None or not Path(workspace_path).exists()
                    ),
                    "termination_scope": "resident_process_and_workspace",
                },
                actor=AuditActor("codex-app-server", "backend"),
                session_id=state.thread_id if state else None,
                backend_id="codex-app-server",
                artifacts=(stderr_artifact,),
            )


def _nested_string(value: Mapping[str, Any], key: str, nested_key: str) -> str:
    nested = value.get(key)
    if not isinstance(nested, Mapping) or not isinstance(
        nested.get(nested_key), str
    ):
        raise CodexSessionError(f"Codex response is missing {key}.{nested_key}")
    return nested[nested_key]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
