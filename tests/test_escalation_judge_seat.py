"""The graph-level escalation judge seat (ADR 0007 / CC-08's EscalationJudge).

Every test drives a fake ``AgentSession``; nothing here calls a real model or
opens a network connection.
"""

import json
import unittest

from harness_labs.core.agent_sessions import (
    BackendCapabilities,
    BackendFailure,
    FinalOutput,
    ModelRequest,
    ToolCall,
)
from harness_labs.graphrun.escalation_judge import (
    DEFAULT_JUDGE_IDENTITY,
    ConfirmEverythingStubJudge,
    GraphEscalationJudgeSeat,
)
from harness_labs.plangraph.plan_graph import (
    ESCALATION_JUDGMENT_PROTOCOL,
    EscalationJudgeUnavailable,
    PlanGraph,
    PlanGraphError,
    PlanGraphPlan,
    PlanRun,
)


def plan() -> PlanGraphPlan:
    return PlanGraphPlan(
        plan="docs/approved-plan.md",
        base_commit="0" * 40,
        runs=(
            PlanRun(
                id="a", objective="Build A", plan_sections=("1",), criteria=("AC-1",),
                allowed_paths=("producer.py",),
            ),
            PlanRun(
                id="b", objective="Build B", plan_sections=("2",), criteria=("AC-2",),
                depends_on=("a",), allowed_paths=("consumer.py",),
            ),
        ),
        plan_sections={"1": "one", "2": "two"},
        acceptance_criteria={"AC-1": "one", "AC-2": "two"},
    )


PACKET = {
    "key": "consumer.py:needs-producer",
    "required_paths": ["producer.py"],
    "statement": "This finding needs a path outside my grant.",
    "origin_node": "b",
    "origin_reviewer_id": "b",
    "owner_node": "a",
}


class _FakeSession:
    """One scripted ``AgentSession``: a queue of events per opened session."""

    capabilities = BackendCapabilities(
        persistent_sessions=False, native_tool_calls=False,
        resumable_sessions=False, cached_input_reporting=False,
        structured_output=False,
    )

    def __init__(self, events, log) -> None:
        self.events = list(events)
        self.log = log

    def open(self, request: ModelRequest) -> str:
        self.log.setdefault("requests", []).append(request)
        return "session-1"

    def step(self, session_id, tool_result=None):
        if not self.events:
            raise AssertionError("fake session stepped past its script")
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    def close(self, session_id) -> None:
        self.log["closed"] = self.log.get("closed", 0) + 1


def seat(*scripts, **options) -> tuple[GraphEscalationJudgeSeat, dict]:
    """A seat whose Nth judgment attempt replays the Nth scripted session."""

    log: dict = {"opened": 0}
    remaining = list(scripts)

    def factory():
        log["opened"] += 1
        if not remaining:
            raise AssertionError("seat opened more sessions than scripted")
        return _FakeSession(remaining.pop(0), log)

    options.setdefault("plan", plan())
    return GraphEscalationJudgeSeat(session_factory=factory, **options), log


def final(payload) -> FinalOutput:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return FinalOutput(content=text)


VALID = {
    "protocol": ESCALATION_JUDGMENT_PROTOCOL,
    "verdict": "confirm",
    "rationale": "producer.py is genuinely outside b's grant",
    "evidence_refs": ["ref-1"],
}


class SeatIdentityTests(unittest.TestCase):
    def test_identity_is_a_seat_name_independent_of_the_backend_spec(self) -> None:
        claude, _ = seat([final(VALID)], spec="claude:claude-opus-5@high")
        codex, _ = seat([final(VALID)], spec="codex:gpt-5-codex@high")

        self.assertEqual(claude.identity, DEFAULT_JUDGE_IDENTITY)
        self.assertEqual(codex.identity, claude.identity)

    def test_identity_colliding_with_a_node_id_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(PlanGraphError, "collides with a plan node id"):
            seat([final(VALID)], identity="b")

    def test_default_identity_is_not_a_node_id_shape_used_by_this_plan(self) -> None:
        self.assertNotIn(DEFAULT_JUDGE_IDENTITY, [run.id for run in plan().runs])


class SeatJudgmentTests(unittest.TestCase):
    def test_valid_reply_is_returned_and_passes_the_graph_validator(self) -> None:
        judge, log = seat([final(VALID)])

        judgment = judge(PACKET)

        self.assertEqual(PlanGraph._validate_judgment(judgment), dict(judgment))
        self.assertEqual(judgment["verdict"], "confirm")
        self.assertEqual(log["opened"], 1)
        self.assertEqual(log["closed"], 1)

    def test_fenced_and_prose_wrapped_replies_are_normalized_by_the_seat(self) -> None:
        """Structured-output enforcement differs per provider, so the seat --
        not the backend -- owns extraction, identically for every provider."""

        for reply in (
            "```json\n" + json.dumps(VALID) + "\n```",
            "Here is my judgment:\n" + json.dumps(VALID) + "\nThanks.",
        ):
            with self.subTest(reply=reply[:20]):
                judge, _ = seat([final(reply)])
                self.assertEqual(judge(PACKET)["verdict"], "confirm")

    def test_missing_protocol_tag_is_stamped_rather_than_refused(self) -> None:
        payload = {k: v for k, v in VALID.items() if k != "protocol"}
        judge, _ = seat([final(payload)])

        self.assertEqual(judge(PACKET)["protocol"], ESCALATION_JUDGMENT_PROTOCOL)

    def test_packet_and_whole_plan_context_reach_the_session(self) -> None:
        judge, log = seat([final(VALID)])
        judge.sealed_nodes = lambda: ("a",)

        judge(PACKET)

        context = log["requests"][0].context
        self.assertEqual(context["escalation"], dict(PACKET))
        self.assertEqual(context["routing"]["owner_node"], "a")
        nodes = {node["id"]: node for node in context["graph"]["nodes"]}
        self.assertEqual(set(nodes), {"a", "b"})
        self.assertEqual(nodes["a"]["allowed_paths"], ["producer.py"])
        self.assertEqual(nodes["b"]["depends_on"], ["a"])
        self.assertTrue(nodes["a"]["sealed"])
        self.assertFalse(nodes["b"]["sealed"])

    def test_sealed_state_is_re_read_for_every_judgment(self) -> None:
        """The seat outlives many judgments; the sealed set must not be a
        snapshot taken when the seat was constructed."""

        judge, log = seat([final(VALID)], [final(VALID)])
        sealed: list[str] = []
        judge.sealed_nodes = lambda: tuple(sealed)

        judge(PACKET)
        sealed.append("a")
        judge(PACKET)

        first, second = (request.context["graph"]["nodes"] for request in log["requests"])
        self.assertFalse({node["id"]: node for node in first}["a"]["sealed"])
        self.assertTrue({node["id"]: node for node in second}["a"]["sealed"])

    def test_unwired_sealed_state_is_reported_unknown_not_empty(self) -> None:
        judge, log = seat([final(VALID)])

        judge(PACKET)

        graph = log["requests"][0].context["graph"]
        self.assertEqual(graph["sealed_state"], "unknown")
        self.assertNotIn("sealed", graph["nodes"][0])

    def test_instructions_carry_the_reject_asymmetry_calibration(self) -> None:
        judge, log = seat([final(VALID)])
        from harness_labs.graphrun.escalation_judge import SEAT_INSTRUCTIONS

        self.assertIn("PERMANENT", SEAT_INSTRUCTIONS)
        self.assertIn("required_paths", SEAT_INSTRUCTIONS)
        self.assertIn("When you genuinely\ncannot tell, confirm", SEAT_INSTRUCTIONS)
        self.assertIn("not choosing the owner", SEAT_INSTRUCTIONS.replace("NOT", "not"))


class SeatFailurePolicyTests(unittest.TestCase):
    def test_malformed_reply_is_retried_on_a_fresh_session_then_accepted(self) -> None:
        judge, log = seat([final("no json here")], [final(VALID)])

        judgment = judge(PACKET)

        self.assertEqual(judgment["verdict"], "confirm")
        self.assertEqual(log["opened"], 2)
        self.assertEqual(log["closed"], 2)
        self.assertIn("previous reply was rejected", log["requests"][1].task)

    def test_exhausted_retries_refuse_rather_than_fabricate_a_verdict(self) -> None:
        judge, log = seat([final("garbage")], [final({"verdict": "maybe"})])

        with self.assertRaises(EscalationJudgeUnavailable) as caught:
            judge(PACKET)

        self.assertIn("no valid judgment", str(caught.exception))
        self.assertEqual(log["closed"], 2, "every attempt's session is closed")

    def test_backend_failure_refuses_rather_than_fabricating_a_verdict(self) -> None:
        judge, _ = seat(
            [BackendFailure(error="transport died")],
            [BackendFailure(error="transport died")],
        )

        with self.assertRaises(EscalationJudgeUnavailable) as caught:
            judge(PACKET)

        self.assertIn("transport died", str(caught.exception))

    def test_a_session_that_never_finalizes_refuses(self) -> None:
        call = ToolCall(call_id="1", name="whatever", arguments={})
        judge, _ = seat([call] * 8, [call] * 8, max_steps=2)

        with self.assertRaises(EscalationJudgeUnavailable):
            judge(PACKET)

    def test_transport_exception_refuses_rather_than_propagating(self) -> None:
        judge, _ = seat([RuntimeError("socket closed")], [RuntimeError("socket closed")])

        with self.assertRaises(EscalationJudgeUnavailable) as caught:
            judge(PACKET)

        self.assertIn("socket closed", str(caught.exception))

    def test_refusal_is_identical_for_every_provider_spec(self) -> None:
        for spec in ("claude:claude-opus-5@high", "codex:gpt-5-codex@high"):
            with self.subTest(spec=spec):
                judge, _ = seat([final("garbage")], [final("garbage")], spec=spec)
                with self.assertRaises(EscalationJudgeUnavailable):
                    judge(PACKET)


class StubJudgeTests(unittest.TestCase):
    def test_stub_confirms_everything_with_a_schema_valid_judgment(self) -> None:
        stub = ConfirmEverythingStubJudge()

        judgment = stub(PACKET)

        self.assertEqual(PlanGraph._validate_judgment(judgment), dict(judgment))
        self.assertEqual(judgment["verdict"], "confirm")
        self.assertEqual(stub.packets, [dict(PACKET)])

    def test_stub_identity_is_not_a_node_id(self) -> None:
        self.assertNotIn(
            ConfirmEverythingStubJudge().identity, [run.id for run in plan().runs]
        )

    def test_stub_declares_itself_a_test_instrument(self) -> None:
        self.assertIn("NOT a production", ConfirmEverythingStubJudge.__doc__)


if __name__ == "__main__":
    unittest.main()
