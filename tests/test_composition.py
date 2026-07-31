"""Tests for bounded, policy-controlled child attempts."""

from __future__ import annotations

import unittest

from harness_labs import (
    ChildAuthorization,
    ChildDispatcher,
    ChildRequest,
    ChildRequestDenied,
    TaskAttempt,
    TaskResult,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.attempts: list[TaskAttempt] = []

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        self.attempts.append(attempt)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": "there is booty here"},
            evidence=("file:sha256:abc",),
        )


class ChildDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TaskAttempt(
            attempt_id="treasure",
            task_ref="task:parent",
            context_ref="context:parent",
            grant_ref="grant:spawn-reader",
        )
        self.reader = RecordingExecutor()
        self.dispatcher = ChildDispatcher(
            self.root,
            {
                "file_reader": ChildAuthorization(
                    role="file_reader",
                    task_ref="task:read-treasure",
                    context_ref="context:treasure",
                    grant_ref="grant:read-treasure",
                    backend_id="recording",
                    capabilities=frozenset({"read_file"}),
                    executor=self.reader,
                )
            },
        )

    def test_policy_constructs_and_runs_child_attempt(self) -> None:
        result = self.dispatcher.run_child(
            self.root,
            ChildRequest(
                role="file_reader",
                objective="Read treasure_chest.txt",
            ),
        )

        child = self.reader.attempts[0]
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(child.parent_attempt_id, self.root.attempt_id)
        self.assertEqual(child.task_ref, "task:read-treasure")
        self.assertEqual(child.grant_ref, "grant:read-treasure")
        self.assertEqual(
            [event.event_type for event in self.dispatcher.events],
            ["child_dispatched", "child_completed"],
        )
        self.assertEqual(
            self.dispatcher.events[-1].evidence,
            ("file:sha256:abc",),
        )
        self.assertEqual(self.dispatcher.events[0].backend_id, "recording")
        self.assertEqual(
            self.dispatcher.events[0].capabilities,
            ("read_file",),
        )

    def test_parent_cannot_select_an_unauthorized_role(self) -> None:
        with self.assertRaisesRegex(ChildRequestDenied, "not authorized"):
            self.dispatcher.run_child(
                self.root,
                ChildRequest(role="arbitrary_shell", objective="Read everything"),
            )

        self.assertEqual(self.reader.attempts, [])
        self.assertEqual(self.dispatcher.events, ())

    def test_child_count_is_bounded(self) -> None:
        request = ChildRequest(role="file_reader", objective="Read the treasure")
        self.dispatcher.run_child(self.root, request)

        with self.assertRaisesRegex(ChildRequestDenied, "maximum children"):
            self.dispatcher.run_child(self.root, request)

        self.assertEqual(len(self.reader.attempts), 1)

    def test_child_depth_is_bounded(self) -> None:
        request = ChildRequest(role="file_reader", objective="Read the treasure")
        self.dispatcher.run_child(self.root, request)
        child = self.reader.attempts[0]

        with self.assertRaisesRegex(ChildRequestDenied, "maximum child depth"):
            self.dispatcher.run_child(child, request)

        self.assertEqual(len(self.reader.attempts), 1)

    def test_unregistered_parent_cannot_spawn(self) -> None:
        imposter = TaskAttempt(
            attempt_id=self.root.attempt_id,
            task_ref="task:different",
            context_ref=self.root.context_ref,
            grant_ref=self.root.grant_ref,
        )

        with self.assertRaisesRegex(ChildRequestDenied, "not registered"):
            self.dispatcher.run_child(
                imposter,
                ChildRequest(role="file_reader", objective="Read the treasure"),
            )


if __name__ == "__main__":
    unittest.main()
