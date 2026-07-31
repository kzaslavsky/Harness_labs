"""Model-backed child execution with controller-declared capabilities."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .attempts import TaskAttempt, TaskResult
from .text_executor import InMemoryReferenceStore, TextBackend, TextBackendError


@dataclass(frozen=True)
class ModelCapabilityExecutor:
    """Always run a model, while failing safely for unavailable capabilities."""

    store: InMemoryReferenceStore
    backend: TextBackend
    backend_id: str
    capabilities: frozenset[str]
    unavailable_response: str

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
        model_context: Mapping[str, Any] = context
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
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": text},
            evidence=evidence,
        )

    @staticmethod
    def _failed(attempt: TaskAttempt, error: str) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="failed",
            payload={"error": error},
        )
