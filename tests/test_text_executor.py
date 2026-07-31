"""End-to-end tests for the minimal text executor."""

from __future__ import annotations

import hashlib
import unittest
from typing import Any, Mapping

from harness_labs import (
    AttemptRunner,
    InMemoryReferenceStore,
    TaskAttempt,
    TextExecutor,
)


class RecordingPoemBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        self.calls.append((task, context))
        return "The operator tends the code,\nAnd lights each careful road."


class TextExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {
            "task:poem": "write a poem about the operator",
            "context:operator": {"subject": "the operator"},
            "grant:text": {"capabilities": ["generate_text"]},
        }
        self.attempt = TaskAttempt(
            attempt_id="poem-1",
            task_ref="task:poem",
            context_ref="context:operator",
            grant_ref="grant:text",
        )

    def test_performs_poem_attempt_through_runner(self) -> None:
        backend = RecordingPoemBackend()
        executor = TextExecutor(
            store=InMemoryReferenceStore(self.values),
            backend=backend,
        )

        result = AttemptRunner().run(self.attempt, executor)

        poem = result.payload["text"]
        digest = hashlib.sha256(poem.encode("utf-8")).hexdigest()
        self.assertEqual(result.status, "succeeded")
        self.assertIn("operator", poem.lower())
        self.assertEqual(
            backend.calls,
            [
                (
                    "write a poem about the operator",
                    {"subject": "the operator"},
                )
            ],
        )
        self.assertEqual(result.evidence, (f"content:sha256:{digest}",))

    def test_blocks_without_text_generation_grant(self) -> None:
        values = dict(self.values)
        values["grant:text"] = {"capabilities": []}
        result = AttemptRunner().run(
            self.attempt,
            TextExecutor(
                store=InMemoryReferenceStore(values),
                backend=RecordingPoemBackend(),
            ),
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("generate_text", result.payload["error"])

    def test_fails_on_unresolved_reference(self) -> None:
        result = AttemptRunner().run(
            self.attempt,
            TextExecutor(
                store=InMemoryReferenceStore({}),
                backend=RecordingPoemBackend(),
            ),
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("task:poem", result.payload["error"])


if __name__ == "__main__":
    unittest.main()
