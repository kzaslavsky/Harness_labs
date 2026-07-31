"""Provider-neutral resident agent sessions and controller-owned tool loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, TypeAlias

from .attempts import TaskAttempt, TaskResult
from .composition import ChildDispatcher, ChildRequest, ChildRequestDenied
from .text_executor import InMemoryReferenceStore


TOOL_UNAVAILABLE_REFUSAL = "sorry, I cannot do that, Dave."


@dataclass(frozen=True)
class BackendCapabilities:
    """Transport features and capabilities available to authorized children."""

    persistent_sessions: bool
    native_tool_calls: bool
    resumable_sessions: bool
    cached_input_reporting: bool
    structured_output: bool
    experimental_tool_transport: bool = False
    child_capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolSpec:
    """One controller-defined tool exposed through a backend transport."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    """Provider-neutral input used to open one parent session."""

    task: str
    context: Mapping[str, Any]
    tools: tuple[ToolSpec, ...] = ()
    unavailable_tool_response: str = TOOL_UNAVAILABLE_REFUSAL


@dataclass(frozen=True)
class Usage:
    """Normalized token usage reported by a backend."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    success: bool
    payload: Mapping[str, Any]
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalOutput:
    content: str
    usage: Usage | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendFailure:
    error: str


ModelEvent: TypeAlias = ToolCall | FinalOutput | BackendFailure


class AgentSession(Protocol):
    """The only provider-specific interface used by the tool-loop controller."""

    @property
    def capabilities(self) -> BackendCapabilities:
        """Return immutable transport capabilities."""

    def open(self, request: ModelRequest) -> str:
        """Open a session and return its provider identity."""

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ) -> ModelEvent:
        """Return the next model event, optionally after supplying a tool result."""

    def close(self, session_id: str) -> None:
        """Release the session and its resident process, if any."""


@dataclass(frozen=True)
class SessionToolExecutor:
    """Resolve a parent attempt and run its provider-neutral session tool loop."""

    store: InMemoryReferenceStore
    session: AgentSession
    dispatcher: ChildDispatcher
    max_tool_calls: int = 1
    _tool_name: str = field(default="spawn_child", init=False, repr=False)

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        resolved = self._resolve(attempt)
        if isinstance(resolved, TaskResult):
            return resolved
        task, context, grant = resolved

        granted_roles = grant.get("child_roles", ())
        if not isinstance(granted_roles, (list, tuple)):
            return self._failed(attempt, "child_roles must be a list or tuple")
        allowed_roles = tuple(
            role for role in self.dispatcher.allowed_roles if role in granted_roles
        )
        if not allowed_roles:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "no authorized child role is available"},
            )

        tools = (
            ToolSpec(
                name=self._tool_name,
                description="Run one controller-authorized child task.",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["role", "objective"],
                    "properties": {
                        "role": {
                            "type": "string",
                            "enum": list(allowed_roles),
                        },
                        "objective": {"type": "string", "minLength": 1},
                    },
                },
            ),
        )

        request = ModelRequest(task=task, context=context, tools=tools)
        session_id: str | None = None
        child_result: TaskResult | None = None
        tool_calls = 0
        try:
            session_id = self.session.open(request)
            event = self.session.step(session_id)
            while isinstance(event, ToolCall):
                tool_calls += 1
                if tool_calls > self.max_tool_calls:
                    return self._failed(attempt, "maximum parent tool calls exceeded")
                tool_result, child_result = self._run_tool(
                    attempt,
                    event,
                    allowed_roles,
                )
                event = self.session.step(session_id, tool_result)

            if isinstance(event, BackendFailure):
                return self._failed(attempt, f"backend failed: {event.error}")
            if not isinstance(event, FinalOutput):
                return self._failed(attempt, "backend returned an unknown event")

            answer = event.content.strip()
            if not answer:
                return self._failed(attempt, "backend returned no answer")
            if child_result is None:
                return self._failed(attempt, "parent backend did not call child")
            if child_result.status != "succeeded":
                return self._failed(attempt, "authorized child did not succeed")
            return self._succeeded(
                attempt,
                answer,
                session_id,
                event,
                child_result,
            )
        except ChildRequestDenied as exc:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": str(exc)},
            )
        except Exception as exc:
            return self._failed(attempt, f"session execution failed: {exc}")
        finally:
            if session_id is not None:
                self.session.close(session_id)

    def _resolve(
        self,
        attempt: TaskAttempt,
    ) -> tuple[str, Mapping[str, Any], Mapping[str, Any]] | TaskResult:
        try:
            task = self.store.resolve(attempt.task_ref)
            context = self.store.resolve(attempt.context_ref)
            grant = self.store.resolve(attempt.grant_ref)
        except KeyError as exc:
            return self._failed(attempt, f"unresolved reference: {exc.args[0]}")
        if not isinstance(task, str) or not task.strip():
            return self._failed(attempt, "task must resolve to non-empty text")
        if not isinstance(context, Mapping):
            return self._failed(attempt, "context must resolve to a mapping")
        if not isinstance(grant, Mapping) or "spawn_child" not in grant.get(
            "capabilities", ()
        ):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "spawn_child capability is required"},
            )
        return task, context, grant

    def _run_tool(
        self,
        parent: TaskAttempt,
        call: ToolCall,
        allowed_roles: tuple[str, ...],
    ) -> tuple[ToolResult, TaskResult | None]:
        if call.name != self._tool_name:
            return (
                ToolResult(
                    call_id=call.call_id,
                    success=False,
                    payload={"error": f"unknown tool: {call.name}"},
                ),
                None,
            )
        role = call.arguments.get("role")
        objective = call.arguments.get("objective")
        if role not in allowed_roles or not isinstance(objective, str):
            return (
                ToolResult(
                    call_id=call.call_id,
                    success=False,
                    payload={"error": "invalid child request"},
                ),
                None,
            )
        child = self.dispatcher.run_child(
            parent,
            ChildRequest(role=role, objective=objective),
        )
        return (
            ToolResult(
                call_id=call.call_id,
                success=child.status == "succeeded",
                payload={
                    "attempt_id": child.attempt_id,
                    "status": child.status,
                    "payload": dict(child.payload),
                },
                evidence=child.evidence,
            ),
            child,
        )

    @staticmethod
    def _succeeded(
        attempt: TaskAttempt,
        answer: str,
        session_id: str,
        event: FinalOutput,
        child_result: TaskResult | None,
    ) -> TaskResult:
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        evidence = (*event.evidence, f"parent-content:sha256:{digest}")
        payload: dict[str, Any] = {
            "text": answer,
            "session_id": session_id,
        }
        if event.usage is not None:
            payload["usage"] = {
                "input_tokens": event.usage.input_tokens,
                "cached_input_tokens": event.usage.cached_input_tokens,
                "output_tokens": event.usage.output_tokens,
            }
        if child_result is not None:
            payload["child_attempt_id"] = child_result.attempt_id
            evidence = (*child_result.evidence, *evidence)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload=payload,
            evidence=evidence,
        )

    @staticmethod
    def _failed(attempt: TaskAttempt, error: str) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="failed",
            payload={"error": error},
        )


def tool_result_json(result: ToolResult) -> str:
    """Serialize a tool result identically across backend transports."""

    return json.dumps(
        {
            "call_id": result.call_id,
            "success": result.success,
            "payload": dict(result.payload),
            "evidence": list(result.evidence),
        },
        sort_keys=True,
    )
