"""oMLX AgentSession with provider-neutral JSON tool-call emulation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from harness_labs.core.agent_sessions import (
    BackendCapabilities,
    BackendFailure,
    FinalOutput,
    ModelEvent,
    ModelRequest,
    ToolCall,
    ToolResult,
    Usage,
)
from harness_labs.core.backends import OmlxBackend
from harness_labs.core.text_executor import TextBackendError


@dataclass
class OmlxAgentSession:
    """Adapt local text completion to the same session boundary, without tools."""

    backend: OmlxBackend = field(
        default_factory=lambda: OmlxBackend(max_tokens=128, temperature=0.0)
    )
    _request: ModelRequest | None = field(default=None, init=False, repr=False)
    _completed: bool = field(default=False, init=False, repr=False)
    _pending_call_id: str | None = field(default=None, init=False, repr=False)
    _pending_tool_name: str | None = field(default=None, init=False, repr=False)
    _tool_results: list[dict[str, Any]] = field(
        default_factory=list, init=False, repr=False
    )
    _session_id: str = field(default="omlx:single-turn", init=False, repr=False)
    _usage: Usage = field(
        default_factory=lambda: Usage(0, 0, 0), init=False, repr=False
    )

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
        if not 1 <= len(request.tools) <= 2:
            raise RuntimeError("oMLX session requires one or two controller tools")
        self._request = request
        self._completed = False
        self._pending_call_id = None
        self._pending_tool_name = None
        self._tool_results.clear()
        self._usage = Usage(0, 0, 0)
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
        pending_tool_name = self._pending_tool_name
        child_payload = dict(tool_result.payload)
        self._tool_results.append(child_payload)
        self._pending_call_id = None
        self._pending_tool_name = None
        if pending_tool_name == "spawn_children":
            return self._complete_batch(child_payload)
        if len(self._tool_results) == 1 and any(
            tool.name == "send_child_message" for tool in self._request.tools
        ):
            return self._request_followup(child_payload)
        self._completed = True
        task = (
            "Complete the original task using only the authorized child result. "
            "Return exactly the child result's payload.text value, with no "
            "commentary.\n\n"
            f"Original task:\n{self._request.task}\n\n"
            f"Initial child result:\n"
            f"{json.dumps(self._tool_results[0], sort_keys=True)}\n\n"
            f"Follow-up child result:\n{json.dumps(child_payload, sort_keys=True)}"
        )
        try:
            text = self._generate(task, self._request.context).strip()
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        expected = self._tool_results[0].get("payload", {}).get("text")
        if not isinstance(expected, str):
            return BackendFailure("oMLX child result did not contain text")
        if text != expected:
            correction = (
                "Your previous output changed the initial child answer. Return "
                "exactly the following text and nothing else:\n"
                f"{expected}"
            )
            try:
                text = self._generate(correction, {}).strip()
            except TextBackendError as exc:
                return BackendFailure(str(exc))
        if text != expected:
            return BackendFailure("oMLX did not faithfully return the child result")
        return FinalOutput(
            text,
            usage=self._usage,
            evidence=("omlx-transport:openai-compatible-http",),
        )

    def close(self, session_id: str) -> None:
        if self._request is not None and session_id != self._session_id:
            raise RuntimeError("unknown oMLX session identity")
        self._request = None
        self._completed = False
        self._pending_call_id = None
        self._pending_tool_name = None
        self._tool_results.clear()
        self._usage = Usage(0, 0, 0)

    def _request_child(self) -> ModelEvent:
        assert self._request is not None
        tool = next(
            tool
            for tool in self._request.tools
            if tool.name in {"spawn_child", "spawn_children"}
        )
        if tool.name == "spawn_children":
            return self._request_batch(tool)
        task = (
            "Select the one controller tool needed to perform the task. Return "
            "only a JSON object with keys role, objective, and context. Select a "
            "role allowed by the supplied schema. Put the exact text the child "
            "needs to start the task in context; use an empty string when no "
            "context is needed.\n\n"
            f"Task:\n{self._request.task}\n\n"
            f"Tool schema:\n{json.dumps(dict(tool.input_schema), sort_keys=True)}"
        )
        try:
            text = self._generate(task, self._request.context).strip()
            payload: Any = json.loads(text)
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        except json.JSONDecodeError:
            return BackendFailure("oMLX child request is not valid JSON")
        if not isinstance(payload, dict):
            return BackendFailure("oMLX child request must be an object")
        role = payload.get("role")
        objective = payload.get("objective")
        context = payload.get("context")
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
            or not isinstance(context, str)
        ):
            return BackendFailure("oMLX returned an invalid child request")
        self._pending_call_id = "omlx:child-1"
        self._pending_tool_name = tool.name
        return ToolCall(
            call_id=self._pending_call_id,
            name=tool.name,
            arguments={"role": role, "objective": objective, "context": context},
        )

    def _request_batch(self, tool) -> ModelEvent:
        assert self._request is not None
        task = (
            "Select the independent controller child tasks needed to perform the "
            "task. Return only a JSON object with key requests, whose value is an "
            "array of objects with keys role, objective, and context. Put the exact "
            "text each child needs to start its task in context; use an empty string "
            "when none is needed. Use each required role once and obey the supplied "
            "schema.\n\n"
            f"Task:\n{self._request.task}\n\n"
            f"Tool schema:\n{json.dumps(dict(tool.input_schema), sort_keys=True)}"
        )
        try:
            text = self._generate(task, self._request.context).strip()
            payload: Any = json.loads(text)
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        except json.JSONDecodeError:
            return BackendFailure("oMLX child batch request is not valid JSON")
        requests = payload.get("requests") if isinstance(payload, dict) else None
        properties = tool.input_schema.get("properties", {})
        request_schema = (
            properties.get("requests", {}) if isinstance(properties, dict) else {}
        )
        item_schema = (
            request_schema.get("items", {})
            if isinstance(request_schema, dict)
            else {}
        )
        item_properties = (
            item_schema.get("properties", {})
            if isinstance(item_schema, dict)
            else {}
        )
        role_schema = (
            item_properties.get("role", {})
            if isinstance(item_properties, dict)
            else {}
        )
        allowed_roles = (
            role_schema.get("enum", []) if isinstance(role_schema, dict) else []
        )
        max_items = (
            request_schema.get("maxItems", len(allowed_roles))
            if isinstance(request_schema, dict)
            else 0
        )
        if (
            not isinstance(requests, list)
            or not isinstance(max_items, int)
            or not 1 <= len(requests) <= max_items
            or any(
                not isinstance(request, dict)
                or request.get("role") not in allowed_roles
                or not isinstance(request.get("objective"), str)
                or not request["objective"].strip()
                or not isinstance(request.get("context"), str)
                for request in requests
            )
        ):
            return BackendFailure("oMLX returned an invalid child batch request")
        self._pending_call_id = "omlx:batch-1"
        self._pending_tool_name = tool.name
        return ToolCall(
            call_id=self._pending_call_id,
            name=tool.name,
            arguments={"requests": requests},
        )

    def _complete_batch(self, child_payload: dict[str, Any]) -> ModelEvent:
        assert self._request is not None
        self._completed = True
        task = (
            "Complete the original task by collating every ordered authorized "
            "child result below. Do not claim evidence absent from those results. "
            "Return a concise direct answer.\n\n"
            f"Original task:\n{self._request.task}\n\n"
            f"Child batch result:\n{json.dumps(child_payload, sort_keys=True)}"
        )
        try:
            text = self._generate(task, self._request.context).strip()
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        if not text:
            return BackendFailure("oMLX returned no batch collation")
        return FinalOutput(
            text,
            usage=self._usage,
            evidence=("omlx-transport:openai-compatible-http",),
        )

    def _request_followup(self, child_payload: dict[str, Any]) -> ModelEvent:
        assert self._request is not None
        tool = next(
            tool
            for tool in self._request.tools
            if tool.name == "send_child_message"
        )
        child_attempt_id = child_payload.get("attempt_id")
        expected_message = "what enabled you to answer me this way?"
        task = (
            "The task requires a follow-up to the retained child. Return only a "
            "JSON object with keys child_attempt_id and message. Use the supplied "
            "child_attempt_id and this exact message: "
            f"{expected_message}\n\n"
            f"Initial child result:\n{json.dumps(child_payload, sort_keys=True)}\n\n"
            f"Tool schema:\n{json.dumps(dict(tool.input_schema), sort_keys=True)}"
        )
        try:
            text = self._generate(task, self._request.context).strip()
            payload: Any = json.loads(text)
        except TextBackendError as exc:
            return BackendFailure(str(exc))
        except json.JSONDecodeError:
            return BackendFailure("oMLX follow-up request is not valid JSON")
        if (
            not isinstance(payload, dict)
            or payload.get("child_attempt_id") != child_attempt_id
            or payload.get("message") != expected_message
        ):
            return BackendFailure("oMLX returned an invalid child follow-up")
        self._pending_call_id = "omlx:child-message-1"
        self._pending_tool_name = tool.name
        return ToolCall(
            call_id=self._pending_call_id,
            name=tool.name,
            arguments={
                "child_attempt_id": child_attempt_id,
                "message": expected_message,
            },
        )

    def _generate(self, task: str, context: dict[str, Any] | Any) -> str:
        text = self.backend.generate(task, context)
        usage = getattr(self.backend, "last_usage", None)
        if usage is not None:
            self._usage = Usage(
                self._usage.input_tokens + usage.input_tokens,
                self._usage.cached_input_tokens + usage.cached_input_tokens,
                self._usage.output_tokens + usage.output_tokens,
            )
        return text
