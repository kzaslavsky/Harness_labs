"""Shared deterministic fixtures for hybrid-controller scenario tests."""

from __future__ import annotations

from collections.abc import Callable

from harness_labs.agent_sessions import (
    BackendCapabilities,
    FinalOutput,
    ModelRequest,
    ToolCall,
    ToolResult,
)
from harness_labs.attempts import TaskAttempt, TaskResult


class ScriptedCoordinatorSession:
    capabilities = BackendCapabilities(True, True, True, True, True)

    def __init__(self, calls: list[tuple[str, dict]], *, final: str) -> None:
        self.calls = list(calls)
        self.final = final
        self.request: ModelRequest | None = None
        self.results: list[ToolResult | None] = []
        self.closed = False
        self._index = 0

    def open(self, request: ModelRequest) -> str:
        self.request = request
        return "scripted-coordinator"

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        self.results.append(tool_result)
        if self._index < len(self.calls):
            name, arguments = self.calls[self._index]
            self._index += 1
            return ToolCall(f"call-{self._index}", name, arguments)
        return FinalOutput(self.final, evidence=("fixture:coordinator",))

    def close(self, session_id: str) -> None:
        self.closed = True


class FixtureExecutor:
    def __init__(
        self,
        task: dict,
        result_builder: Callable[[dict, TaskAttempt], TaskResult],
    ) -> None:
        self.task = task
        self.result_builder = result_builder
        self.closed = False

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        return self.result_builder(self.task, attempt)

    def close(self) -> None:
        self.closed = True


def task(
    task_id: str,
    role: str,
    objective: str,
    details_schema: str,
    *,
    capabilities: tuple[str, ...] = (),
    criteria: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    parent_task_id: str | None = None,
    may_delegate: bool = False,
) -> dict:
    value = {
        "id": task_id,
        "role": role,
        "objective": objective,
        "details_schema": details_schema,
        "required_capabilities": list(capabilities),
        "acceptance_criteria": list(criteria),
        "dependencies": list(dependencies),
        "may_delegate": may_delegate,
    }
    if parent_task_id is not None:
        value["parent_task_id"] = parent_task_id
    return value
