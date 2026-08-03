"""Tests for the minimal Task Attempt execution boundary."""

from __future__ import annotations

import unittest

from harness_labs import (
    AttemptRunner,
    InvalidAttempt,
    InvalidResult,
    TaskAttempt,
    TaskResult,
)


ATTEMPT = TaskAttempt(
    attempt_id="attempt-1",
    task_ref="task:sha256:abc",
    context_ref="context:sha256:def",
    grant_ref="grant-1",
)


class SuccessfulExecutor:
    def execute(self, attempt: TaskAttempt) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"message": "done"},
            evidence=("artifact:sha256:123",),
        )


class AttemptRunnerTests(unittest.TestCase):
    def test_runs_executor_and_returns_matching_result(self) -> None:
        result = AttemptRunner().run(ATTEMPT, SuccessfulExecutor())

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload, {"message": "done"})
        self.assertEqual(result.evidence, ("artifact:sha256:123",))

    def test_rejects_result_for_another_attempt(self) -> None:
        class StaleExecutor:
            def execute(self, attempt: TaskAttempt) -> TaskResult:
                return TaskResult(attempt_id="attempt-old", status="succeeded")

        with self.assertRaisesRegex(InvalidResult, "does not match"):
            AttemptRunner().run(ATTEMPT, StaleExecutor())

    def test_rejects_non_result_output(self) -> None:
        class InvalidExecutor:
            def execute(self, attempt: TaskAttempt) -> object:
                return {"status": "succeeded"}

        with self.assertRaisesRegex(InvalidResult, "must return TaskResult"):
            AttemptRunner().run(ATTEMPT, InvalidExecutor())  # type: ignore[arg-type]

    def test_rejects_unknown_status(self) -> None:
        class UnknownStatusExecutor:
            def execute(self, attempt: TaskAttempt) -> TaskResult:
                return TaskResult(
                    attempt_id=attempt.attempt_id,
                    status="maybe",  # type: ignore[arg-type]
                )

        with self.assertRaisesRegex(InvalidResult, "unsupported result status"):
            AttemptRunner().run(ATTEMPT, UnknownStatusExecutor())

    def test_rejects_empty_attempt_reference(self) -> None:
        with self.assertRaisesRegex(InvalidAttempt, "context_ref"):
            TaskAttempt(
                attempt_id="attempt-1",
                task_ref="task:sha256:abc",
                context_ref="",
                grant_ref="grant-1",
            )

    def test_rejects_non_string_supplied_context(self) -> None:
        with self.assertRaisesRegex(InvalidAttempt, "context must be a string"):
            TaskAttempt(
                attempt_id="attempt-1",
                task_ref="task:sha256:abc",
                context_ref="context:sha256:def",
                grant_ref="grant-1",
                context={"not": "text"},  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
