"""Provider-neutral resident agent sessions and controller-owned tool loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from time import monotonic_ns
from typing import Any, Mapping, Protocol, TypeAlias

from .attempts import TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
from .composition import (
    ChildBatchRequest,
    ChildBatchResult,
    ChildDispatcher,
    ChildRequest,
    ChildRequestDenied,
)
from .text_executor import InMemoryReferenceStore


TOOL_UNAVAILABLE_REFUSAL = "sorry, I cannot do that, Dave."


@dataclass(frozen=True)
class BackendCapabilities:
    """Features of one parent-model transport."""

    persistent_sessions: bool
    native_tool_calls: bool
    resumable_sessions: bool
    cached_input_reporting: bool
    structured_output: bool
    experimental_tool_transport: bool = False


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
    max_parallel_children: int = 1
    max_batch_children: int | None = None
    require_all_child_roles: bool = False
    keep_child_alive: bool = False
    audit: AuditJournal | None = None
    _tool_name: str = field(default="spawn_child", init=False, repr=False)
    _batch_tool_name: str = field(
        default="spawn_children", init=False, repr=False
    )
    _message_tool_name: str = field(
        default="send_child_message", init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if self.max_parallel_children < 1:
            raise ValueError("max_parallel_children must be positive")
        if self.max_batch_children is not None and self.max_batch_children < 1:
            raise ValueError("max_batch_children must be positive")

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        started_ns = monotonic_ns()
        actor = AuditActor(attempt.attempt_id, "parent")
        if self.audit is not None:
            attempt_artifact = self.audit.write_artifact(
                "parent-attempt",
                {
                    "attempt_id": attempt.attempt_id,
                    "parent_attempt_id": attempt.parent_attempt_id,
                    "task_ref": attempt.task_ref,
                    "context_ref": attempt.context_ref,
                    "grant_ref": attempt.grant_ref,
                },
            )
            self.audit.append(
                "attempt_started",
                status="started",
                payload={"keep_child_alive": self.keep_child_alive},
                actor=actor,
                attempt_id=attempt.attempt_id,
                artifacts=(attempt_artifact,),
            )
            self.audit.merge_checkpoint(
                updates={"active_attempt": attempt.attempt_id}
            )
        try:
            result = self._execute(attempt)
        except Exception as exc:
            if self.audit is not None:
                self.audit.append(
                    "attempt_crashed",
                    status="failed",
                    payload={"error_type": type(exc).__name__, "error": str(exc)},
                    actor=actor,
                    attempt_id=attempt.attempt_id,
                    duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
                )
            raise
        if self.audit is not None:
            result_artifact = self.audit.write_artifact(
                "parent-task-result",
                _task_result_dict(result),
            )
            self.audit.append(
                "attempt_completed",
                status=result.status,
                payload={"evidence": list(result.evidence)},
                actor=actor,
                attempt_id=attempt.attempt_id,
                duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
                artifacts=(result_artifact,),
            )
            self.audit.merge_checkpoint(
                status=result.status,
                updates={"active_attempt": None},
            )
        return result

    def _execute(self, attempt: TaskAttempt) -> TaskResult:
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

        capabilities = grant.get("capabilities", ())
        tools: tuple[ToolSpec, ...] = ()
        if "spawn_child" in capabilities:
            tools += (ToolSpec(
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
            ),)
        if "spawn_children" in capabilities:
            tools += (ToolSpec(
                name=self._batch_tool_name,
                description=(
                    "Run independent controller-authorized child tasks concurrently "
                    "and return all results in request order."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["requests"],
                    "properties": {
                        "requests": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": (
                                self.max_batch_children
                                if self.max_batch_children is not None
                                else self.max_parallel_children
                            ),
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["role", "objective"],
                                "properties": {
                                    "role": {
                                        "type": "string",
                                        "enum": list(allowed_roles),
                                    },
                                    "objective": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                },
                            },
                        }
                    },
                },
            ),)
        if self.keep_child_alive:
            if "send_child_message" not in grant.get("capabilities", ()):
                return TaskResult(
                    attempt_id=attempt.attempt_id,
                    status="blocked",
                    payload={"error": "send_child_message capability is required"},
                )
            tools += (
                ToolSpec(
                    name=self._message_tool_name,
                    description="Send one follow-up message to the retained child.",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["child_attempt_id", "message"],
                        "properties": {
                            "child_attempt_id": {"type": "string", "minLength": 1},
                            "message": {"type": "string", "minLength": 1},
                        },
                    },
                ),
            )

        request = ModelRequest(task=task, context=context, tools=tools)
        if self.audit is not None:
            request_artifact = self.audit.write_artifact(
                "parent-model-request",
                _model_request_dict(request),
            )
            authorization_artifact = self.audit.write_artifact(
                "parent-authorization",
                {
                    "grant_ref": attempt.grant_ref,
                    "grant": dict(grant),
                    "allowed_roles": list(allowed_roles),
                    "tool_names": [tool.name for tool in tools],
                },
            )
            self.audit.append(
                "authorization_bound",
                status="succeeded",
                payload={
                    "allowed_roles": list(allowed_roles),
                    "tool_names": [tool.name for tool in tools],
                },
                actor=AuditActor(attempt.attempt_id, "parent"),
                attempt_id=attempt.attempt_id,
                artifacts=(request_artifact, authorization_artifact),
            )
        session_id: str | None = None
        child_result: TaskResult | None = None
        child_attempt_id: str | None = None
        child_turns: list[TaskResult] = []
        child_results: list[TaskResult] = []
        tool_calls = 0
        try:
            session_id = self.session.open(request)
            if self.audit is not None:
                sessions = self.audit.checkpoint_ids("active_sessions")
                sessions.add(session_id)
                self.audit.append(
                    "session_opened",
                    status="started",
                    payload={"persistent": self.session.capabilities.persistent_sessions},
                    actor=AuditActor(attempt.attempt_id, "parent"),
                    attempt_id=attempt.attempt_id,
                    session_id=session_id,
                )
                self.audit.merge_checkpoint(
                    updates={"active_sessions": sorted(sessions)}
                )
            event = self.session.step(session_id)
            self._record_model_event(attempt, session_id, event)
            while isinstance(event, ToolCall):
                tool_calls += 1
                if tool_calls > self.max_tool_calls:
                    return self._failed(attempt, "maximum parent tool calls exceeded")
                tool_result, returned_children = self._run_tool(
                    attempt,
                    event,
                    allowed_roles,
                    child_attempt_id,
                )
                if returned_children:
                    child_results.extend(returned_children)
                    child_result = returned_children[-1]
                if len(returned_children) == 1 and event.name == self._tool_name:
                    child_attempt_id = child_result.attempt_id
                    child_turns.append(child_result)
                elif (
                    len(returned_children) == 1
                    and event.name == self._message_tool_name
                ):
                    child_turns.append(returned_children[0])
                if self.audit is not None:
                    result_artifact = self.audit.write_artifact(
                        f"tool-result-{event.call_id}",
                        _tool_result_dict(tool_result),
                    )
                    self.audit.append(
                        "tool_result",
                        status="succeeded" if tool_result.success else "failed",
                        payload={
                            "call_id": tool_result.call_id,
                            "tool_name": event.name,
                        },
                        actor=AuditActor(attempt.attempt_id, "parent"),
                        attempt_id=attempt.attempt_id,
                        session_id=session_id,
                        artifacts=(result_artifact,),
                    )
                event = self.session.step(session_id, tool_result)
                self._record_model_event(attempt, session_id, event)

            if isinstance(event, BackendFailure):
                return self._failed(attempt, f"backend failed: {event.error}")
            if not isinstance(event, FinalOutput):
                return self._failed(attempt, "backend returned an unknown event")

            answer = event.content.strip()
            if not answer:
                return self._failed(attempt, "backend returned no answer")
            if not child_results:
                return self._failed(attempt, "parent backend did not call child")
            if any(result.status != "succeeded" for result in child_results):
                return self._failed(attempt, "one or more authorized children failed")
            if self.keep_child_alive and len(child_turns) != 2:
                return self._failed(
                    attempt,
                    "parent backend did not complete the retained-child follow-up",
                )
            return self._succeeded(
                attempt,
                answer,
                session_id,
                event,
                child_result,
                tuple(child_turns),
                tuple(child_results),
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
            try:
                if child_attempt_id is not None and self.keep_child_alive:
                    self.dispatcher.terminate_child(attempt, child_attempt_id)
            finally:
                if session_id is not None:
                    self.session.close(session_id)
                    if self.audit is not None:
                        sessions = self.audit.checkpoint_ids("active_sessions")
                        sessions.discard(session_id)
                        self.audit.append(
                            "session_closed",
                            status="succeeded",
                            payload={"termination_scope": "transport_session"},
                            actor=AuditActor(attempt.attempt_id, "parent"),
                            attempt_id=attempt.attempt_id,
                            session_id=session_id,
                        )
                        self.audit.merge_checkpoint(
                            updates={"active_sessions": sorted(sessions)}
                        )

    def _record_model_event(
        self,
        attempt: TaskAttempt,
        session_id: str,
        event: ModelEvent,
    ) -> None:
        if self.audit is None:
            return
        artifact = self.audit.write_artifact(
            f"model-event-{type(event).__name__}",
            _model_event_dict(event),
        )
        status = "failed" if isinstance(event, BackendFailure) else "succeeded"
        self.audit.append(
            "model_event",
            status=status,
            payload={"event_kind": type(event).__name__},
            actor=AuditActor(attempt.attempt_id, "parent"),
            attempt_id=attempt.attempt_id,
            session_id=session_id,
            artifacts=(artifact,),
        )

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
        if not isinstance(grant, Mapping) or not {
            "spawn_child",
            "spawn_children",
        }.intersection(grant.get("capabilities", ())):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "a child-spawn capability is required"},
            )
        return task, context, grant

    def _run_tool(
        self,
        parent: TaskAttempt,
        call: ToolCall,
        allowed_roles: tuple[str, ...],
        child_attempt_id: str | None,
    ) -> tuple[ToolResult, tuple[TaskResult, ...]]:
        if call.name == self._message_tool_name and self.keep_child_alive:
            requested_id = call.arguments.get("child_attempt_id")
            message = call.arguments.get("message")
            if (
                requested_id != child_attempt_id
                or not isinstance(message, str)
                or not message.strip()
            ):
                return (
                    ToolResult(
                        call_id=call.call_id,
                        success=False,
                        payload={"error": "invalid retained-child message"},
                    ),
                    (),
                )
            child = self.dispatcher.send_child_message(
                parent,
                requested_id,
                message,
            )
            tool_result, result = self._child_tool_result(call.call_id, child)
            return tool_result, (result,)
        if call.name == self._batch_tool_name and child_attempt_id is None:
            if self.keep_child_alive:
                return (
                    ToolResult(
                        call_id=call.call_id,
                        success=False,
                        payload={
                            "error": (
                                "retained child sessions require individual dispatch"
                            )
                        },
                    ),
                    (),
                )
            raw_requests = call.arguments.get("requests")
            if (
                not isinstance(raw_requests, list)
                or not 1
                <= len(raw_requests)
                <= (
                    self.max_batch_children
                    if self.max_batch_children is not None
                    else self.max_parallel_children
                )
            ):
                return (
                    ToolResult(
                        call_id=call.call_id,
                        success=False,
                        payload={"error": "invalid child batch request"},
                    ),
                    (),
                )
            requests: list[ChildRequest] = []
            for raw_request in raw_requests:
                if not isinstance(raw_request, Mapping):
                    return (
                        ToolResult(
                            call_id=call.call_id,
                            success=False,
                            payload={"error": "invalid child batch request"},
                        ),
                        (),
                    )
                role = raw_request.get("role")
                objective = raw_request.get("objective")
                if (
                    role not in allowed_roles
                    or not isinstance(objective, str)
                    or not objective.strip()
                ):
                    return (
                        ToolResult(
                            call_id=call.call_id,
                            success=False,
                            payload={"error": "invalid child batch request"},
                        ),
                        (),
                    )
                requests.append(ChildRequest(role=role, objective=objective))
            if self.require_all_child_roles and {
                request.role for request in requests
            } != set(allowed_roles):
                return (
                    ToolResult(
                        call_id=call.call_id,
                        success=False,
                        payload={
                            "error": (
                                "child batch must contain every authorized role "
                                "exactly once"
                            )
                        },
                    ),
                    (),
                )
            batch = self.dispatcher.run_children(
                parent,
                ChildBatchRequest(
                    requests=tuple(requests),
                    max_parallelism=self.max_parallel_children,
                ),
            )
            return self._batch_tool_result(call.call_id, batch)
        if call.name != self._tool_name or child_attempt_id is not None:
            return (
                ToolResult(
                    call_id=call.call_id,
                    success=False,
                    payload={"error": f"unknown tool: {call.name}"},
                ),
                (),
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
                (),
            )
        child = self.dispatcher.start_child(
            parent,
            ChildRequest(role=role, objective=objective),
            keep_alive=self.keep_child_alive,
        )
        tool_result, result = self._child_tool_result(call.call_id, child)
        return tool_result, (result,)

    @staticmethod
    def _child_tool_result(
        call_id: str,
        child: TaskResult,
    ) -> tuple[ToolResult, TaskResult]:
        return (
            ToolResult(
                call_id=call_id,
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
    def _batch_tool_result(
        call_id: str,
        batch: ChildBatchResult,
    ) -> tuple[ToolResult, tuple[TaskResult, ...]]:
        evidence = tuple(
            item for result in batch.results for item in result.evidence
        )
        return (
            ToolResult(
                call_id=call_id,
                success=batch.succeeded,
                payload={
                    "batch_id": batch.batch_id,
                    "max_parallelism": batch.max_parallelism,
                    "results": [
                        {
                            "attempt_id": result.attempt_id,
                            "status": result.status,
                            "payload": dict(result.payload),
                        }
                        for result in batch.results
                    ],
                },
                evidence=evidence,
            ),
            batch.results,
        )

    @staticmethod
    def _succeeded(
        attempt: TaskAttempt,
        answer: str,
        session_id: str,
        event: FinalOutput,
        child_result: TaskResult | None,
        child_turns: tuple[TaskResult, ...],
        child_results: tuple[TaskResult, ...],
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
            payload["child_results"] = [
                {
                    "attempt_id": result.attempt_id,
                    "status": result.status,
                    "payload": dict(result.payload),
                    "evidence": list(result.evidence),
                }
                for result in child_results
            ]
            payload["child_turns"] = [
                {
                    "status": turn.status,
                    "payload": dict(turn.payload),
                    "evidence": list(turn.evidence),
                }
                for turn in child_turns
            ]
            evidence = (
                *(item for turn in child_turns for item in turn.evidence),
                *evidence,
            )
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


def _model_request_dict(request: ModelRequest) -> dict[str, Any]:
    return {
        "task": request.task,
        "context": dict(request.context),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.input_schema),
            }
            for tool in request.tools
        ],
        "unavailable_tool_response": request.unavailable_tool_response,
    }


def _tool_result_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "call_id": result.call_id,
        "success": result.success,
        "payload": dict(result.payload),
        "evidence": list(result.evidence),
    }


def _model_event_dict(event: ModelEvent) -> dict[str, Any]:
    if isinstance(event, ToolCall):
        return {
            "kind": "tool_call",
            "call_id": event.call_id,
            "name": event.name,
            "arguments": dict(event.arguments),
        }
    if isinstance(event, FinalOutput):
        return {
            "kind": "final_output",
            "content": event.content,
            "usage": (
                {
                    "input_tokens": event.usage.input_tokens,
                    "cached_input_tokens": event.usage.cached_input_tokens,
                    "output_tokens": event.usage.output_tokens,
                }
                if event.usage is not None
                else None
            ),
            "evidence": list(event.evidence),
        }
    return {"kind": "backend_failure", "error": event.error}


def _task_result_dict(result: TaskResult) -> dict[str, Any]:
    return {
        "attempt_id": result.attempt_id,
        "status": result.status,
        "payload": dict(result.payload),
        "evidence": list(result.evidence),
    }
