"""Tests for model-backed child capability enforcement."""

from __future__ import annotations

import unittest

from harness_labs import (
    TOOL_UNAVAILABLE_REFUSAL,
    InMemoryReferenceStore,
    ModelCapabilityExecutor,
    TaskAttempt,
)


class RecordingBackend:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls = []

    def generate(self, task, context):
        self.calls.append((task, context))
        return self.answers.pop(0)


class ModelCapabilityExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReferenceStore(
            {
                "task:read": "Read treasure_chest.txt.",
                "context:file": {"path": "/secret/treasure_chest.txt"},
                "grant:read": {"capabilities": ["read_file"]},
            }
        )
        self.attempt = TaskAttempt(
            attempt_id="parent/child-1",
            task_ref="task:read",
            context_ref="context:file",
            grant_ref="grant:read",
            parent_attempt_id="parent",
        )

    def test_missing_capability_still_runs_model_and_validates_refusal(self) -> None:
        backend = RecordingBackend(TOOL_UNAVAILABLE_REFUSAL)
        result = ModelCapabilityExecutor(
            store=self.store,
            backend=backend,
            backend_id="omlx",
            capabilities=frozenset(),
            unavailable_response=TOOL_UNAVAILABLE_REFUSAL,
        ).execute(self.attempt)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["text"], TOOL_UNAVAILABLE_REFUSAL)
        self.assertEqual(len(backend.calls), 1)
        self.assertNotIn("/secret", str(backend.calls[0]))
        self.assertIn("model-invocation:completed", result.evidence)
        self.assertIn("capability:read_file:unavailable", result.evidence)

    def test_missing_capability_rejects_unfaithful_model_response(self) -> None:
        backend = RecordingBackend("I read it anyway")
        result = ModelCapabilityExecutor(
            store=self.store,
            backend=backend,
            backend_id="omlx",
            capabilities=frozenset(),
            unavailable_response=TOOL_UNAVAILABLE_REFUSAL,
        ).execute(self.attempt)

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(backend.calls), 1)
        self.assertIn("required capability refusal", result.payload["error"])

    def test_retained_model_receives_followup_and_can_be_closed(self) -> None:
        backend = RecordingBackend(
            TOOL_UNAVAILABLE_REFUSAL,
            "The missing read_file capability required me to refuse.",
        )
        executor = ModelCapabilityExecutor(
            store=self.store,
            backend=backend,
            backend_id="omlx",
            capabilities=frozenset(),
            unavailable_response=TOOL_UNAVAILABLE_REFUSAL,
            keep_alive=True,
        )
        initial = executor.execute(self.attempt)
        followup = executor.send(
            self.attempt,
            "what enabled you to answer me this way?",
        )
        executor.close()
        after_close = executor.send(self.attempt, "Are you there?")

        self.assertEqual(initial.status, "succeeded")
        self.assertEqual(followup.status, "succeeded")
        self.assertIn("model-invocation:follow-up", followup.evidence)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(after_close.status, "failed")


if __name__ == "__main__":
    unittest.main()
