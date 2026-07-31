"""The minimal Task Attempt execution boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol


Status = Literal["succeeded", "blocked", "failed", "cancelled"]
VALID_STATUSES = frozenset({"succeeded", "blocked", "failed", "cancelled"})


class InvalidAttempt(ValueError):
    """Raised when a Task Attempt is incomplete."""


class InvalidResult(ValueError):
    """Raised when an executor returns an invalid Task Result."""


def _require_reference(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidAttempt(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class TaskAttempt:
    """One immutable request to execute a task."""

    attempt_id: str
    task_ref: str
    context_ref: str
    grant_ref: str
    parent_attempt_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("attempt_id", "task_ref", "context_ref", "grant_ref"):
            _require_reference(name, getattr(self, name))
        if self.parent_attempt_id is not None:
            _require_reference("parent_attempt_id", self.parent_attempt_id)


@dataclass(frozen=True)
class TaskResult:
    """The typed envelope returned by an executor."""

    attempt_id: str
    status: Status
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


class Executor(Protocol):
    """Anything capable of executing one Task Attempt."""

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        """Execute the attempt and return its result."""


class AttemptRunner:
    """Invoke one executor and validate the result envelope."""

    def run(self, attempt: TaskAttempt, executor: Executor) -> TaskResult:
        result = executor.execute(attempt)
        if not isinstance(result, TaskResult):
            raise InvalidResult("executor must return TaskResult")
        if result.attempt_id != attempt.attempt_id:
            raise InvalidResult("result attempt_id does not match the attempt")
        if result.status not in VALID_STATUSES:
            raise InvalidResult(f"unsupported result status: {result.status!r}")
        return result
