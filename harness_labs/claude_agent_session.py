"""Resident headless-Claude implementation of the AgentSession contract.

Claude Code executes tools internally — including MCP tools — so controller
tools cannot be injected the way Codex accepts ``dynamicTools``. Instead the
session owns a loopback HTTP MCP bridge (stdlib only): every controller
:class:`~harness_labs.agent_sessions.ToolSpec` is served as an MCP tool whose
``tools/call`` handler blocks until the harness supplies the matching
:class:`~harness_labs.agent_sessions.ToolResult` through ``step()``.

This inversion is safe because of a live-verified ordering fact (probed
2026-08-12 against claude 2.1.226): with ``--output-format stream-json`` the
assistant ``tool_use`` event is written to stdout *before* the MCP call is
awaited, so ``step()`` can surface the tool intent while the bridge handler
holds Claude's tool call open.
"""

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
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
    ToolSpec,
    Usage,
    tool_result_json,
)
from .usage import ModelPrice, parse_claude_result_usage


class ClaudeSessionError(RuntimeError):
    """Raised when the headless-Claude transport violates the session contract."""


_BRIDGE_SERVER_NAME = "controller"
_TOOL_PREFIX = f"mcp__{_BRIDGE_SERVER_NAME}__"

_ANSWER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
}

_DEFAULT_BASE_INSTRUCTIONS = (
    "You are a bounded parent task attempt. You cannot read files "
    "or run commands. Use only controller-provided child tools when "
    "the task requires delegated access. Use spawn_children once "
    "when independent child work can run concurrently, and wait for "
    "the complete ordered result array before collating. Never guess "
    "missing evidence. When send_child_message is available and the "
    "task requires a follow-up, send the exact requested message to "
    "the same child_attempt_id before finishing. Preserve the initial "
    "child answer as the final answer when the task says to do so."
)


@dataclass
class _BridgeCall:
    """One MCP tools/call held open until the harness delivers its result."""

    name: str
    arguments_json: str
    ready: threading.Event = field(default_factory=threading.Event)
    response: Mapping[str, Any] | None = None


class _LoopbackToolBridge:
    """Serve controller tools over loopback MCP; block tools/call until stepped."""

    def __init__(
        self,
        tools: tuple[ToolSpec, ...],
        unavailable_tool_response: str,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._unavailable = unavailable_tool_response
        self._condition = threading.Condition()
        self._pending: list[_BridgeCall] = []
        self._closed = False
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> str:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length", 0))
                try:
                    message = json.loads(self.rfile.read(length)) if length else {}
                except json.JSONDecodeError:
                    self.send_error(400)
                    return
                if message.get("id") is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = bridge._handle(message)
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *arguments: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/mcp"

    def deliver(
        self,
        name: str,
        arguments_json: str,
        text: str,
        *,
        is_error: bool,
        timeout: float,
    ) -> bool:
        """Answer the blocked tools/call matching one issued ToolCall."""

        deadline = monotonic() + timeout
        with self._condition:
            while True:
                for call in self._pending:
                    if call.name == name and call.arguments_json == arguments_json:
                        call.response = {
                            "content": [{"type": "text", "text": text}],
                            "isError": is_error,
                        }
                        self._pending.remove(call)
                        call.ready.set()
                        return True
                remaining = deadline - monotonic()
                if remaining <= 0 or self._closed:
                    return False
                self._condition.wait(timeout=remaining)

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            for call in self._pending:
                call.response = {
                    "content": [{"type": "text", "text": "session closed"}],
                    "isError": True,
                }
                call.ready.set()
            self._pending.clear()
            self._condition.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _handle(self, message: Mapping[str, Any]) -> Mapping[str, Any]:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _BRIDGE_SERVER_NAME, "version": "0.1"},
            }
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": dict(tool.input_schema),
                    }
                    for tool in self._tools.values()
                ]
            }
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name not in self._tools or not isinstance(arguments, Mapping):
                return {
                    "content": [{"type": "text", "text": self._unavailable}],
                    "isError": True,
                }
            call = _BridgeCall(
                name=name,
                arguments_json=json.dumps(dict(arguments), sort_keys=True),
            )
            with self._condition:
                if self._closed:
                    return {
                        "content": [{"type": "text", "text": "session closed"}],
                        "isError": True,
                    }
                self._pending.append(call)
                self._condition.notify_all()
            call.ready.wait()
            assert call.response is not None
            return call.response
        return {}


@dataclass
class _SessionState:
    session_id: str
    pending_call_id: str | None = None
    pending_tool_name: str | None = None
    pending_arguments_json: str | None = None


@dataclass
class ClaudeAgentSession:
    """Keep one headless Claude process resident across child execution."""

    model: str = "claude-sonnet-5"
    effort: str = "medium"
    executable: str = "claude"
    timeout_seconds: float = 180.0
    tool_timeout_seconds: float = 86400.0
    max_budget_usd: float | None = None
    base_instructions: str | None = None
    audit: AuditJournal | None = field(default=None, repr=False)
    pricing: ModelPrice | None = None
    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _workspace: tempfile.TemporaryDirectory[str] | None = field(
        default=None, init=False, repr=False
    )
    _bridge: _LoopbackToolBridge | None = field(default=None, init=False, repr=False)
    _events: queue.Queue[tuple[str, Any] | BaseException] = field(
        default_factory=queue.Queue, init=False, repr=False
    )
    _stderr: deque[str] = field(
        default_factory=lambda: deque(maxlen=50), init=False, repr=False
    )
    _tool_names: frozenset[str] = field(
        default=frozenset(), init=False, repr=False
    )
    _state: _SessionState | None = field(default=None, init=False, repr=False)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_sessions=True,
            native_tool_calls=True,
            resumable_sessions=False,
            cached_input_reporting=True,
            structured_output=True,
            experimental_tool_transport=True,
        )

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def open(self, request: ModelRequest) -> str:
        if self._process is not None:
            raise ClaudeSessionError("session is already open")
        claude = shutil.which(self.executable)
        if claude is None:
            raise ClaudeSessionError(f"Claude executable not found: {self.executable}")
        if not request.tools:
            raise ClaudeSessionError("Claude parent requires a controller tool")

        self._tool_names = frozenset(tool.name for tool in request.tools)
        self._bridge = _LoopbackToolBridge(
            request.tools, request.unavailable_tool_response
        )
        bridge_url = self._bridge.start()
        self._workspace = tempfile.TemporaryDirectory(
            prefix="harness-claude-session-"
        )

        prompt = (
            f"Task:\n{request.task}\n\nContext:\n"
            f"{json.dumps(request.context, sort_keys=True)}"
        )
        mcp_config = json.dumps(
            {
                "mcpServers": {
                    _BRIDGE_SERVER_NAME: {"type": "http", "url": bridge_url}
                }
            }
        )
        argv = [
            claude,
            "-p",
            prompt,
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--output-format",
            "stream-json",
            "--verbose",
            "--tools",
            "",
            "--setting-sources",
            "",
            "--mcp-config",
            mcp_config,
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(f"{_TOOL_PREFIX}{tool.name}" for tool in request.tools),
            "--json-schema",
            json.dumps(_ANSWER_SCHEMA),
            "--no-session-persistence",
            "--system-prompt",
            self.base_instructions or _DEFAULT_BASE_INSTRUCTIONS,
        ]
        if self.max_budget_usd is not None:
            argv.extend(["--max-budget-usd", str(self.max_budget_usd)])
        environment = os.environ.copy()
        tool_timeout_ms = str(int(self.tool_timeout_seconds * 1000))
        environment["MCP_TIMEOUT"] = "30000"
        environment["MCP_TOOL_TIMEOUT"] = tool_timeout_ms

        try:
            self._process = subprocess.Popen(
                argv,
                cwd=self._workspace.name,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            self._cleanup()
            raise ClaudeSessionError(f"Claude failed to start: {exc}") from exc
        if self.audit is not None:
            identity = {
                "path": claude,
                "sha256": _file_sha256(Path(claude)),
                "argv": argv,
                "model": self.model,
                "effort": self.effort,
                "bridge_url": bridge_url,
                "pid": self._process.pid,
            }
            identity_artifact = self.audit.write_artifact(
                "claude-session-identity", identity
            )
            prompt_artifact = self.audit.write_artifact(
                "claude-session-prompt", prompt, media_type="text/plain"
            )
            self.audit.append(
                "backend_process_started",
                status="started",
                payload=identity,
                actor=AuditActor("claude-session", "backend"),
                backend_id="claude-session",
                artifacts=(identity_artifact, prompt_artifact),
            )

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            session_id = self._await_init()
            self._state = _SessionState(session_id=session_id)
            if self.audit is not None:
                self.audit.append(
                    "backend_session_identified",
                    status="succeeded",
                    payload={"session_id": session_id},
                    actor=AuditActor("claude-session", "backend"),
                    session_id=session_id,
                    backend_id="claude-session",
                )
            return session_id
        except Exception:
            self._cleanup()
            raise

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ) -> ModelEvent:
        state = self._require_state(session_id)
        bridge = self._bridge
        if bridge is None:
            return BackendFailure("Claude session bridge is not running")
        if tool_result is not None:
            if (
                state.pending_call_id is None
                or state.pending_call_id != tool_result.call_id
                or state.pending_tool_name is None
                or state.pending_arguments_json is None
            ):
                return BackendFailure("tool result does not match a pending call")
            text = tool_result_json(tool_result)
            delivered = bridge.deliver(
                state.pending_tool_name,
                state.pending_arguments_json,
                text,
                is_error=not tool_result.success,
                timeout=self.timeout_seconds,
            )
            if not delivered:
                return BackendFailure(
                    "no bridged Claude tool call matched the pending result"
                )
            if self.audit is not None:
                artifact = self.audit.write_artifact(
                    "claude-bridge-outbound", text, media_type="application/json"
                )
                self.audit.append(
                    "transport_message",
                    status="sent",
                    payload={
                        "direction": "outbound",
                        "call_id": tool_result.call_id,
                        "tool": state.pending_tool_name,
                        "success": tool_result.success,
                    },
                    actor=AuditActor("claude-session", "backend"),
                    session_id=state.session_id,
                    backend_id="claude-session",
                    artifacts=(artifact,),
                )
            state.pending_call_id = None
            state.pending_tool_name = None
            state.pending_arguments_json = None
        elif state.pending_call_id is not None:
            return BackendFailure("pending tool call requires a result")

        while True:
            try:
                event = self._events.get(timeout=self.timeout_seconds)
            except queue.Empty:
                return BackendFailure("Claude session timed out")
            if isinstance(event, BaseException):
                return BackendFailure(str(event))
            kind, payload = event
            if kind == "tool_use":
                if state.pending_call_id is not None:
                    return BackendFailure("Claude issued concurrent tool calls")
                name = payload.get("name")
                call_id = payload.get("id")
                arguments = payload.get("input") or {}
                if not isinstance(name, str) or not isinstance(call_id, str):
                    return BackendFailure("Claude returned an invalid tool identity")
                if name == "StructuredOutput":
                    # The internal delivery mechanism for --json-schema output;
                    # the result envelope that follows carries the same object.
                    continue
                if not name.startswith(_TOOL_PREFIX):
                    return BackendFailure(
                        f"Claude called an unauthorized tool: {name}"
                    )
                bare_name = name[len(_TOOL_PREFIX):]
                if bare_name not in self._tool_names:
                    return BackendFailure(
                        f"Claude called an unauthorized tool: {name}"
                    )
                if not isinstance(arguments, Mapping):
                    return BackendFailure("Claude tool arguments must be an object")
                state.pending_call_id = call_id
                state.pending_tool_name = bare_name
                state.pending_arguments_json = json.dumps(
                    dict(arguments), sort_keys=True
                )
                return ToolCall(call_id, bare_name, dict(arguments))
            if kind == "result":
                return self._final_output(payload)
            if kind == "eof":
                detail = " | ".join(self._stderr)
                return BackendFailure(
                    f"Claude exited without a result: {detail}"
                )

    def close(self, session_id: str) -> None:
        if self._state is not None and self._state.session_id != session_id:
            raise ClaudeSessionError("unknown session identity")
        self._cleanup()

    def _final_output(self, envelope: Mapping[str, Any]) -> ModelEvent:
        if envelope.get("is_error"):
            return BackendFailure(
                f"Claude result error: {envelope.get('result')}"
            )
        structured = envelope.get("structured_output")
        if not isinstance(structured, Mapping):
            try:
                structured = json.loads(envelope.get("result") or "")
            except (json.JSONDecodeError, TypeError):
                structured = None
        answer = (
            structured.get("answer") if isinstance(structured, Mapping) else None
        )
        if not isinstance(answer, str) or not answer.strip():
            return BackendFailure("Claude final output violated its schema")
        usage_map = parse_claude_result_usage(envelope)
        usage = (
            Usage(
                input_tokens=usage_map["input_tokens"],
                cached_input_tokens=usage_map["cached_input_tokens"],
                output_tokens=usage_map["output_tokens"],
            )
            if usage_map is not None
            else None
        )
        return FinalOutput(
            answer.strip(),
            usage=usage,
            evidence=(
                "claude-transport:stream-json",
                "claude-session:resident",
            ),
        )

    def _await_init(self) -> str:
        try:
            event = self._events.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            raise ClaudeSessionError("Claude never reported its session") from exc
        if isinstance(event, BaseException):
            raise ClaudeSessionError(str(event))
        kind, payload = event
        if kind != "init" or not isinstance(payload.get("session_id"), str):
            detail = " | ".join(self._stderr)
            raise ClaudeSessionError(
                f"Claude did not initialize a session: {detail}"
            )
        return payload["session_id"]

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                message = json.loads(line)
                if self.audit is not None:
                    artifact = self.audit.write_artifact(
                        "claude-stream-inbound",
                        line,
                        media_type="application/x-ndjson",
                    )
                    self.audit.append(
                        "transport_message",
                        status="received",
                        payload={
                            "direction": "inbound",
                            "type": message.get("type"),
                            "subtype": message.get("subtype"),
                        },
                        actor=AuditActor("claude-session", "backend"),
                        session_id=(
                            self._state.session_id if self._state else None
                        ),
                        backend_id="claude-session",
                        artifacts=(artifact,),
                    )
                message_type = message.get("type")
                if message_type == "system" and message.get("subtype") == "init":
                    self._events.put(("init", message))
                elif message_type == "assistant":
                    content = message.get("message", {}).get("content", [])
                    for block in content:
                        if (
                            isinstance(block, Mapping)
                            and block.get("type") == "tool_use"
                        ):
                            self._events.put(("tool_use", block))
                elif message_type == "result":
                    self._events.put(("result", message))
        except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
            self._events.put(ClaudeSessionError(f"invalid Claude output: {exc}"))
        self._events.put(("eof", None))

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if line.strip():
                self._stderr.append(line.strip())

    def _require_state(self, session_id: str) -> _SessionState:
        if self._state is None or self._state.session_id != session_id:
            raise ClaudeSessionError("unknown session identity")
        return self._state

    def _cleanup(self) -> None:
        process = self._process
        state = self._state
        workspace_path = self._workspace.name if self._workspace is not None else None
        self._process = None
        self._state = None
        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None
        if process is not None and process.poll() is None:
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
                "claude-session-stderr",
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
                        workspace_path is None
                        or not Path(workspace_path).exists()
                    ),
                    "termination_scope": "resident_process_and_bridge",
                },
                actor=AuditActor("claude-session", "backend"),
                session_id=state.session_id if state else None,
                backend_id="claude-session",
                artifacts=(stderr_artifact,),
            )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
