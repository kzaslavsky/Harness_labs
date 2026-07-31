"""Contract tests for the provider-neutral session tool loop."""

from __future__ import annotations

import unittest

from harness_labs import (
    BackendCapabilities,
    ChildAuthorization,
    ChildDispatcher,
    FinalOutput,
    InMemoryReferenceStore,
    ModelRequest,
    OmlxAgentSession,
    SessionToolExecutor,
    TaskAttempt,
    TaskResult,
    ToolCall,
    ToolResult,
    Usage,
)


class Reader:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        self.calls += 1
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": "there is booty here"},
            evidence=("file:sha256:abc",),
        )


class ToolSession:
    capabilities = BackendCapabilities(True, True, True, True, True)

    def __init__(self) -> None:
        self.request: ModelRequest | None = None
        self.process_alive_during_child = False
        self.opened = False
        self.closed = False

    def open(self, request: ModelRequest) -> str:
        self.request = request
        self.opened = True
        return "resident-1"

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        if tool_result is None:
            return ToolCall(
                "call-1",
                "spawn_child",
                {
                    "role": "file_reader",
                    "objective": "Read treasure_chest.txt",
                },
            )
        self.process_alive_during_child = self.opened and not self.closed
        return FinalOutput(
            tool_result.payload["payload"]["text"],
            usage=Usage(100, 60, 10),
            evidence=("session:resident",),
        )

    def close(self, session_id: str) -> None:
        self.closed = True


class FakeOmlxBackend:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.tasks: list[str] = []

    def generate(self, task, context):
        self.tasks.append(task)
        return self.answers.pop(0)


class SessionToolExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TaskAttempt(
            "treasure",
            "task:parent",
            "context:parent",
            "grant:parent",
        )
        self.store = InMemoryReferenceStore(
            {
                "task:parent": "Output what is in treasure_chest.txt.",
                "context:parent": {},
                "grant:parent": {
                    "capabilities": ["spawn_child"],
                    "child_roles": ["file_reader"],
                },
            }
        )
        self.reader = Reader()
        self.dispatcher = ChildDispatcher(
            self.root,
            {
                "file_reader": ChildAuthorization(
                    "file_reader",
                    "task:child",
                    "context:child",
                    "grant:child",
                    self.reader,
                )
            },
        )

    def test_one_loop_keeps_tool_session_open_while_child_runs(self) -> None:
        session = ToolSession()
        result = SessionToolExecutor(
            self.store, session, self.dispatcher
        ).execute(self.root)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["text"], "there is booty here")
        self.assertEqual(result.payload["usage"]["cached_input_tokens"], 60)
        self.assertEqual(self.reader.calls, 1)
        self.assertTrue(session.process_alive_during_child)
        self.assertTrue(session.closed)
        self.assertEqual(session.request.tools[0].name, "spawn_child")

    def test_non_native_tool_transport_still_dispatches_child(self) -> None:
        backend = FakeOmlxBackend(
            '{"role":"file_reader","objective":"Read treasure_chest.txt"}',
            "there is booty here",
        )
        result = SessionToolExecutor(
            self.store,
            OmlxAgentSession(backend=backend),
            self.dispatcher,
        ).execute(self.root)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["text"], "there is booty here")
        self.assertEqual(self.reader.calls, 1)
        self.assertEqual(len(self.dispatcher.events), 2)
        self.assertIn("Select the one controller tool", backend.tasks[0])
        self.assertIn("authorized child result", backend.tasks[1])

    def test_non_native_tool_transport_fails_closed_on_changed_child_answer(self) -> None:
        result = SessionToolExecutor(
            self.store,
            OmlxAgentSession(
                backend=FakeOmlxBackend(
                    '{"role":"file_reader","objective":"Read treasure_chest.txt"}',
                    "I guessed",
                )
            ),
            self.dispatcher,
        ).execute(self.root)

        self.assertEqual(result.status, "failed")
        self.assertIn("faithfully return", result.payload["error"])
        self.assertEqual(self.reader.calls, 1)


if __name__ == "__main__":
    unittest.main()
