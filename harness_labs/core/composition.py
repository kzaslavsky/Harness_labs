"""Bounded, policy-controlled child Task Attempt execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import threading
from time import monotonic_ns
from typing import Literal, Mapping, Protocol

from harness_labs.core.attempts import AttemptRunner, Executor, TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal


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
    context: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("child role must be non-empty")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("child objective must be non-empty")
        if not isinstance(self.context, str):
            raise ValueError("child context must be a string")


@dataclass(frozen=True)
class ChildBatchRequest:
    """One bounded, controller-scheduled group of independent child requests."""

    requests: tuple[ChildRequest, ...]
    max_parallelism: int
    failure_policy: Literal["collect_all"] = "collect_all"

    def __post_init__(self) -> None:
        if not self.requests:
            raise ValueError("child batch must contain at least one request")
        if not all(isinstance(request, ChildRequest) for request in self.requests):
            raise ValueError("child batch requests must be ChildRequest values")
        if self.max_parallelism < 1:
            raise ValueError("child batch max_parallelism must be positive")
        if self.failure_policy != "collect_all":
            raise ValueError("unsupported child batch failure policy")


@dataclass(frozen=True)
class ChildBatchResult:
    """Terminal child results in the same order as the submitted requests."""

    batch_id: str
    results: tuple[TaskResult, ...]
    max_parallelism: int

    @property
    def succeeded(self) -> bool:
        return all(result.status == "succeeded" for result in self.results)


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
    context_sha256: str
    status: str
    evidence: tuple[str, ...] = ()


class ChildDispatcher:
    """Authorize and run bounded children through a controller-owned scheduler."""

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
        self._mutex = threading.RLock()
        self._batch_counts: dict[str, int] = {}
        self._inflight_roles: set[str] = set()
        self._active_children: dict[
            str, tuple[TaskAttempt, ChildAuthorization, ConversationalExecutor]
        ] = {}

    @property
    def allowed_roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._authorizations))

    @property
    def events(self) -> tuple[ChildEvent, ...]:
        with self._mutex:
            return tuple(self._events)

    def run_child(self, parent: TaskAttempt, request: ChildRequest) -> TaskResult:
        return self.start_child(parent, request, keep_alive=False)

    def run_children(
        self,
        parent: TaskAttempt,
        batch: ChildBatchRequest,
        *,
        keep_alive: bool = False,
    ) -> ChildBatchResult:
        """Validate the whole batch, then execute it with bounded concurrency."""

        if not isinstance(batch, ChildBatchRequest):
            raise ChildRequestDenied("child batch has an invalid type")
        batch_id, prepared = self._prepare_batch(parent, batch)
        self._append_batch_event(
            "child_batch_started",
            parent,
            batch_id,
            prepared,
            batch.max_parallelism,
            "started",
        )

        results: list[TaskResult | None] = [None] * len(prepared)
        worker_count = min(batch.max_parallelism, len(prepared))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="child-attempt",
        ) as pool:
            futures = {
                pool.submit(
                    self._execute_prepared,
                    parent,
                    child,
                    request,
                    authorization,
                    keep_alive,
                    True,
                ): index
                for index, (child, request, authorization) in enumerate(prepared)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

        terminal = tuple(
            result for result in results if isinstance(result, TaskResult)
        )
        if len(terminal) != len(prepared):  # defensive: futures must all settle
            raise RuntimeError("child batch did not produce one result per request")
        self._append_batch_event(
            "child_batch_completed",
            parent,
            batch_id,
            prepared,
            batch.max_parallelism,
            "succeeded" if all(r.status == "succeeded" for r in terminal) else "failed",
        )
        return ChildBatchResult(
            batch_id=batch_id,
            results=terminal,
            max_parallelism=batch.max_parallelism,
        )

    def start_child(
        self,
        parent: TaskAttempt,
        request: ChildRequest,
        *,
        keep_alive: bool,
    ) -> TaskResult:
        child, authorization = self._prepare_child(parent, request)
        return self._execute_prepared(
            parent,
            child,
            request,
            authorization,
            keep_alive,
            False,
        )

    def _prepare_batch(
        self,
        parent: TaskAttempt,
        batch: ChildBatchRequest,
    ) -> tuple[
        str,
        tuple[tuple[TaskAttempt, ChildRequest, ChildAuthorization], ...],
    ]:
        with self._mutex:
            self._validate_parent(parent)
            if len(batch.requests) > self._remaining_children(parent):
                raise ChildRequestDenied("maximum children per attempt exceeded")
            roles = [request.role for request in batch.requests]
            if len(set(roles)) != len(roles):
                raise ChildRequestDenied(
                    "parallel batch roles must be unique so executors are not shared"
                )
            overlapping = sorted(set(roles).intersection(self._inflight_roles))
            if overlapping:
                raise ChildRequestDenied(
                    f"child role already has an in-flight executor: {overlapping[0]}"
                )
            for request in batch.requests:
                self._validate_request(parent, request)
            prepared = tuple(
                self._reserve_child(parent, request) for request in batch.requests
            )
            batch_number = self._batch_counts.get(parent.attempt_id, 0) + 1
            self._batch_counts[parent.attempt_id] = batch_number
            self._inflight_roles.update(roles)
            return f"{parent.attempt_id}/batch-{batch_number}", prepared

    def _prepare_child(
        self,
        parent: TaskAttempt,
        request: ChildRequest,
    ) -> tuple[TaskAttempt, ChildAuthorization]:
        with self._mutex:
            self._validate_parent(parent)
            self._validate_request(parent, request)
            if request.role in self._inflight_roles:
                raise ChildRequestDenied(
                    f"child role already has an in-flight executor: {request.role}"
                )
            child, _, authorization = self._reserve_child(parent, request)
            self._inflight_roles.add(request.role)
            return child, authorization

    def _validate_parent(self, parent: TaskAttempt) -> None:
        known_parent = self._attempts.get(parent.attempt_id)
        if known_parent != parent:
            raise ChildRequestDenied("parent attempt is not registered")

    def _validate_request(
        self,
        parent: TaskAttempt,
        request: ChildRequest,
    ) -> ChildAuthorization:
        if not isinstance(request, ChildRequest):
            raise ChildRequestDenied("child request has an invalid type")
        authorization = self._authorizations.get(request.role)
        if authorization is None:
            raise ChildRequestDenied(f"child role is not authorized: {request.role}")
        child_depth = self._depths[parent.attempt_id] + 1
        if child_depth > self._max_depth:
            raise ChildRequestDenied("maximum child depth exceeded")
        if self._remaining_children(parent) < 1:
            raise ChildRequestDenied("maximum children per attempt exceeded")
        return authorization

    def _remaining_children(self, parent: TaskAttempt) -> int:
        return self._max_children - self._child_counts.get(parent.attempt_id, 0)

    def _reserve_child(
        self,
        parent: TaskAttempt,
        request: ChildRequest,
    ) -> tuple[TaskAttempt, ChildRequest, ChildAuthorization]:
        authorization = self._authorizations[request.role]
        child_depth = self._depths[parent.attempt_id] + 1
        child_number = self._child_counts.get(parent.attempt_id, 0) + 1
        child_id = f"{parent.attempt_id}/child-{child_number}"
        child = TaskAttempt(
            attempt_id=child_id,
            task_ref=authorization.task_ref,
            context_ref=authorization.context_ref,
            grant_ref=authorization.grant_ref,
            parent_attempt_id=parent.attempt_id,
            context=request.context,
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
        return child, request, authorization

    def _execute_prepared(
        self,
        parent: TaskAttempt,
        child: TaskAttempt,
        request: ChildRequest,
        authorization: ChildAuthorization,
        keep_alive: bool,
        capture_exceptions: bool,
    ) -> TaskResult:
        started_ns = monotonic_ns()
        retain_role = False
        try:
            result = self._runner.run(child, authorization.executor)
        except Exception as exc:
            result = TaskResult(
                attempt_id=child.attempt_id,
                status="failed",
                payload={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            try:
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
            finally:
                with self._mutex:
                    self._inflight_roles.discard(request.role)
            if capture_exceptions:
                return result
            raise
        try:
            conversational = _as_conversational(authorization.executor)
            if keep_alive and result.status == "succeeded":
                if conversational is None:
                    raise ChildRequestDenied(
                        f"child backend does not support retained sessions: "
                        f"{authorization.backend_id}"
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
            if keep_alive and result.status == "succeeded":
                assert conversational is not None
                with self._mutex:
                    self._active_children[child.attempt_id] = (
                        child,
                        authorization,
                        conversational,
                    )
                retain_role = True
            return result
        finally:
            if not retain_role:
                if (
                    keep_alive
                    and result.status == "succeeded"
                    and conversational is not None
                ):
                    conversational.close()
                with self._mutex:
                    self._inflight_roles.discard(request.role)

    def _append_batch_event(
        self,
        event_type: str,
        parent: TaskAttempt,
        batch_id: str,
        prepared: tuple[
            tuple[TaskAttempt, ChildRequest, ChildAuthorization], ...
        ],
        max_parallelism: int,
        status: str,
    ) -> None:
        if self._audit is None:
            return
        child_ids = [child.attempt_id for child, _, _ in prepared]
        artifact = self._audit.write_artifact(
            event_type,
            {
                "batch_id": batch_id,
                "parent_attempt_id": parent.attempt_id,
                "child_attempt_ids": child_ids,
                "request_order": [
                    {
                        "role": request.role,
                        "objective": request.objective,
                        "context": request.context,
                        "context_sha256": _sha256_text(request.context),
                    }
                    for _, request, _ in prepared
                ],
                "max_parallelism": max_parallelism,
            },
        )
        self._audit.append(
            event_type,
            status=status,
            payload={
                "batch_id": batch_id,
                "child_attempt_ids": child_ids,
                "max_parallelism": max_parallelism,
            },
            actor=AuditActor(parent.attempt_id, "parent"),
            attempt_id=parent.attempt_id,
            artifacts=(artifact,),
        )

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
        with self._mutex:
            self._active_children.pop(child_attempt_id, None)
            self._inflight_roles.discard(authorization.role)
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
        with self._mutex:
            event = ChildEvent(
                sequence=len(self._events),
                event_type=event_type,
                parent_attempt_id=parent.attempt_id,
                child_attempt_id=child.attempt_id,
                role=role,
                backend_id=backend_id,
                capabilities=tuple(sorted(capabilities)),
                objective=objective,
                context_sha256=_sha256_text(child.context),
                status=status,
                evidence=evidence,
            )
            self._events.append(event)
            audit = self._audit
            if audit is not None:
                artifact = audit.write_artifact(
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
                        "context": child.context,
                        "context_sha256": event.context_sha256,
                        "status": event.status,
                        "evidence": list(event.evidence),
                    },
                )
                audit.append(
                    event.event_type,
                    status=event.status,
                    payload={
                        "role": event.role,
                        "capabilities": list(event.capabilities),
                        "objective": event.objective,
                        "context_sha256": event.context_sha256,
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
                checkpoint = audit.checkpoint_ids("active_children")
                if event.event_type == "child_dispatched":
                    checkpoint.add(event.child_attempt_id)
                elif event.event_type in {"child_completed", "child_terminated"}:
                    checkpoint.discard(event.child_attempt_id)
                audit.merge_checkpoint(
                    updates={"active_children": sorted(checkpoint)}
                )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
