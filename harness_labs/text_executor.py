"""A minimal executor for referenced text-generation tasks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .attempts import TaskAttempt, TaskResult


class TextBackend(Protocol):
    """A replaceable text generator."""

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        """Generate text for a task and its context."""


@dataclass(frozen=True)
class InMemoryReferenceStore:
    """Resolve attempt references from an in-memory mapping."""

    values: Mapping[str, Any]

    def resolve(self, reference: str) -> Any:
        return self.values[reference]


@dataclass(frozen=True)
class TextExecutor:
    """Resolve a text task, enforce its grant, and invoke a backend."""

    store: InMemoryReferenceStore
    backend: TextBackend

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        try:
            task = self.store.resolve(attempt.task_ref)
            context = self.store.resolve(attempt.context_ref)
            grant = self.store.resolve(attempt.grant_ref)
        except KeyError as exc:
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": f"unresolved reference: {exc.args[0]}"},
            )

        if not isinstance(grant, Mapping) or "generate_text" not in grant.get(
            "capabilities", ()
        ):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="blocked",
                payload={"error": "generate_text capability is required"},
            )
        if not isinstance(task, str) or not task.strip():
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": "task must resolve to non-empty text"},
            )
        if not isinstance(context, Mapping):
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": "context must resolve to a mapping"},
            )

        text = self.backend.generate(task, context)
        if not isinstance(text, str) or not text.strip():
            return TaskResult(
                attempt_id=attempt.attempt_id,
                status="failed",
                payload={"error": "backend returned no text"},
            )

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": text},
            evidence=(f"content:sha256:{digest}",),
        )
