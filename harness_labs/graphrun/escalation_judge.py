"""The graph-level coordinator seat that fills CC-08's ``EscalationJudge``.

ADR 0007 (``docs/decisions/0007-in-graph-escalation-bounded-unsealing.md``)
shipped the whole escalation pipeline except its judgment seat: recognition,
routing, validation, unseal and cascade all exist, and
``PlanGraph(escalation_judge=...)`` had no implementation to receive. This
module is that missing rung.

Why the seat lives at graph level, and why here
-----------------------------------------------
The question a judgment answers is "does this finding genuinely require a path
this node does not own, and is the owner's claim real?". Answering it needs the
whole plan's grants, its dependency edges, and which nodes are sealed right
now. A node-level reviewer -- the seat CC-08's review-fix loop already has --
sees only its own node, so it cannot answer it; the graph controller sees all
of it. Independence then comes free: a graph seat's identity is never a node
id, so the reviewer-independence refusal (AC-CC08-7) can never fire for
structural reasons.

The module sits in the graphrun layer, not plangraph, because it binds an
``AgentSession`` (core) to a ``PlanGraph`` contract (plangraph); graphrun is
the only layer allowed to import both. Nothing in plangraph imports it, so
``escalation_judge=None`` remains the default and costs nothing (AC-CC08-1).

Platform neutrality
-------------------
The seat takes a ``provider:model[@effort]`` spec and binds it through
:func:`~harness_labs.graphrun.agent_mixture.build_coordinator_session`, the
same provider-neutral seat binding every other coordinator in this harness
uses. It never touches ``ClaudeSemanticTaskExecutor`` or any other
provider-bound executor, so a ``codex:`` seat -- or any future provider added
to ``_PROVIDER_BACKEND_IDS`` -- works without editing this file. Everything
that guarantees the contract (JSON extraction, schema validation, retry,
refusal) lives in the seat, above the transport, so every backend gets
byte-identical guarantees rather than inheriting whatever structured-output
enforcement its provider happens to offer.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from harness_labs.core.agent_sessions import (
    AgentSession,
    BackendFailure,
    FinalOutput,
    ModelRequest,
)
from harness_labs.core.audit import AuditJournal
from harness_labs.core.usage import ModelPrice
from harness_labs.graphrun.agent_mixture import BackendSpec, build_coordinator_session
from harness_labs.plangraph.plan_graph import (
    ESCALATION_JUDGMENT_PROTOCOL,
    EscalationJudgeUnavailable,
    PlanGraph,
    PlanGraphError,
    PlanGraphPlan,
)

# A seat name, not a derived one. It must never collide with a node id (the
# constructor refuses that outright) and it must not change when the operator
# swaps providers: `.identity` is what the reviewer-independence refusal
# compares, so deriving it from the spec would make a provider swap silently
# change independence semantics, and would make the same seat unrecognisable
# across two attempts of one lineage.
DEFAULT_JUDGE_IDENTITY = "graph-escalation-judge"

SEAT_INSTRUCTIONS = """\
You are the graph-level escalation judge for one PlanGraph run. You are not
any node's reviewer and you are not the coordinator of any FeatureRun. You
answer exactly one question per call, in JSON, and nothing else.

The question: does the escalated finding genuinely require a path the
escalating node does not own?

What you are NOT deciding:
- You are not choosing the owner. Ownership was already resolved by a
  deterministic longest-prefix lookup over the plan's grants before you were
  called. `owner_node` is a fact in your input, not a proposal. Never
  second-guess it, never suggest a different owner, never treat a
  disagreement with it as grounds for a verdict.
- You are not deciding whether the finding is worth fixing, how to fix it, or
  who should fix it. Only whether it truly reaches outside the escalating
  node's grant.

Your verdicts:
- "confirm" -- the finding genuinely needs work in a path the escalating node
  does not own. The finding is routed to `owner_node`; if that node is
  already sealed it is unsealed and re-run under a bounded fix, which is
  costly but reversible.
- "reject" -- the escalation is unfounded: the work the finding describes can
  be done entirely within the escalating node's own grant, or the finding
  does not describe real work at all.

Calibration -- read this twice, it is the asymmetry that matters:
A reject is PERMANENT. This graph's journal makes it stick: if the same
finding key is escalated again anywhere in this lineage, it is not re-judged
-- the run blocks for a human operator instead. One wrong reject poisons that
finding for good. A confirm is merely expensive: it costs a re-run and is
undone by the owner finding nothing to do.

So reject ONLY when you are confident the escalation is unfounded. Do not
reject because the record is imprecise, because `required_paths` is broader
or vaguer than it needed to be, because the statement is poorly written,
because the severity looks inflated, or because you would have described the
problem differently. A sloppy escalation from a node that was nonetheless
right about needing someone else's file is a CONFIRM. When you genuinely
cannot tell, confirm: the cost of a needless re-run is recoverable, the cost
of a wrong permanent no is not.

Answer with a single JSON object and no prose, no markdown fence, no
commentary:
{"protocol": "plan-graph-escalation-judgment/1",
 "verdict": "confirm" | "reject",
 "rationale": "<one or two sentences citing the specific grant or path that
   decided it>",
 "evidence_refs": ["<zero or more references you were given>"]}
"""

_RETRY_NOTE = """\

Your previous reply was rejected: {reason}
Reply again with ONLY the JSON object described above. No prose, no fence.
"""


class GraphEscalationJudgeSeat:
    """One graph-level ``EscalationJudge``, created once per graph run.

    Lifetime and context currency
    -----------------------------
    The *seat* is built once per graph run and reused across every judgment:
    it carries the plan, the instructions, and the backend spec. The
    *transport session* is rebuilt per judgment and closed afterwards, for
    three reasons: (1) each judgment must be decided on its own record --
    a resident session would let an earlier verdict prime a later one, which
    is precisely the contamination the reviewer-independence rule exists to
    prevent; (2) ``BackendCapabilities.persistent_sessions`` differs by
    provider, so per-call sessions are the only lifetime that behaves
    identically on every backend; (3) escalations are rare, so the reuse
    saving is negligible against the cost of a bad judgment.

    Graph context is likewise rebuilt per call. The static half (node ids,
    objectives, dependency edges, path grants) comes from the frozen plan; the
    dynamic half -- which nodes are sealed -- comes from ``sealed_nodes``,
    a provider invoked at judgment time, so the seat sees the graph as it is
    when it judges rather than as it was when it was constructed. Wire it to
    :meth:`PlanGraph.sealed_node_ids`; left unset it reports the sealed set as
    unknown rather than as empty, so the model is never told a false fact.

    Failure policy
    --------------
    Never returns a verdict it did not get from the model. A transport
    failure, an unparseable reply, or a reply that fails
    ``plan-graph-escalation-judgment/1`` is retried on a fresh session up to
    ``max_attempts`` times; if that is exhausted the seat raises
    :class:`EscalationJudgeUnavailable`, which ``PlanGraph`` turns into an
    ordinary operator block -- no verdict journaled, no budget spent, no
    finding key poisoned. Fabricating a verdict instead would mean either
    unsealing a node on no evidence (confirm) or permanently killing a
    finding key because a model call timed out (reject). Neither is a defensible
    thing to do on a transport error, and only the block is undoable.
    """

    def __init__(
        self,
        spec: str | BackendSpec = "claude:claude-opus-5@high",
        *,
        plan: PlanGraphPlan,
        identity: str = DEFAULT_JUDGE_IDENTITY,
        sealed_nodes: Callable[[], Sequence[str]] | None = None,
        session_factory: Callable[[], AgentSession] | None = None,
        audit: AuditJournal | None = None,
        executable: str | None = None,
        pricing: ModelPrice | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int = 2,
        max_steps: int = 4,
    ) -> None:
        if not isinstance(identity, str) or not identity.strip():
            raise PlanGraphError("escalation judge identity must be a non-empty string")
        node_ids = tuple(run.id for run in plan.runs)
        if identity in node_ids:
            # AC-CC08-7 refuses this at judgment time, after the escalation
            # has already been journaled. A graph seat can know at
            # construction that its name collides with a node, so it refuses
            # then -- before any launch, any spend, or any journal entry.
            raise PlanGraphError(
                f"escalation judge identity {identity!r} collides with a plan node id; "
                "a graph-level seat must be independent of every node"
            )
        if max_attempts < 1:
            raise PlanGraphError("escalation judge max_attempts must be positive")
        if max_steps < 1:
            raise PlanGraphError("escalation judge max_steps must be positive")
        self.identity = identity
        self.plan = plan
        self.spec = spec
        self.sealed_nodes = sealed_nodes
        self.max_attempts = max_attempts
        self.max_steps = max_steps
        self._session_factory = session_factory or (
            lambda: build_coordinator_session(
                spec,
                base_instructions=SEAT_INSTRUCTIONS,
                audit=audit,
                executable=executable,
                pricing=pricing,
                timeout_seconds=timeout_seconds,
            )
        )

    # -- context ---------------------------------------------------------

    def graph_context(self) -> dict[str, Any]:
        """The whole-plan view handed to the seat for one judgment."""

        sealed = self._sealed_ids()
        nodes = []
        for run in self.plan.runs:
            node: dict[str, Any] = {
                "id": run.id,
                "objective": run.objective,
                "depends_on": list(run.depends_on),
                "allowed_paths": list(run.allowed_paths),
            }
            if sealed is not None:
                node["sealed"] = run.id in sealed
            nodes.append(node)
        context: dict[str, Any] = {"nodes": nodes}
        if sealed is None:
            # Never report "nothing is sealed" when the truth is "not wired".
            context["sealed_state"] = "unknown"
        return context

    def _sealed_ids(self) -> frozenset[str] | None:
        if self.sealed_nodes is None:
            return None
        try:
            values = self.sealed_nodes()
        except Exception as exc:  # pragma: no cover - defensive
            raise EscalationJudgeUnavailable(
                f"sealed-node provider failed: {exc}"
            ) from exc
        return frozenset(str(value) for value in values)

    # -- judgment --------------------------------------------------------

    def __call__(self, packet: Mapping[str, object]) -> Mapping[str, object]:
        context = {
            "graph": self.graph_context(),
            "escalation": dict(packet),
            "routing": {
                "owner_node": packet.get("owner_node"),
                "origin_node": packet.get("origin_node"),
                "note": (
                    "owner_node was resolved deterministically from the plan's "
                    "grants before this call. It is not yours to revisit."
                ),
            },
        }
        task = (
            "Judge one escalated review finding: does it genuinely require a "
            "path the escalating node does not own, and is the owner's claim "
            "real? Answer with the judgment JSON object only."
        )
        failures: list[str] = []
        for attempt in range(self.max_attempts):
            suffix = (
                _RETRY_NOTE.format(reason=failures[-1]) if failures else ""
            )
            try:
                judgment, failure = self._one_attempt(task + suffix, context)
            except EscalationJudgeUnavailable:
                raise
            except Exception as exc:  # transport blew up in an unexpected way
                failure = f"session error: {exc}"
                judgment = None
            if judgment is not None:
                return judgment
            failures.append(str(failure))
        raise EscalationJudgeUnavailable(
            "escalation judge produced no valid judgment in "
            f"{self.max_attempts} attempt(s): " + "; ".join(failures)
        )

    def _one_attempt(
        self, task: str, context: Mapping[str, Any]
    ) -> tuple[dict[str, object] | None, str | None]:
        session = self._session_factory()
        request = ModelRequest(task=task, context=dict(context))
        session_id = session.open(request)
        try:
            for _ in range(self.max_steps):
                event = session.step(session_id)
                if isinstance(event, BackendFailure):
                    return None, f"backend failure: {event.error}"
                if isinstance(event, FinalOutput):
                    return self._parse(event.content)
                # A ToolCall: this seat offers no tools, so there is nothing
                # to answer with; step again and let the backend converge or
                # run out of steps.
            return None, "session produced no final output"
        finally:
            try:
                session.close(session_id)
            except Exception:  # pragma: no cover - close is best effort
                pass

    @staticmethod
    def _parse(content: object) -> tuple[dict[str, object] | None, str | None]:
        """Extract and validate one judgment from a backend's final output."""

        if not isinstance(content, str) or not content.strip():
            return None, "empty reply"
        raw = _extract_json_object(content)
        if raw is None:
            return None, "reply contained no JSON object"
        if not isinstance(raw, dict):
            return None, "reply JSON was not an object"
        candidate = dict(raw)
        # The protocol tag is the seat's own contract knowledge, not a model
        # judgment: stamping it when a backend omits it costs nothing, while
        # a wrong tag still fails validation below.
        candidate.setdefault("protocol", ESCALATION_JUDGMENT_PROTOCOL)
        candidate.setdefault("evidence_refs", [])
        try:
            # Deliberately the graph's own validator rather than a copy: the
            # seat's guarantee is "passes PlanGraph._validate_judgment", and
            # a second implementation could drift away from it.
            return PlanGraph._validate_judgment(candidate), None
        except PlanGraphError as exc:
            return None, f"invalid judgment: {exc}"


def _extract_json_object(content: str) -> object | None:
    """Best-effort JSON object extraction from a model reply.

    Backends differ in how much they wrap a structured answer, and the seat --
    not the provider -- owns this normalization so every backend behaves the
    same. Whole-string parse first, then a fenced block, then the first
    balanced brace span.
    """

    text = content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:
        for chunk in text.split("```")[1::2]:
            body = chunk.split("\n", 1)[-1] if chunk.lower().startswith("json") else chunk
            try:
                return json.loads(body.strip())
            except json.JSONDecodeError:
                continue
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class ConfirmEverythingStubJudge:
    """A test and smoke instrument. NOT a production judgment path.

    It confirms every escalation without looking at it, which exercises the
    whole mechanism -- routing, journal, unseal, cascade, retry frontier --
    with no model spend and no network. Wiring it into a real campaign would
    mean every escalated finding unseals its owner unexamined and spends a
    structural ``transfer_ownership`` decision, which is exactly the failure
    ADR 0007 introduced a judge to prevent. Use
    :class:`GraphEscalationJudgeSeat` for anything real.
    """

    def __init__(
        self,
        identity: str = "stub-confirm-escalation-judge",
        *,
        rationale: str = (
            "stub judge: confirmed without examination (test instrument, "
            "not a judgment)"
        ),
    ) -> None:
        self.identity = identity
        self.rationale = rationale
        self.packets: list[dict[str, object]] = []

    def __call__(self, packet: Mapping[str, object]) -> Mapping[str, object]:
        self.packets.append(dict(packet))
        return {
            "protocol": ESCALATION_JUDGMENT_PROTOCOL,
            "verdict": "confirm",
            "rationale": self.rationale,
            "evidence_refs": [],
        }


__all__ = [
    "DEFAULT_JUDGE_IDENTITY",
    "SEAT_INSTRUCTIONS",
    "ConfirmEverythingStubJudge",
    "GraphEscalationJudgeSeat",
]
