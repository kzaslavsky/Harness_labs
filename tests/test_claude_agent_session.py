"""Tests for the headless-Claude AgentSession without invoking a model.

The fake ``claude`` executable used here is a real subprocess that speaks the
session's actual loopback MCP bridge over HTTP and emits genuine stream-json
lines, so the blocking tools/call round-trip is exercised end to end.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from harness_labs.core.agent_sessions import (
    BackendFailure,
    FinalOutput,
    ModelRequest,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from harness_labs.core.claude_agent_session import (
    ClaudeAgentSession,
    ClaudeSessionError,
)


_FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Fake claude: drives the session's MCP bridge and emits stream-json."""
import json, os, sys, urllib.request

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def bridge_url():
    config = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])
    return config["mcpServers"]["controller"]["url"]

def rpc(url, method, params, request_id=1):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["result"]

def call_tool(url, name, arguments):
    return rpc(url, "tools/call", {"name": name, "arguments": arguments})

def result_envelope(answer, is_error=False):
    return {
        "type": "result",
        "subtype": "success" if not is_error else "error",
        "is_error": is_error,
        "result": json.dumps({"answer": answer}) if not is_error else answer,
        "structured_output": {"answer": answer} if not is_error else None,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 100,
            "output_tokens": 50,
        },
        "session_id": "sess-fake-1",
    }

scenario = os.environ["FAKE_CLAUDE_SCENARIO"]
emit({"type": "system", "subtype": "init", "session_id": "sess-fake-1"})
url = bridge_url()

if scenario == "two_tools":
    listing = rpc(url, "tools/list", {})
    tool_names = sorted(tool["name"] for tool in listing["tools"])
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "mcp__controller__spawn_child",
         "input": {"role": "file_reader", "objective": "Read the treasure"}}]}})
    first = call_tool(url, "spawn_child",
                      {"role": "file_reader", "objective": "Read the treasure"})
    first_payload = json.loads(first["content"][0]["text"])
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_2",
         "name": "mcp__controller__send_child_message",
         "input": {"child_attempt_id": "child-1", "message": "again"}}]}})
    second = call_tool(url, "send_child_message",
                       {"child_attempt_id": "child-1", "message": "again"})
    second_payload = json.loads(second["content"][0]["text"])
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_3", "name": "StructuredOutput",
         "input": {"answer": "internal delivery"}}]}})
    emit(result_envelope(json.dumps({
        "tools": tool_names,
        "first": first_payload,
        "second": second_payload,
        "first_is_error": first.get("isError"),
    })))
elif scenario == "unknown_tool":
    refused = call_tool(url, "not_a_tool", {})
    emit(result_envelope(refused["content"][0]["text"]))
elif scenario == "error_result":
    emit(result_envelope("model exploded", is_error=True))
elif scenario == "dies":
    pass
elif scenario == "bad_schema":
    envelope = result_envelope("x")
    envelope["structured_output"] = {"wrong": "shape"}
    envelope["result"] = "not json"
    emit(envelope)
else:
    raise SystemExit(f"unknown scenario: {scenario}")
'''


def _tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="spawn_child",
            description="Spawn one child attempt.",
            input_schema={"type": "object"},
        ),
        ToolSpec(
            name="send_child_message",
            description="Message a child attempt.",
            input_schema={"type": "object"},
        ),
    )


class ClaudeAgentSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="fake-claude-")
        self.addCleanup(self._temporary.cleanup)
        executable = Path(self._temporary.name) / "claude"
        executable.write_text(_FAKE_CLAUDE)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        self.executable = str(executable)

    def _session(self, scenario: str) -> ClaudeAgentSession:
        os.environ["FAKE_CLAUDE_SCENARIO"] = scenario
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SCENARIO", None)
        return ClaudeAgentSession(
            executable=self.executable, timeout_seconds=30.0
        )

    def test_capabilities_describe_resident_bridge_transport(self) -> None:
        session = ClaudeAgentSession()
        self.assertTrue(session.capabilities.persistent_sessions)
        self.assertTrue(session.capabilities.native_tool_calls)
        self.assertFalse(session.capabilities.resumable_sessions)
        self.assertTrue(session.capabilities.cached_input_reporting)
        self.assertTrue(session.capabilities.structured_output)

    def test_two_tool_turns_round_trip_through_the_bridge(self) -> None:
        session = self._session("two_tools")
        session_id = session.open(
            ModelRequest(task="find treasure", context={"hint": "x"}, tools=_tools())
        )
        self.assertEqual(session_id, "sess-fake-1")

        first = session.step(session_id)
        self.assertIsInstance(first, ToolCall)
        self.assertEqual(first.name, "spawn_child")
        self.assertEqual(first.arguments["role"], "file_reader")

        second = session.step(
            session_id,
            ToolResult(first.call_id, True, {"text": "gold doubloons"}),
        )
        self.assertIsInstance(second, ToolCall)
        self.assertEqual(second.name, "send_child_message")

        final = session.step(
            session_id,
            ToolResult(second.call_id, True, {"text": "still gold"}),
        )
        self.assertIsInstance(final, FinalOutput)
        report = json.loads(final.content)
        self.assertEqual(report["tools"], ["send_child_message", "spawn_child"])
        self.assertEqual(report["first"]["payload"], {"text": "gold doubloons"})
        self.assertEqual(report["second"]["payload"], {"text": "still gold"})
        self.assertFalse(report["first_is_error"])
        self.assertIsNotNone(final.usage)
        self.assertEqual(final.usage.input_tokens, 115)
        self.assertEqual(final.usage.cached_input_tokens, 5)
        self.assertEqual(final.usage.output_tokens, 50)
        self.assertIn("claude-transport:stream-json", final.evidence)
        session.close(session_id)
        self.assertIsNone(session.process_id)

    def test_unknown_bridge_tool_gets_the_refusal_text(self) -> None:
        session = self._session("unknown_tool")
        session_id = session.open(
            ModelRequest(
                task="t",
                context={},
                tools=_tools(),
                unavailable_tool_response="sorry, no such tool",
            )
        )
        final = session.step(session_id)
        self.assertIsInstance(final, FinalOutput)
        self.assertEqual(final.content, "sorry, no such tool")
        session.close(session_id)

    def test_error_result_surfaces_as_backend_failure(self) -> None:
        session = self._session("error_result")
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        event = session.step(session_id)
        self.assertIsInstance(event, BackendFailure)
        self.assertIn("model exploded", event.error)
        session.close(session_id)

    def test_process_death_surfaces_as_backend_failure(self) -> None:
        session = self._session("dies")
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        event = session.step(session_id)
        self.assertIsInstance(event, BackendFailure)
        self.assertIn("exited without a result", event.error)
        session.close(session_id)

    def test_schema_violation_surfaces_as_backend_failure(self) -> None:
        session = self._session("bad_schema")
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        event = session.step(session_id)
        self.assertIsInstance(event, BackendFailure)
        self.assertIn("violated its schema", event.error)
        session.close(session_id)

    def test_mismatched_tool_result_is_rejected(self) -> None:
        session = self._session("two_tools")
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        call = session.step(session_id)
        self.assertIsInstance(call, ToolCall)
        event = session.step(
            session_id, ToolResult("wrong-id", True, {"text": "x"})
        )
        self.assertIsInstance(event, BackendFailure)
        self.assertIn("does not match", event.error)
        session.close(session_id)

    def test_pending_call_requires_a_result(self) -> None:
        session = self._session("two_tools")
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        call = session.step(session_id)
        self.assertIsInstance(call, ToolCall)
        event = session.step(session_id)
        self.assertIsInstance(event, BackendFailure)
        self.assertIn("requires a result", event.error)
        session.close(session_id)

    def test_open_requires_tools_and_rejects_reopen(self) -> None:
        session = self._session("two_tools")
        with self.assertRaises(ClaudeSessionError):
            session.open(ModelRequest(task="t", context={}))
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        with self.assertRaises(ClaudeSessionError):
            session.open(ModelRequest(task="t", context={}, tools=_tools()))
        session.close(session_id)

    def test_unknown_session_identity_is_rejected(self) -> None:
        session = self._session("two_tools")
        with self.assertRaises(ClaudeSessionError):
            session.step("nope")
        session_id = session.open(
            ModelRequest(task="t", context={}, tools=_tools())
        )
        with self.assertRaises(ClaudeSessionError):
            session.step("someone-else")
        with self.assertRaises(ClaudeSessionError):
            session.close("someone-else")
        session.close(session_id)


if __name__ == "__main__":
    unittest.main()
