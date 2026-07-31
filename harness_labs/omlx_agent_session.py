"""oMLX AgentSession with provider-neutral JSON tool-call emulation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .agent_sessions import (
    BackendCapabilities,
    BackendFailure,
    FinalOutput,
    ModelEvent,
    ModelRequest,
    ToolCall,
    ToolResult,
)
from .backends import OmlxBackend
from .text_executor import TextBackendError


@dataclass
class OmlxAgentSession:
    """Adapt local text completion to the same session boundary, without tools."""

    backend: OmlxBackend = field(
        default_factory=lambda: OmlxBackend(max_tokens=32, temperature=0.0)
    )
    _request: ModelRequest | None = field(default=None, init=False, repr=False)
    _completed: bool = field(default=False, init=False, repr=False)
    _pending_call_id: str | None = field(default=None, init=False, repr=False)
    _session_id: str = field(default="omlx:single-turn", init=False, repr=False)

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            persistent_sessions=False,
            native_tool_calls=False,
            resumable_sessions=False,
            cached_input_reporting=False,
            structured_output=False,
        )

    def open(self, request: ModelRequest) -> str:
        if self._request is not None:
            raise RuntimeError("session is already open")
        if len(request.tools) != 1:
            raise RuntimeError("oMLX session requires exactly one controller tool")
        self._request = request
        self._completed = False
        self._pending_call_id = None
        return self._session_id

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ) -> ModelEvent:
        if self._request is None or session_id != self._session_id:
            return BackendFailure("unknown oMLX session identity")
        if self._completed:
            return BackendFailure("oMLX session is already complete")
        if tool_result is None:
            return self._request_child()
        if (
            self._pending_call_id is None
            or tool_result.call_id != self._pending_call_id
        ):
            return BackendFailure("tool result does not match the pending oMLX call")
        self._completed = True
        child_payload = dict(tool_result.payload)
        task = (
            "Complete the original task using only the authorized child result. "
            "Return exactly the child result's payload.text value, with no "
            "commentary.\n\n"
            f"Original task:\n{self._request.task}\n\n"
            f"Child result:\n{json.dumps(child_payload, sort_keys=True)}"
        )
        try:
            text = self.backend.generate(task, self._request.context).strip()
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        expected = child_payload.get("payload", {}).get("text")
        if not isinstance(expected, str) or text != expected:
            return BackendFailure("oMLX did not faithfully return the child result")
        return FinalOutput(
            text,
            evidence=("omlx-transport:openai-compatible-http",),
        )

    def close(self, session_id: str) -> None:
        if self._request is not None and session_id != self._session_id:
            raise RuntimeError("unknown oMLX session identity")
        self._request = None
        self._completed = False
        self._pending_call_id = None

    def _request_child(self) -> ModelEvent:
        assert self._request is not None
        tool = self._request.tools[0]
        task = (
            "Select the one controller tool needed to perform the task. Return "
            "only a JSON object with keys role and objective. Select a role "
            "allowed by the supplied schema.\n\n"
            f"Task:\n{self._request.task}\n\n"
            f"Tool schema:\n{json.dumps(dict(tool.input_schema), sort_keys=True)}"
        )
        try:
            text = self.backend.generate(task, self._request.context).strip()
            payload: Any = json.loads(text)
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        except json.JSONDecodeError:
            return BackendFailure("oMLX child request is not valid JSON")
        if not isinstance(payload, dict):
            return BackendFailure("oMLX child request must be an object")
        role = payload.get("role")
        objective = payload.get("objective")
        properties = tool.input_schema.get("properties", {})
        role_schema = (
            properties.get("role", {}) if isinstance(properties, dict) else {}
        )
        allowed_roles = (
            role_schema.get("enum", []) if isinstance(role_schema, dict) else []
        )
        if (
            role not in allowed_roles
            or not isinstance(objective, str)
            or not objective
        ):
            return BackendFailure("oMLX returned an invalid child request")
        self._pending_call_id = "omlx:child-1"
        return ToolCall(
            call_id=self._pending_call_id,
            name=tool.name,
            arguments={"role": role, "objective": objective},
        )
