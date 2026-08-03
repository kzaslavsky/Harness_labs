"""Model-backed child execution with controller-declared capabilities."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from .attempts import TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
from .text_executor import InMemoryReferenceStore, TextBackend, TextBackendError


@dataclass
class ModelCapabilityExecutor:
    """Always run a model, while failing safely for unavailable capabilities."""

    store: InMemoryReferenceStore
    backend: TextBackend
    backend_id: str
    capabilities: frozenset[str]
    unavailable_response: str
    keep_alive: bool = False
    audit: AuditJournal | None = field(default=None, repr=False)
    _attempt_id: str | None = field(default=None, init=False, repr=False)
    _last_text: str | None = field(default=None, init=False, repr=False)
    _session_id: str | None = field(default=None, init=False, repr=False)

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        try:
            task = self.store.resolve(attempt.task_ref)
            context = self.store.resolve(attempt.context_ref)
            grant = self.store.resolve(attempt.grant_ref)
        except KeyError as exc:
            return self._failed(attempt, f"unresolved reference: {exc.args[0]}")
        if not isinstance(task, str) or not task.strip():
            return self._failed(attempt, "task must resolve to non-empty text")
        if not isinstance(context, Mapping) or not isinstance(grant, Mapping):
            return self._failed(attempt, "context and grant must be mappings")
        required = grant.get("capabilities", ())
        if not isinstance(required, (list, tuple)) or not all(
            isinstance(capability, str) and capability for capability in required
        ):
            return self._failed(attempt, "grant capabilities must be a list or tuple")
        missing = tuple(sorted(set(required) - self.capabilities))

        model_task = task
        expanded_context = dict(context)
        if attempt.context:
            expanded_context["supplied_context"] = attempt.context
        model_context: Mapping[str, Any] = expanded_context
        expected: str | None = None
        if missing:
            expected = self.unavailable_response
            model_task = (
                "You are executing a child task but the controller has determined "
                f"that your backend lacks these required capabilities: {missing}. "
                "Do not claim to perform the task. Return exactly this sentence and "
                f"nothing else: {expected}"
            )
            model_context = {
                "available_capabilities": sorted(self.capabilities),
                "missing_capabilities": list(missing),
                "supplied_context": attempt.context,
            }
        try:
            raw_text = self.backend.generate(model_task, model_context)
        except TextBackendError as exc:
            return self._failed(attempt, str(exc))
        if not isinstance(raw_text, str):
            return self._failed(attempt, "child model returned non-text output")
        text = raw_text.strip()
        if not text:
            return self._failed(attempt, "child model returned no text")
        if expected is not None and text != expected:
            return self._failed(
                attempt,
                "child model did not return the required capability refusal",
            )

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        evidence = (
            f"model-backend:{self.backend_id}",
            "model-invocation:completed",
            f"content:sha256:{digest}",
        )
        evidence += tuple(f"capability:{name}:unavailable" for name in missing)
        result = TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={
                "text": text,
                "session_id": f"{self.backend_id}:{attempt.attempt_id}",
                "backend_id": self.backend_id,
            },
            evidence=evidence,
        )
        if self.keep_alive:
            self._attempt_id = attempt.attempt_id
            self._last_text = text
            self._session_id = f"{self.backend_id}:{attempt.attempt_id}"
            if self.audit is not None:
                self.audit.append(
                    "child_session_opened",
                    status="started",
                    payload={
                        "emulated_session": True,
                        "available_capabilities": sorted(self.capabilities),
                    },
                    actor=AuditActor(
                        attempt.attempt_id,
                        "model_child",
                        parent_id=attempt.parent_attempt_id,
                    ),
                    attempt_id=attempt.attempt_id,
                    parent_attempt_id=attempt.parent_attempt_id,
                    session_id=self._session_id,
                    backend_id=self.backend_id,
                )
        return result

    def send(self, attempt: TaskAttempt, message: str) -> TaskResult:
        if (
            not self.keep_alive
            or self._attempt_id != attempt.attempt_id
            or self._last_text is None
        ):
            return self._failed(attempt, "child model session is not active")
        task = (
            "Continue the same child task conversation. Explain what capability "
            "or capability limitation enabled you to give your prior response. "
            "Answer the operator's question directly in one concise sentence.\n\n"
            f"Operator message: {message}\n"
            f"Prior response: {self._last_text}"
        )
        context = {
            "backend_id": self.backend_id,
            "available_capabilities": sorted(self.capabilities),
        }
        try:
            raw_text = self.backend.generate(task, context)
        except TextBackendError as exc:
            return self._failed(attempt, str(exc))
        if not isinstance(raw_text, str) or not raw_text.strip():
            return self._failed(attempt, "child model returned no follow-up text")
        text = raw_text.strip()
        self._last_text = text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={
                "text": text,
                "session_id": self._session_id,
                "backend_id": self.backend_id,
            },
            evidence=(
                f"model-backend:{self.backend_id}",
                "model-invocation:follow-up",
                f"content:sha256:{digest}",
            ),
        )

    def close(self) -> None:
        attempt_id = self._attempt_id
        session_id = self._session_id
        self._attempt_id = None
        self._last_text = None
        self._session_id = None
        if self.audit is not None and session_id is not None:
            self.audit.append(
                "child_session_terminated",
                status="succeeded",
                payload={
                    "emulated_session": True,
                    "controller_handle_active": False,
                    "process_alive": False,
                    "termination_scope": "in_memory_conversation_state",
                },
                actor=AuditActor(
                    attempt_id or "model-child",
                    "model_child",
                    parent_id=(
                        attempt_id.rsplit("/child-", 1)[0]
                        if attempt_id and "/child-" in attempt_id
                        else None
                    ),
                ),
                attempt_id=attempt_id,
                session_id=session_id,
                backend_id=self.backend_id,
            )

    @staticmethod
    def _failed(attempt: TaskAttempt, error: str) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="failed",
            payload={"error": error},
        )
