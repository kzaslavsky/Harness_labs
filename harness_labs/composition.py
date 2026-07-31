"""Bounded, policy-controlled child Task Attempt execution."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic_ns
from typing import Mapping, Protocol

from .attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal


class ChildRequestDenied(RuntimeError):
    """Raised when a parent requests a child outside controller policy."""


class ConversationalExecutor(Protocol):
    """An executor that can receive messages after its initial result."""

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        """Run the initial child turn."""

    def send(self, attempt: TaskAttempt, message: str) -> TaskResult:
        """Run one follow-up turn in the retained child session."""

    def close(self) -> None:
        """Terminate the retained child session."""


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
        audit: AuditJournal | None = None,
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
        self._audit = audit
        self._active_children: dict[
            str, tuple[TaskAttempt, ChildAuthorization, ConversationalExecutor]
        ] = {}

    @property
    def allowed_roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._authorizations))

    @property
    def events(self) -> tuple[ChildEvent, ...]:
        return tuple(self._events)

    def run_child(self, parent: TaskAttempt, request: ChildRequest) -> TaskResult:
        return self.start_child(parent, request, keep_alive=False)

    def start_child(
        self,
        parent: TaskAttempt,
        request: ChildRequest,
        *,
        keep_alive: bool,
    ) -> TaskResult:
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

        started_ns = monotonic_ns()
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
                duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
            )
            raise

        conversational = _as_conversational(authorization.executor)
        if keep_alive and result.status == "succeeded":
            if conversational is None:
                raise ChildRequestDenied(
                    f"child backend does not support retained sessions: "
                    f"{authorization.backend_id}"
                )
            self._active_children[child_id] = (
                child,
                authorization,
                conversational,
            )
        elif conversational is not None:
            conversational.close()
        self._append_event(
            "child_responded" if keep_alive else "child_completed",
            parent,
            child,
            request.role,
            authorization.backend_id,
            authorization.capabilities,
            request.objective,
            result.status,
            result.evidence,
            duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
        )
        return result

    def send_child_message(
        self,
        parent: TaskAttempt,
        child_attempt_id: str,
        message: str,
    ) -> TaskResult:
        active = self._active_children.get(child_attempt_id)
        if active is None:
            raise ChildRequestDenied("child session is not active")
        child, authorization, executor = active
        if child.parent_attempt_id != parent.attempt_id:
            raise ChildRequestDenied("child session belongs to another parent")
        if not isinstance(message, str) or not message.strip():
            raise ChildRequestDenied("child message must be non-empty")
        self._append_event(
            "child_message_sent",
            parent,
            child,
            authorization.role,
            authorization.backend_id,
            authorization.capabilities,
            message,
            "started",
        )
        started_ns = monotonic_ns()
        result = self._runner.run(
            child,
            _FollowupExecutor(executor=executor, message=message),
        )
        self._append_event(
            "child_responded",
            parent,
            child,
            authorization.role,
            authorization.backend_id,
            authorization.capabilities,
            message,
            result.status,
            result.evidence,
            duration_ms=(monotonic_ns() - started_ns) // 1_000_000,
        )
        return result

    def terminate_child(self, parent: TaskAttempt, child_attempt_id: str) -> None:
        active = self._active_children.get(child_attempt_id)
        if active is None:
            return
        child, authorization, executor = active
        if child.parent_attempt_id != parent.attempt_id:
            raise ChildRequestDenied("child session belongs to another parent")
        executor.close()
        self._active_children.pop(child_attempt_id, None)
        self._append_event(
            "child_terminated",
            parent,
            child,
            authorization.role,
            authorization.backend_id,
            authorization.capabilities,
            "controller termination after follow-up",
            "succeeded",
        )

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
        duration_ms: int | None = None,
    ) -> None:
        event = ChildEvent(
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
        self._events.append(event)
        if self._audit is not None:
            artifact = self._audit.write_artifact(
                f"child-event-{event.sequence}",
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "parent_attempt_id": event.parent_attempt_id,
                    "child_attempt_id": event.child_attempt_id,
                    "role": event.role,
                    "backend_id": event.backend_id,
                    "capabilities": list(event.capabilities),
                    "objective": event.objective,
                    "status": event.status,
                    "evidence": list(event.evidence),
                },
            )
            self._audit.append(
                event.event_type,
                status=event.status,
                payload={
                    "role": event.role,
                    "capabilities": list(event.capabilities),
                    "objective": event.objective,
                    "evidence": list(event.evidence),
                },
                attempt_id=event.child_attempt_id,
                parent_attempt_id=event.parent_attempt_id,
                backend_id=event.backend_id,
                duration_ms=duration_ms,
                actor=AuditActor(
                    id=event.child_attempt_id,
                    role=event.role,
                    parent_id=event.parent_attempt_id,
                ),
                artifacts=(artifact,),
            )
            checkpoint = self._audit.checkpoint_ids("active_children")
            if event.event_type == "child_dispatched":
                checkpoint.add(event.child_attempt_id)
            elif event.event_type in {"child_completed", "child_terminated"}:
                checkpoint.discard(event.child_attempt_id)
            self._audit.merge_checkpoint(
                updates={"active_children": sorted(checkpoint)}
            )


@dataclass(frozen=True)
class _FollowupExecutor:
    executor: ConversationalExecutor
    message: str

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        return self.executor.send(attempt, self.message)


def _as_conversational(executor: Executor) -> ConversationalExecutor | None:
    if (
        callable(getattr(executor, "send", None))
        and callable(getattr(executor, "close", None))
    ):
        return executor  # type: ignore[return-value]
    return None
