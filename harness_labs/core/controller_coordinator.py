"""Resident model coordinator connected to kernel commands and read queries."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from harness_labs.core.agent_sessions import (
    AgentSession,
    BackendFailure,
    FinalOutput,
    ModelRequest,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from harness_labs.core.attempts import TaskResult
from harness_labs.core.audit import AuditActor
from harness_labs.core.controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandProvenance,
)
from harness_labs.core.controller_kernel import ControllerKernel
from harness_labs.core.controller_projection import ControllerQueries, project_run_view
from harness_labs.core.controller_scheduler import CapabilityScheduler, SchedulingError
from harness_labs.core.usage import usage_payload


QUERY_TOOL_MAP = {
    "run_get_view": "run.get_view",
    "task_get_result": "task.get_result",
    "artifact_open": "artifact.open",
    "event_query": "event.query",
    "decision_list": "decision.list",
    "acceptance_get_matrix": "acceptance.get_matrix",
    "finding_list": "finding.list",
}
COMMAND_TOOL_MAP = {
    "criterion_propose": "criterion.propose",
    "task_dispatch": "task.dispatch",
    "decision_record": "decision.record",
    "finding_disposition": "finding.disposition",
    "phase_advance_request": "phase.advance_request",
    "retry_request": "retry.request",
    "replan_request": "replan.request",
    "operator_input_request": "operator_input.request",
    "run_complete_request": "run.complete_request",
    "run_block_request": "run.block_request",
}


@dataclass
class CoordinatorLoop:
    """Keep one coordinator session resident while the kernel owns effects."""

    kernel: ControllerKernel
    queries: ControllerQueries
    scheduler: CapabilityScheduler
    session: AgentSession
    max_tool_calls: int | None = None
    actor_id: str = "coordinator-1"
    phase_scope: tuple[str, ...] = ()
    task_override: str | None = None
    initial_context: Mapping[str, Any] = field(default_factory=dict)

    def run(self) -> TaskResult:
        if self.max_tool_calls is not None and self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive or unbounded")
        if self.phase_scope:
            unknown = set(self.phase_scope) - set(self.kernel.contract.phases)
            if unknown:
                raise ValueError(
                    f"coordinator phase scope contains unknown phases: {sorted(unknown)}"
                )
            if self.kernel.snapshot()["phase"] not in self.phase_scope:
                raise ValueError("coordinator phase scope excludes the current phase")
        context = {
            "controller_contract": {
                "goal": (
                    "Advance the objective with evidence while preserving "
                    "authority, bounds, and auditability."
                ),
                "rules": [
                    "Use typed commands for every state change.",
                    "Use query tools to inspect referenced evidence as needed.",
                    "Treat worker claims as untrusted until evidence is recorded.",
                    "Do not finish until run_complete_request is accepted or "
                    "run_block_request is accepted.",
                ],
            },
            "run_view": project_run_view(self.kernel),
            "available_role_profiles": list(self.scheduler.profile_view),
        }
        if self.initial_context:
            context["coordinator_segment"] = copy.deepcopy(
                dict(self.initial_context)
            )
        request = ModelRequest(
            task=self.task_override or self.kernel.contract.objective,
            context=context,
            tools=_tool_specs(),
        )
        session_id = self.session.open(request)
        tool_result: ToolResult | None = None
        tool_calls = 0
        try:
            while self.max_tool_calls is None or tool_calls <= self.max_tool_calls:
                event = self.session.step(session_id, tool_result)
                tool_result = None
                if isinstance(event, ToolCall):
                    tool_calls += 1
                    if (
                        self.max_tool_calls is not None
                        and tool_calls > self.max_tool_calls
                    ):
                        return self._blocked(
                            "coordinator exceeded max_tool_calls",
                            session_id,
                        )
                    tool_result = self._handle_tool(session_id, event)
                    boundary = self._segment_boundary(session_id)
                    if boundary is not None:
                        return boundary
                    continue
                if isinstance(event, BackendFailure):
                    return TaskResult(
                        attempt_id=f"{self.kernel.contract.run_id}/coordinator",
                        status="failed",
                        payload={
                            "error": event.error,
                            "session_id": session_id,
                        },
                    )
                if isinstance(event, FinalOutput):
                    state = self.kernel.snapshot()
                    status = (
                        "succeeded"
                        if state["status"] == "succeeded"
                        else "blocked"
                    )
                    if self.kernel.audit is not None:
                        model = str(getattr(self.session, "model", "unknown"))
                        self.kernel.audit.append(
                            "backend_usage",
                            status="succeeded",
                            payload={
                                "tool_calls": tool_calls,
                                "usage": (
                                    usage_payload(
                                        model=model,
                                        input_tokens=event.usage.input_tokens,
                                        cached_input_tokens=(
                                            event.usage.cached_input_tokens
                                        ),
                                        output_tokens=event.usage.output_tokens,
                                        pricing=getattr(
                                            self.session, "pricing", None
                                        ),
                                    )
                                    if event.usage is not None
                                    else None
                                ),
                            },
                            actor=AuditActor(
                                self.actor_id, "run_coordinator"
                            ),
                            session_id=session_id,
                            backend_id=str(
                                getattr(
                                    self.session,
                                    "backend_id",
                                    type(self.session).__name__,
                                )
                            ),
                        )
                    return TaskResult(
                        attempt_id=f"{self.kernel.contract.run_id}/coordinator",
                        status=status,
                        payload={
                            "text": event.content,
                            "session_id": session_id,
                            "run_status": state["status"],
                            "run_view": project_run_view(self.kernel),
                            "available_role_profiles": list(
                                self.scheduler.profile_view
                            ),
                            "tool_calls": tool_calls,
                        },
                        evidence=event.evidence,
                    )
                return self._blocked("unknown coordinator event", session_id)
            return self._blocked("coordinator loop ended unexpectedly", session_id)
        finally:
            self.session.close(session_id)

    def _handle_tool(self, session_id: str, call: ToolCall) -> ToolResult:
        try:
            if call.name in QUERY_TOOL_MAP:
                payload = self.queries.execute(
                    QUERY_TOOL_MAP[call.name],
                    dict(call.arguments),
                )
                return ToolResult(call.call_id, True, payload)
            command_type = COMMAND_TOOL_MAP.get(call.name)
            if command_type is None:
                raise ValueError(f"unknown coordinator tool: {call.name}")
            if command_type in {
                "phase.advance_request",
                "run.complete_request",
            }:
                required = self.initial_context.get(
                    "required_exit_artifact_kinds", ()
                )
                if not isinstance(required, (list, tuple)):
                    raise ValueError(
                        "segment required exit artifacts must be a list"
                    )
                present = {
                    item["kind"]
                    for item in self.kernel.snapshot()["artifacts"].values()
                }
                missing = [kind for kind in required if kind not in present]
                if missing:
                    raise ValueError(
                        "segment cannot cross its boundary without exit "
                        "artifacts: " + ", ".join(missing)
                    )
            arguments = dict(call.arguments)
            evidence_refs = arguments.pop("evidence_refs", [])
            if (
                not isinstance(evidence_refs, list)
                or not all(isinstance(ref, str) for ref in evidence_refs)
            ):
                raise ValueError("command evidence_refs must be a string list")
            idempotency_key = arguments.pop(
                "idempotency_key",
                f"{self.actor_id}/{session_id}/{call.call_id}",
            )
            if not isinstance(idempotency_key, str) or not idempotency_key:
                raise ValueError("command idempotency_key must be non-empty")
            command = CommandEnvelope(
                command_id=f"{session_id}/{call.call_id}",
                run_id=self.kernel.contract.run_id,
                type=command_type,
                actor=CommandActor(self.actor_id, "run_coordinator"),
                expected_revision=self.kernel.revision,
                idempotency_key=idempotency_key,
                provenance=CommandProvenance(
                    trigger_event=call.call_id,
                    evidence_refs=tuple(evidence_refs),
                ),
                payload=arguments,
            )
            if command_type == "task.dispatch":
                tasks = arguments.get("tasks")
                if isinstance(tasks, list) and all(
                    isinstance(task, dict) for task in tasks
                ):
                    self.scheduler.validate_task_profiles(tasks)
            receipt = self.kernel.handle(command)
            response: dict[str, Any] = {"receipt": receipt.as_dict()}
            if receipt.accepted and command_type == "task.dispatch":
                task_ids = tuple(
                    ref.removeprefix("task:")
                    for ref in receipt.effect_refs
                    if ref.startswith("task:")
                )
                ready_task_ids = tuple(
                    task_id
                    for task_id in task_ids
                    if self.kernel.task(task_id)["status"] == "ready"
                )
                max_parallelism = command.payload.get("max_parallelism", 1)
                assert isinstance(max_parallelism, int)
                outcomes = (
                    self.scheduler.dispatch(
                        self.kernel,
                        ready_task_ids,
                        max_parallelism=max_parallelism,
                    )
                    if ready_task_ids
                    else ()
                )
                response["outcomes"] = [
                    {
                        "task_id": outcome.task_id,
                        "profile_id": outcome.profile_id,
                        "backend_id": outcome.backend_id,
                        "status": outcome.result.status,
                    }
                    for outcome in outcomes
                ]
            response["run_view"] = project_run_view(self.kernel)
            response["available_role_profiles"] = list(
                self.scheduler.profile_view
            )
            return ToolResult(
                call.call_id,
                receipt.accepted,
                response,
                receipt.effect_refs,
            )
        except (ValueError, SchedulingError) as exc:
            return ToolResult(
                call.call_id,
                False,
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "run_view": project_run_view(self.kernel),
                    "available_role_profiles": list(
                        self.scheduler.profile_view
                    ),
                },
            )

    def _blocked(self, error: str, session_id: str) -> TaskResult:
        return TaskResult(
            attempt_id=f"{self.kernel.contract.run_id}/coordinator",
            status="blocked",
            payload={
                "error": error,
                "session_id": session_id,
                "run_view": project_run_view(self.kernel),
                "available_role_profiles": list(self.scheduler.profile_view),
            },
        )

    def _segment_boundary(self, session_id: str) -> TaskResult | None:
        if not self.phase_scope:
            return None
        state = self.kernel.snapshot()
        if state["status"] == "running" and state["phase"] in self.phase_scope:
            return None
        return TaskResult(
            attempt_id=f"{self.kernel.contract.run_id}/{self.actor_id}",
            status=(
                "succeeded"
                if state["status"] in {"running", "succeeded"}
                else "blocked"
            ),
            payload={
                "segment_boundary": True,
                "session_id": session_id,
                "phase_scope": list(self.phase_scope),
                "ending_phase": state["phase"],
                "run_status": state["status"],
                "run_view": project_run_view(self.kernel),
            },
        )


def _tool_specs() -> tuple[ToolSpec, ...]:
    common_command_properties = {
        "idempotency_key": {"type": "string", "minLength": 1},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Evidence references backing this command. A rejected "
                "task.dispatch is itself citable once rejected: use "
                "command:<command_id> from that dispatch's receipt."
            ),
        },
    }
    specs = [
        ToolSpec(
            "run_get_view",
            "Return the current authoritative run projection.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolSpec(
            "task_get_result",
            "Open one task's structured result.",
            {
                "type": "object",
                "required": ["task_id"],
                "properties": {"task_id": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            "artifact_open",
            "Open one referenced evidence artifact.",
            {
                "type": "object",
                "required": ["ref"],
                "properties": {"ref": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            "event_query",
            "Query normalized controller events by optional type or task.",
            {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "task_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            "decision_list",
            "List recorded semantic decisions and their evidence.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolSpec(
            "acceptance_get_matrix",
            "List acceptance criteria, satisfiers, and evidence.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolSpec(
            "finding_list",
            "List findings and their current dispositions.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]
    command_specs = {
        "criterion_propose": (
            "Propose one acceptance criterion with explicit source.",
            {
                "id": {"type": "string", "minLength": 1},
                "statement": {"type": "string", "minLength": 1},
                "source": {
                    "type": "string",
                    "enum": ["operator", "repository", "coordinator", "plan"],
                },
                "rationale": {"type": "string"},
                "minimum_satisfiers": {"type": "integer", "minimum": 1},
            },
            ["id", "statement", "source"],
        ),
        "task_dispatch": (
            "Dispatch one independent bounded batch. Repeated roles are allowed.",
            {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "id",
                            "role",
                            "objective",
                            "details_schema",
                            "required_capabilities",
                            "acceptance_criteria",
                            "dependencies",
                        ],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "role": {"type": "string", "minLength": 1},
                            "objective": {"type": "string", "minLength": 1},
                            "context": {"type": "string"},
                            "details_schema": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "required_capabilities": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "parent_task_id": {
                                "type": ["string", "null"],
                            },
                            "supersedes_task_id": {
                                "type": ["string", "null"],
                                "description": (
                                    "Failed task replaced by this changed-method retry."
                                ),
                            },
                            "optional": {"type": "boolean"},
                            "may_delegate": {"type": "boolean"},
                        },
                    },
                },
                "max_parallelism": {"type": "integer", "minimum": 1},
            },
            ["tasks"],
        ),
        "decision_record": (
            "Record a material choice, alternatives, rationale, and evidence.",
            {
                "id": {"type": "string", "minLength": 1},
                "question": {"type": "string", "minLength": 1},
                "choice": {"type": "string", "minLength": 1},
                "alternatives": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "rationale": {"type": "string", "minLength": 1},
            },
            ["id", "question", "choice", "alternatives", "rationale"],
        ),
        "finding_disposition": (
            "Disposition one finding with rationale and resolution evidence.",
            {
                "finding_id": {"type": "string", "minLength": 1},
                "disposition": {
                    "type": "string",
                    "enum": [
                        "resolved",
                        "rejected",
                        "duplicate",
                        "deferred",
                        "accepted",
                    ],
                },
                "rationale": {"type": "string", "minLength": 1},
                "resolution_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            ["finding_id", "disposition", "rationale", "resolution_refs"],
        ),
        "phase_advance_request": (
            "Request the next phase declared by the run contract.",
            {"target": {"type": "string", "minLength": 1}},
            ["target"],
        ),
        "retry_request": (
            "Record a changed-method retry request.",
            {"reason": {"type": "string", "minLength": 1}},
            ["reason"],
        ),
        "replan_request": (
            "Record a semantic replanning request.",
            {"reason": {"type": "string", "minLength": 1}},
            ["reason"],
        ),
        "operator_input_request": (
            "Request operator judgment, context, budget, or authority.",
            {
                "id": {"type": "string", "minLength": 1},
                "question": {"type": "string", "minLength": 1},
            },
            ["id", "question"],
        ),
        "run_complete_request": (
            "Request completion. The kernel independently evaluates every gate.",
            {},
            [],
        ),
        "run_block_request": (
            "Request a durable blocked outcome with an exact reason.",
            {"reason": {"type": "string", "minLength": 1}},
            ["reason"],
        ),
    }
    for name, (description, properties, required) in command_specs.items():
        specs.append(
            ToolSpec(
                name,
                description,
                {
                    "type": "object",
                    "properties": {
                        **common_command_properties,
                        **properties,
                    },
                    "required": required,
                    "additionalProperties": False,
                },
            )
        )
    return tuple(specs)
