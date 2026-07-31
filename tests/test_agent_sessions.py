"""Contract tests for the provider-neutral session tool loop."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness_labs import (
    AuditActor,
    AuditJournal,
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
    ToolSpec,
    Usage,
)


class Reader:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []
        self.closed = False

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        self.calls += 1
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": "there is booty here"},
            evidence=("file:sha256:abc",),
        )

    def send(self, attempt: TaskAttempt, message: str) -> TaskResult:
        self.messages.append(message)
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": "The read_file capability enabled me."},
            evidence=("model-invocation:follow-up",),
        )

    def close(self) -> None:
        self.closed = True


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


class RetainedToolSession:
    capabilities = BackendCapabilities(True, True, True, True, True)

    def __init__(self) -> None:
        self.turn = 0
        self.initial_text = ""

    def open(self, request: ModelRequest) -> str:
        self.request = request
        return "retained-parent"

    def step(self, session_id: str, tool_result: ToolResult | None = None):
        if self.turn == 0:
            self.turn = 1
            return ToolCall(
                "spawn-1",
                "spawn_child",
                {"role": "file_reader", "objective": "Read the treasure"},
            )
        if self.turn == 1 and tool_result is not None:
            self.turn = 2
            self.initial_text = tool_result.payload["payload"]["text"]
            return ToolCall(
                "message-1",
                "send_child_message",
                {
                    "child_attempt_id": tool_result.payload["attempt_id"],
                    "message": "what enabled you to answer me this way?",
                },
            )
        return FinalOutput(self.initial_text)

    def close(self, session_id: str) -> None:
        pass


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
                    "capabilities": ["spawn_child", "send_child_message"],
                    "child_roles": ["file_reader"],
                },
            }
        )
        self.reader = Reader()
        self.dispatcher = ChildDispatcher(
            self.root,
            {
                "file_reader": ChildAuthorization(
                    role="file_reader",
                    task_ref="task:child",
                    context_ref="context:child",
                    grant_ref="grant:child",
                    backend_id="recording",
                    capabilities=frozenset({"read_file"}),
                    executor=self.reader,
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

    def test_audit_reconstructs_parent_child_and_session_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = AuditJournal(
                Path(temporary) / "audit-run",
                "audit-run",
                actor=AuditActor("controller", "controller"),
                evidence_classification="component",
            )
            reader = Reader()
            dispatcher = ChildDispatcher(
                self.root,
                {
                    "file_reader": ChildAuthorization(
                        role="file_reader",
                        task_ref="task:child",
                        context_ref="context:child",
                        grant_ref="grant:child",
                        backend_id="recording",
                        capabilities=frozenset({"read_file"}),
                        executor=reader,
                    )
                },
                audit=audit,
            )
            result = SessionToolExecutor(
                self.store,
                ToolSession(),
                dispatcher,
                audit=audit,
            ).execute(self.root)
            audit.finalize(
                result.status,
                result={
                    "attempt_id": result.attempt_id,
                    "status": result.status,
                },
            )

            rows = [
                json.loads(line)
                for line in audit.events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            event_types = [row["event_type"] for row in rows]

            self.assertEqual(result.status, "succeeded")
            self.assertIn("authorization_bound", event_types)
            self.assertIn("child_dispatched", event_types)
            self.assertIn("tool_result", event_types)
            self.assertIn("session_closed", event_types)
            self.assertIn("attempt_completed", event_types)
            self.assertTrue(
                any(
                    row["actor"]["id"] == "treasure/child-1"
                    and row["parent_attempt_id"] == "treasure"
                    for row in rows
                )
            )
            AuditJournal.verify(audit.run_dir)

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
                    "I still guessed",
                )
            ),
            self.dispatcher,
        ).execute(self.root)

        self.assertEqual(result.status, "failed")
        self.assertIn("faithfully return", result.payload["error"])
        self.assertEqual(self.reader.calls, 1)

    def test_parent_messages_retained_child_then_controller_terminates_it(self) -> None:
        result = SessionToolExecutor(
            self.store,
            RetainedToolSession(),
            self.dispatcher,
            max_tool_calls=2,
            keep_child_alive=True,
        ).execute(self.root)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["text"], "there is booty here")
        self.assertEqual(len(result.payload["child_turns"]), 2)
        self.assertEqual(
            self.reader.messages,
            ["what enabled you to answer me this way?"],
        )
        self.assertTrue(self.reader.closed)
        self.assertEqual(
            self.dispatcher.events[-1].event_type,
            "child_terminated",
        )

    def test_omlx_transport_emulates_two_tool_turns(self) -> None:
        child_id = "treasure/child-1"
        backend = FakeOmlxBackend(
            '{"role":"file_reader","objective":"Read treasure_chest.txt"}',
            (
                '{"child_attempt_id":"treasure/child-1",'
                '"message":"what enabled you to answer me this way?"}'
            ),
            "there is booty here",
        )
        session = OmlxAgentSession(backend=backend)
        session_id = session.open(
            ModelRequest(
                task="Read, then ask the required follow-up.",
                context={},
                tools=(
                    ToolSpec(
                        "spawn_child",
                        "Spawn",
                        {
                            "properties": {
                                "role": {"enum": ["file_reader"]}
                            }
                        },
                    ),
                    ToolSpec(
                        "send_child_message",
                        "Message",
                        {"properties": {}},
                    ),
                ),
            )
        )

        spawn = session.step(session_id)
        followup = session.step(
            session_id,
            ToolResult(
                spawn.call_id,
                True,
                {
                    "attempt_id": child_id,
                    "payload": {"text": "there is booty here"},
                },
            ),
        )
        final = session.step(
            session_id,
            ToolResult(
                followup.call_id,
                True,
                {
                    "attempt_id": child_id,
                    "payload": {"text": "read_file enabled me"},
                },
            ),
        )

        self.assertEqual(spawn.name, "spawn_child")
        self.assertEqual(followup.name, "send_child_message")
        self.assertEqual(final.content, "there is booty here")
        self.assertEqual(len(backend.tasks), 3)


if __name__ == "__main__":
    unittest.main()
