#!/usr/bin/env python3
"""Coordinate one bounded, external PlanGraph recovery attempt.

This program deliberately does not import or invoke ``PlanGraph.resume``.  It
records a registration-authorized decision in the lineage ledger, then starts
``run_plan_graph.py`` as a new top-level process.  A blocked coordinator is a
truthful terminal report, not a substitute success result.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_labs.plangraph.plan_graph import (
    PlanGraphRegistration,
    load_registration,
    plan_from_registration,
)
from harness_labs.plangraph.plan_graph_authority import AutomaticRecoveryAuthority
from harness_labs.plangraph.plan_graph_budget import BudgetError, RetryBudgetLedger
from harness_labs.featurerun.feature_run import classify_verification_failure


_HUMAN_CLASSIFICATIONS = {"policy_violation", "structural_decision"}
_CLASSIFICATIONS = _HUMAN_CLASSIFICATIONS | {
    "product", "indeterminate", "infrastructure_transient",
    "harness_or_configuration",
}
_DECISION_PROTOCOL = "plan-graph-recovery-decision/1"


class RecoveryCoordinatorError(ValueError):
    """The escalation cannot safely be recovered automatically."""


@dataclass(frozen=True)
class CoordinatorResult:
    status: str
    reason: str
    decision: Mapping[str, object] | None = None
    resume_argv: tuple[str, ...] = ()
    resume_returncode: int | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "decision": dict(self.decision) if self.decision else None,
            "resume_argv": list(self.resume_argv),
            "resume_returncode": self.resume_returncode,
        }


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RecoveryCoordinatorError(f"{name} must be an object")
    return value


def load_escalation(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryCoordinatorError("could not read block escalation") from exc
    value = _object(value, "block escalation")
    required = {
        "protocol", "graph_run_id", "logical_graph_id", "blocked_node_id",
        "status_flags", "nodes", "budget_state", "significance_guidance",
        "resume_directive_template",
    }
    if value.get("protocol") != "plan-graph-block-escalation/1" or not required.issubset(value):
        raise RecoveryCoordinatorError("unsupported or incomplete block escalation")
    if (not isinstance(value["nodes"], list)
            or not isinstance(value["graph_run_id"], str)
            or not isinstance(value["significance_guidance"], Mapping)):
        raise RecoveryCoordinatorError("block escalation has invalid nodes, graph_run_id, or significance guidance")
    return value


class RecoveryCoordinator:
    """Classify an escalation, consume one bounded decision, and resume fresh."""

    def __init__(
        self, *, repository: Path, registration: PlanGraphRegistration,
        run_root: Path, iteration_cap: int = 3,
        runner: Callable[[Sequence[str]], int] | None = None,
    ) -> None:
        if iteration_cap < 1:
            raise RecoveryCoordinatorError("iteration cap must be positive")
        self.repository = repository.resolve()
        self.registration = registration
        self.run_root = run_root.resolve()
        self.iteration_cap = iteration_cap
        self.runner = runner or self._run_subprocess
        try:
            self.authority = AutomaticRecoveryAuthority.from_mapping(
                registration.automatic_recovery
            )
            # The escalation is diagnostic input.  Recover only against the
            # criteria frozen in the registration, never criteria supplied by
            # the producer of a blocked result.
            self.plan = plan_from_registration(registration)
        except ValueError as exc:
            raise RecoveryCoordinatorError("registration has invalid recovery authority") from exc
        self.ledger = RetryBudgetLedger(self.run_root, registration.plan_lineage_id)

    @staticmethod
    def _run_subprocess(argv: Sequence[str]) -> int:
        return subprocess.run(list(argv), check=False).returncode

    def recover(
        self, escalation: Mapping[str, object], *, launcher_argv: Sequence[str],
        requested_action: str | None = None,
    ) -> CoordinatorResult:
        """Attempt exactly one safe recovery action.

        ``requested_action`` is the executor-factory seam: a future LLM
        executor may supply its classified vocabulary without receiving ledger
        mutation authority.  Unsupported, human-tier, and stop choices fail
        closed.
        """
        try:
            target, classification = self._target(escalation)
            state = self._ledger_state()
        except (RecoveryCoordinatorError, BudgetError) as exc:
            return CoordinatorResult("externally_blocked", str(exc))
        if classification in _HUMAN_CLASSIFICATIONS:
            return CoordinatorResult("requires_human", f"{classification} requires human authority")
        if self._decision_count(state) >= self.iteration_cap:
            return CoordinatorResult("externally_blocked", "recovery iteration cap exhausted")
        action = requested_action or self._default_action(target, state)
        if action is None:
            return CoordinatorResult(
                "externally_blocked", "authorized Tier-1 recovery actions exhausted"
            )
        if action in {"stop", "adjust_plan", "escalate_human", "transfer_ownership", "ratify_gate_change"}:
            return CoordinatorResult("requires_human", f"{action} requires human authority")
        if action == "retry":
            action = "resume"
        if action not in {"resume", "extend_budget"}:
            return CoordinatorResult("requires_human", "no Tier-1 recovery action is authorized")
        try:
            decision = self._decision(action, target, state)
            if self._is_repeat(state, decision):
                return CoordinatorResult("externally_blocked", "repeat recovery decision refused", decision)
            self.ledger.apply_recovery_decision(decision, prior_digest=decision["expected_prior_digest"])
        except (RecoveryCoordinatorError, BudgetError) as exc:
            return CoordinatorResult("externally_blocked", str(exc))
        argv = self._resume_argv(escalation, launcher_argv)
        returncode = self.runner(argv)
        # The fresh process owns its own terminal result.  Its nonzero exit is
        # evidence of an external block, never a synthetic coordinator success.
        if returncode:
            return CoordinatorResult("externally_blocked", "fresh resume process failed", decision, tuple(argv), returncode)
        return CoordinatorResult("resumed", "fresh resume process started and exited successfully", decision, tuple(argv), returncode)

    def _target(self, escalation: Mapping[str, object]) -> tuple[str, str]:
        directive = _object(escalation["resume_directive_template"], "resume directive")
        frontier = directive.get("retry_frontier")
        if not isinstance(frontier, list) or len(frontier) != 1 or not isinstance(frontier[0], str) or not frontier[0]:
            raise RecoveryCoordinatorError("automatic recovery requires one explicit retry-frontier node")
        target = frontier[0]
        nodes = escalation["nodes"]
        matching = [node for node in nodes if isinstance(node, Mapping) and node.get("node_id") == target]
        if len(matching) != 1:
            raise RecoveryCoordinatorError("retry-frontier node is absent from escalation")
        target_run = [run for run in self.plan.runs if run.id == target]
        if len(target_run) != 1:
            raise RecoveryCoordinatorError("retry-frontier node is absent from registered plan")
        return target, self._classify(escalation, matching[0], target_run[0].criteria)

    def _classify(
        self, escalation: Mapping[str, object], node: Mapping[str, object],
        target_criteria: Sequence[str],
    ) -> str:
        """Derive a recovery class from evidence, with AC guidance in scope.

        The escalation's node classification is producer-supplied diagnostic
        data, not authority for the external coordinator.  A Tier-1 decision
        instead uses the target's observed failure text and verifies that the
        escalation's guidance is the registration-bound acceptance criteria
        for the target before treating the failure as recoverable.
        """
        guidance = _object(escalation["significance_guidance"], "significance guidance")
        if (not guidance or any(
                not isinstance(criterion, str) or not criterion
                or not isinstance(statement, str) or not statement
                for criterion, statement in guidance.items()
        )):
            raise RecoveryCoordinatorError("significance guidance must contain acceptance criteria")
        registered_guidance = self.plan.acceptance_criteria
        if dict(guidance) != dict(registered_guidance):
            raise RecoveryCoordinatorError(
                "significance guidance does not match the registered acceptance criteria"
            )
        if not target_criteria or any(
                criterion not in guidance
                or guidance[criterion] != registered_guidance[criterion]
                for criterion in target_criteria
        ):
            raise RecoveryCoordinatorError(
                "significance guidance does not cover the retry-frontier node"
            )
        reason = node.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = escalation.get("reason")
        if not isinstance(reason, str) or not reason:
            raise RecoveryCoordinatorError("retry-frontier node lacks failure evidence for classification")
        classification = classify_verification_failure({"stderr": reason})["classification"]
        if classification not in _CLASSIFICATIONS:
            raise RecoveryCoordinatorError("retry-frontier failure has unsupported classification")
        return classification

    def _ledger_state(self) -> Mapping[str, Any]:
        with self.ledger._locked(shared=True) as handle:
            state = dict(self.ledger._fold(handle))
            handle.seek(0)
            state["events"] = tuple(json.loads(line) for line in handle if line.strip())
            return state

    @staticmethod
    def _decision_count(state: Mapping[str, Any]) -> int:
        return sum(1 for event in state.get("events", ()) if event.get("event") == "recovery_decision")

    def _default_action(self, target: str, state: Mapping[str, Any]) -> str | None:
        attempted = {
            event["decision"].get("action")
            for event in state.get("events", ())
            if event.get("event") == "recovery_decision"
            and isinstance(event.get("decision"), Mapping)
            and event["decision"].get("target") == target
        }
        if "resume" in self.authority.allowed_actions and "resume" not in attempted:
            return "resume"
        if "extend_budget" in self.authority.allowed_actions and "extend_budget" not in attempted:
            return "extend_budget"
        return None

    def _decision(self, action: str, target: str, state: Mapping[str, Any]) -> dict[str, object]:
        prior = state.get("active_plan_sha256")
        if not isinstance(prior, str):
            raise RecoveryCoordinatorError("ledger has no active registered plan")
        payload: dict[str, object] = {}
        if action == "extend_budget":
            payload = {"launches": 1}
        return {"protocol": _DECISION_PROTOCOL, "action": action, "target": target,
                "expected_prior_digest": prior, "payload": payload}

    @staticmethod
    def _is_repeat(state: Mapping[str, Any], decision: Mapping[str, object]) -> bool:
        for event in state.get("events", ()):
            if event.get("event") == "recovery_decision" and event.get("decision") == decision:
                return True
        return False

    def _resume_argv(self, escalation: Mapping[str, object], launcher_argv: Sequence[str]) -> list[str]:
        directive = _object(escalation["resume_directive_template"], "resume directive")
        logical, predecessor, frontier = (directive.get("logical_graph_id"), directive.get("predecessor_attempt_id"), directive.get("retry_frontier"))
        if not all(isinstance(value, str) and value for value in (logical, predecessor)) or not isinstance(frontier, list):
            raise RecoveryCoordinatorError("resume directive is incomplete")
        graph_attempt_id = f"{predecessor}-recovery-{self._decision_count(self._ledger_state())}"
        script = Path(__file__).with_name("run_plan_graph.py")
        return [sys.executable, str(script), "run", "--repository", str(self.repository),
                "--registration", str(self._registration_path), "--graph-attempt-id", graph_attempt_id,
                "--launcher-command", *launcher_argv, "--run-root", str(self.run_root), "--resume",
                "--logical-graph-id", logical, "--predecessor-attempt-id", predecessor,
                "--retry-frontier", *frontier, "--blocker-evidence-ref", str(escalation.get("_reference", ""))]

    @property
    def _registration_path(self) -> Path:
        # Set by the command entry point; direct users should provide a runner
        # and call ``set_registration_path`` to make the fresh invocation explicit.
        path = getattr(self, "_registration_path_value", None)
        if not isinstance(path, Path):
            raise RecoveryCoordinatorError("registration path is required for fresh resume")
        return path

    def set_registration_path(self, path: Path) -> None:
        self._registration_path_value = path.resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("escalation", type=Path)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--launcher-command", nargs="+", required=True)
    parser.add_argument("--iteration-cap", type=int, default=3)
    parser.add_argument("--requested-action")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        escalation = dict(load_escalation(arguments.escalation))
        # The artifact reference is deliberately passed through to PlanGraph's
        # resume authority rather than trusting an arbitrary caller value.
        escalation["_reference"] = (
            "artifact:sha256:" + hashlib.sha256(arguments.escalation.read_bytes()).hexdigest()
        )
        coordinator = RecoveryCoordinator(repository=arguments.repository, registration=load_registration(arguments.registration), run_root=arguments.run_root, iteration_cap=arguments.iteration_cap)
        coordinator.set_registration_path(arguments.registration)
        result = coordinator.recover(escalation, launcher_argv=arguments.launcher_command, requested_action=arguments.requested_action)
    except (OSError, ValueError, RecoveryCoordinatorError) as exc:
        result = CoordinatorResult("externally_blocked", str(exc))
    print(json.dumps(result.as_mapping(), sort_keys=True))
    return 0 if result.status == "resumed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
