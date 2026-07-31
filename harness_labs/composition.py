"""Bounded, policy-controlled child Task Attempt execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from .text_executor import InMemoryReferenceStore


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
    executor: Executor

    def __post_init__(self) -> None:
        for name in ("role", "task_ref", "context_ref", "grant_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"child authorization {name} must be non-empty")


@dataclass(frozen=True)
class ChildEvent:
    """One append-only parent/child lifecycle event."""

    sequence: int
    event_type: str
    parent_attempt_id: str
    child_attempt_id: str
    role: str
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
                request.objective,
                "failed",
            )
            raise

        self._append_event(
            "child_completed",
            parent,
            child,
            request.role,
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
                objective=objective,
                status=status,
                evidence=evidence,
            )
        )


class DelegatingBackend(Protocol):
    """A parent model that can request one child and consume its result."""

    def request_child(
        self,
        task: str,
        context: Mapping[str, Any],
        allowed_roles: tuple[str, ...],
    ) -> ChildRequest:
        """Return a schema-bound child request."""

    def finish(
        self,
        task: str,
        context: Mapping[str, Any],
        child_result: TaskResult,
    ) -> str:
        """Produce the parent answer from the authorized child result."""


@dataclass(frozen=True)
class DelegatingExecutor:
    """Resolve a parent attempt and broker its one child through policy."""

    store: InMemoryReferenceStore
    backend: DelegatingBackend
    dispatcher: ChildDispatcher

    def execute(self, attempt: TaskAttempt) -> TaskResult:
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

        granted_roles = grant.get("child_roles", ())
        if not isinstance(granted_roles, (list, tuple)):
            return self._failed(attempt, "child_roles must be a list or tuple")
        allowed_roles = tuple(
            role
            for role in self.dispatcher.allowed_roles
            if role in granted_roles
        )
        if not allowed_roles:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "no authorized child role is available"},
            )

        try:
            request = self.backend.request_child(task, context, allowed_roles)
        except Exception as exc:
            return self._failed(attempt, f"parent request failed: {exc}")
        if not isinstance(request, ChildRequest):
            return self._failed(attempt, "parent returned an invalid child request")
        if request.role not in allowed_roles:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": f"parent requested ungranted role: {request.role}"},
            )

        try:
            child_result = self.dispatcher.run_child(attempt, request)
        except ChildRequestDenied as exc:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": str(exc)},
            )
        except Exception as exc:
            return self._failed(attempt, f"child execution failed: {exc}")
        if child_result.status != "succeeded":
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status=child_result.status,
                payload={
                    "error": "child did not succeed",
                    "child_result": dict(child_result.payload),
                },
                evidence=child_result.evidence,
            )

        try:
            answer = self.backend.finish(task, context, child_result)
        except Exception as exc:
            return self._failed(
                attempt,
                f"parent completion failed: {exc}",
                child_result.evidence,
            )
        if not isinstance(answer, str) or not answer.strip():
            return self._failed(
                attempt,
                "parent returned no answer",
                child_result.evidence,
            )

        answer = answer.strip()
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={
                "text": answer,
                "child_attempt_id": child_result.attempt_id,
            },
            evidence=(
                *child_result.evidence,
                f"parent-content:sha256:{digest}",
            ),
        )

    @staticmethod
    def _failed(
        attempt: TaskAttempt,
        error: str,
        evidence: tuple[str, ...] = (),
    ) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="failed",
            payload={"error": error},
            evidence=evidence,
        )
