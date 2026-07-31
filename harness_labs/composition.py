"""Bounded, policy-controlled child Task Attempt execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .attempts import AttemptRunner, Executor, TaskAttempt, TaskResult


class ChildRequestDenied(RuntimeError):
    """Raised when a parent requests a child outside controller policy."""


@dataclass(frozen=True)
class ChildRequest:
    """A parent's authority-free request for a controller-selected child."""

    role: str
    objective: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("child role must be non-empty")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("child objective must be non-empty")


@dataclass(frozen=True)
class ChildAuthorization:
    """Controller-owned mapping from a role to fixed attempt authority."""

    role: str
    task_ref: str
    context_ref: str
    grant_ref: str
    backend_id: str
    capabilities: frozenset[str]
    executor: Executor

    def __post_init__(self) -> None:
        for name in ("role", "task_ref", "context_ref", "grant_ref", "backend_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"child authorization {name} must be non-empty")
        if not isinstance(self.capabilities, frozenset) or not all(
            isinstance(capability, str) and capability
            for capability in self.capabilities
        ):
            raise ValueError(
                "child authorization capabilities must be a frozenset of names"
            )


@dataclass(frozen=True)
class ChildEvent:
    """One append-only parent/child lifecycle event."""

    sequence: int
    event_type: str
    parent_attempt_id: str
    child_attempt_id: str
    role: str
    backend_id: str
    capabilities: tuple[str, ...]
    objective: str
    status: str
    evidence: tuple[str, ...] = ()


class ChildDispatcher:
    """Authorize and synchronously run bounded children through AttemptRunner."""

    def __init__(
        self,
        root_attempt: TaskAttempt,
        authorizations: Mapping[str, ChildAuthorization],
        *,
        runner: AttemptRunner | None = None,
        max_depth: int = 1,
        max_children_per_attempt: int = 1,
    ) -> None:
        if root_attempt.parent_attempt_id is not None:
            raise ValueError("dispatcher root must not have a parent")
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        if max_children_per_attempt < 1:
            raise ValueError("max_children_per_attempt must be positive")
        if any(
            role != authorization.role
            for role, authorization in authorizations.items()
        ):
            raise ValueError("authorization key must match its role")

        self._runner = runner or AttemptRunner()
        self._authorizations = dict(authorizations)
        self._max_depth = max_depth
        self._max_children = max_children_per_attempt
        self._attempts = {root_attempt.attempt_id: root_attempt}
        self._depths = {root_attempt.attempt_id: 0}
        self._child_counts: dict[str, int] = {}
        self._events: list[ChildEvent] = []

    @property
    def allowed_roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._authorizations))

    @property
    def events(self) -> tuple[ChildEvent, ...]:
        return tuple(self._events)

    def run_child(self, parent: TaskAttempt, request: ChildRequest) -> TaskResult:
        known_parent = self._attempts.get(parent.attempt_id)
        if known_parent != parent:
            raise ChildRequestDenied("parent attempt is not registered")
        if not isinstance(request, ChildRequest):
            raise ChildRequestDenied("child request has an invalid type")

        authorization = self._authorizations.get(request.role)
        if authorization is None:
            raise ChildRequestDenied(f"child role is not authorized: {request.role}")

        child_depth = self._depths[parent.attempt_id] + 1
        if child_depth > self._max_depth:
            raise ChildRequestDenied("maximum child depth exceeded")

        child_number = self._child_counts.get(parent.attempt_id, 0) + 1
        if child_number > self._max_children:
            raise ChildRequestDenied("maximum children per attempt exceeded")

        child_id = f"{parent.attempt_id}/child-{child_number}"
        child = TaskAttempt(
            attempt_id=child_id,
            task_ref=authorization.task_ref,
            context_ref=authorization.context_ref,
            grant_ref=authorization.grant_ref,
            parent_attempt_id=parent.attempt_id,
        )
        self._child_counts[parent.attempt_id] = child_number
        self._attempts[child_id] = child
        self._depths[child_id] = child_depth
        self._append_event(
            "child_dispatched",
            parent,
            child,
            request.role,
            authorization.backend_id,
            authorization.capabilities,
            request.objective,
            "started",
        )

        try:
            result = self._runner.run(child, authorization.executor)
        except Exception:
            self._append_event(
                "child_completed",
                parent,
                child,
                request.role,
                authorization.backend_id,
                authorization.capabilities,
                request.objective,
                "failed",
            )
            raise

        self._append_event(
            "child_completed",
            parent,
            child,
            request.role,
            authorization.backend_id,
            authorization.capabilities,
            request.objective,
            result.status,
            result.evidence,
        )
        return result

    def _append_event(
        self,
        event_type: str,
        parent: TaskAttempt,
        child: TaskAttempt,
        role: str,
        backend_id: str,
        capabilities: frozenset[str],
        objective: str,
        status: str,
        evidence: tuple[str, ...] = (),
    ) -> None:
        self._events.append(
            ChildEvent(
                sequence=len(self._events),
                event_type=event_type,
                parent_attempt_id=parent.attempt_id,
                child_attempt_id=child.attempt_id,
                role=role,
                backend_id=backend_id,
                capabilities=tuple(sorted(capabilities)),
                objective=objective,
                status=status,
                evidence=evidence,
            )
        )
