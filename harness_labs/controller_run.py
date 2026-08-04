"""Production-shaped entrypoint for one hybrid-controller run."""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent_sessions import (
    AgentSession,
    BackendCapabilities,
    FinalOutput,
    ModelRequest,
    ToolCall,
    ToolResult,
)
from .attempts import TaskAttempt, TaskResult
from .audit import AuditActor, AuditJournal
from .controller_coordinator import CoordinatorLoop
from .controller_evidence import EvidenceCatalog
from .controller_kernel import ControllerKernel, RunContract, RunLimits
from .controller_commands import CommandActor, KernelEvent
from .controller_projection import ControllerQueries, project_run_view
from .controller_scheduler import CapabilityScheduler, RoleProfile


SessionBuilder = Callable[[EvidenceCatalog], AgentSession]
ProfileBuilder = Callable[[EvidenceCatalog], tuple[RoleProfile, ...]]


@dataclass(frozen=True)
class ControllerRunResult:
    result: TaskResult
    run_view: Mapping[str, Any]
    manifest: Mapping[str, Any]
    run_dir: Path


def run_controller(
    contract: RunContract,
    *,
    session_builder: SessionBuilder,
    profile_builder: ProfileBuilder,
    run_dir: Path,
    max_tool_calls: int = 128,
    evidence_classification: str = "production_lifecycle",
) -> ControllerRunResult:
    """Execute one run through the real kernel, scheduler, session, and journal."""

    audit = AuditJournal(
        run_dir,
        contract.run_id,
        actor=AuditActor("kernel", "controller_kernel"),
        evidence_classification=evidence_classification,
    )
    evidence = EvidenceCatalog(audit=audit)
    kernel = ControllerKernel(contract, evidence=evidence, audit=audit)
    session = session_builder(evidence)
    profiles = profile_builder(evidence)
    scheduler = CapabilityScheduler(profiles)
    result = CoordinatorLoop(
        kernel,
        ControllerQueries(kernel, evidence),
        scheduler,
        session,
        max_tool_calls=max_tool_calls,
    ).run()
    _settle_nonterminal(kernel, result)
    view = project_run_view(kernel)
    terminal_status = (
        "succeeded"
        if result.status == "succeeded" and view["status"] == "succeeded"
        else "blocked"
        if result.status == "blocked"
        else "failed"
    )
    manifest = audit.finalize(
        terminal_status,
        result={
            "coordinator_result": {
                "attempt_id": result.attempt_id,
                "status": result.status,
                "payload": dict(result.payload),
                "evidence": list(result.evidence),
            },
            "run_view": view,
            "state_digest": kernel.state_digest(),
        },
        state={"controller": kernel.snapshot()},
    )
    return ControllerRunResult(
        result=result,
        run_view=view,
        manifest=manifest,
        run_dir=run_dir,
    )


def resume_controller(
    contract: RunContract,
    *,
    session_builder: SessionBuilder,
    profile_builder: ProfileBuilder,
    run_dir: Path,
    max_tool_calls: int = 128,
) -> ControllerRunResult:
    """Resume a nonterminal run from its durable checkpoint and evidence."""

    audit = AuditJournal.open_existing(
        run_dir,
        actor=AuditActor("kernel", "controller_kernel"),
    )
    checkpoint = json.loads(audit.checkpoint_path.read_text(encoding="utf-8"))
    if checkpoint.get("status") in {
        "succeeded",
        "failed",
        "blocked",
        "interrupted",
    }:
        raise ValueError("terminal controller run cannot be resumed")
    stored_state = checkpoint.get("state", {}).get("controller")
    if not isinstance(stored_state, Mapping):
        raise ValueError("run checkpoint has no controller state")
    state = copy.deepcopy(dict(stored_state))
    state.setdefault("receipts", {}).update(
        _load_receipt_state(audit.events_path)
    )
    evidence = EvidenceCatalog(audit=audit)
    _restore_evidence(evidence, run_dir, state)
    kernel = ControllerKernel.from_snapshot(
        contract,
        evidence=evidence,
        snapshot=state,
        audit=audit,
        events=_load_kernel_events(audit.events_path),
    )
    session = session_builder(evidence)
    scheduler = CapabilityScheduler(profile_builder(evidence))
    result = CoordinatorLoop(
        kernel,
        ControllerQueries(kernel, evidence),
        scheduler,
        session,
        max_tool_calls=max_tool_calls,
    ).run()
    _settle_nonterminal(kernel, result)
    view = project_run_view(kernel)
    terminal_status = (
        "succeeded"
        if result.status == "succeeded" and view["status"] == "succeeded"
        else "blocked"
        if result.status == "blocked"
        else "failed"
    )
    manifest = audit.finalize(
        terminal_status,
        result={
            "coordinator_result": {
                "attempt_id": result.attempt_id,
                "status": result.status,
                "payload": dict(result.payload),
                "evidence": list(result.evidence),
            },
            "run_view": view,
            "state_digest": kernel.state_digest(),
            "resumed": True,
        },
        state={"controller": kernel.snapshot()},
    )
    return ControllerRunResult(result, view, manifest, run_dir)


class _FixtureSession:
    capabilities = BackendCapabilities(True, True, True, True, True)

    def __init__(self, calls: list[dict[str, Any]], final: str) -> None:
        self._calls = calls
        self._final = final
        self._index = 0

    def open(self, request: ModelRequest) -> str:
        return "fixture-coordinator"

    def step(
        self,
        session_id: str,
        tool_result: ToolResult | None = None,
    ):
        if self._index < len(self._calls):
            item = self._calls[self._index]
            self._index += 1
            return ToolCall(
                f"fixture-call-{self._index}",
                item["name"],
                item.get("arguments", {}),
            )
        return FinalOutput(self._final, evidence=("fixture:coordinator",))

    def close(self, session_id: str) -> None:
        return None


class _FixtureExecutor:
    def __init__(self, task: dict[str, Any], results: Mapping[str, Any]) -> None:
        self._task = task
        self._results = results

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        result = self._results.get(self._task["id"])
        if not isinstance(result, Mapping):
            raise ValueError(f"fixture result is missing: {self._task['id']}")
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status=str(result.get("status", "succeeded")),  # type: ignore[arg-type]
            payload=copy.deepcopy(result.get("payload", {})),
            evidence=tuple(result.get("evidence", ())),
        )


def run_fixture_spec(spec: Mapping[str, Any], *, run_dir: Path) -> ControllerRunResult:
    """Run a JSON fixture through the same production-shaped entrypoint."""

    contract_value = spec.get("contract")
    if not isinstance(contract_value, Mapping):
        raise ValueError("fixture contract must be an object")
    limits_value = contract_value.get("limits", {})
    if not isinstance(limits_value, Mapping):
        raise ValueError("fixture limits must be an object")
    if "max_fan_out" in limits_value:
        raise ValueError(
            "fixture limit max_fan_out was renamed to max_subagents"
        )
    contract = RunContract(
        run_id=str(contract_value["run_id"]),
        objective=str(contract_value["objective"]),
        phases=tuple(contract_value.get("phases", ("active",))),
        criteria=tuple(contract_value.get("criteria", ())),
        terminal_artifact_kinds=tuple(
            contract_value.get("terminal_artifact_kinds", ())
        ),
        limits=RunLimits(
            max_depth=int(limits_value.get("max_depth", 2)),
            max_subagents=int(limits_value.get("max_subagents", 8)),
            max_parallelism=int(limits_value.get("max_parallelism", 4)),
            max_tasks=int(limits_value.get("max_tasks", 32)),
        ),
        repository=dict(contract_value.get("repository", {})),
    )
    prepared: dict[str, Any] = {}

    def prepare(evidence: EvidenceCatalog) -> dict[str, Any]:
        if prepared:
            return prepared
        aliases = {}
        for item in spec.get("artifacts", []):
            if not isinstance(item, Mapping):
                raise ValueError("fixture artifacts must be objects")
            record = evidence.add(
                kind=str(item["kind"]),
                content=item["content"],
                media_type=str(item.get("media_type", "text/plain")),
                producer_task_id=str(item["producer_task_id"]),
            )
            aliases[str(item["alias"])] = record
        prepared["aliases"] = aliases
        prepared["calls"] = _replace_artifact_aliases(
            copy.deepcopy(spec.get("coordinator_calls", [])),
            aliases,
        )
        prepared["results"] = _replace_artifact_aliases(
            copy.deepcopy(spec.get("task_results", {})),
            aliases,
        )
        return prepared

    def session_builder(evidence: EvidenceCatalog) -> AgentSession:
        values = prepare(evidence)
        return _FixtureSession(values["calls"], str(spec.get("final", "complete")))

    def profile_builder(evidence: EvidenceCatalog) -> tuple[RoleProfile, ...]:
        values = prepare(evidence)
        profiles = []
        for item in spec.get("profiles", []):
            if not isinstance(item, Mapping):
                raise ValueError("fixture profiles must be objects")
            profiles.append(
                RoleProfile(
                    profile_id=str(item["profile_id"]),
                    role=str(item["role"]),
                    capabilities=frozenset(item.get("capabilities", [])),
                    backend_id=str(item.get("backend_id", "fixture")),
                    executor_factory=lambda task, results=values["results"]: (
                        _FixtureExecutor(task, results)
                    ),
                )
            )
        return tuple(profiles)

    return run_controller(
        contract,
        session_builder=session_builder,
        profile_builder=profile_builder,
        run_dir=run_dir,
        evidence_classification="component",
    )


def _replace_artifact_aliases(value: Any, aliases: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$artifact:"):
        alias = value.removeprefix("$artifact:")
        try:
            return aliases[alias].ref
        except KeyError as exc:
            raise ValueError(f"unknown fixture artifact alias: {alias}") from exc
    if isinstance(value, list):
        return [_replace_artifact_aliases(item, aliases) for item in value]
    if isinstance(value, dict):
        replaced = {
            key: _replace_artifact_aliases(item, aliases)
            for key, item in value.items()
        }
        if replaced.get("$artifact_descriptor") is not None:
            alias = replaced.pop("$artifact_descriptor")
            if not isinstance(alias, str):
                raise ValueError("artifact descriptor alias must be a string")
            return aliases[alias].as_dict()
        return replaced
    return value


def _restore_evidence(
    evidence: EvidenceCatalog,
    run_dir: Path,
    state: Mapping[str, Any],
) -> None:
    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, Mapping):
        raise ValueError("controller artifact checkpoint is invalid")
    for record in artifacts.values():
        if not isinstance(record, Mapping):
            raise ValueError("controller artifact record is invalid")
        audit_path = record.get("audit_path")
        if not isinstance(audit_path, str) or not audit_path:
            raise ValueError("controller artifact lacks a durable audit path")
        path = (run_dir / audit_path).resolve()
        try:
            path.relative_to((run_dir / "artifacts").resolve())
        except ValueError as exc:
            raise ValueError("controller artifact path escapes the run") from exc
        evidence.restore(record, path.read_bytes())


def _load_kernel_events(events_path: Path) -> tuple[KernelEvent, ...]:
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        controller_event = value.get("payload", {}).get("controller_event")
        if not isinstance(controller_event, Mapping):
            continue
        actor = controller_event.get("actor")
        if not isinstance(actor, Mapping):
            raise ValueError("stored controller event actor is invalid")
        events.append(
            KernelEvent(
                event_id=str(controller_event["event_id"]),
                run_id=str(controller_event["run_id"]),
                revision=int(controller_event["revision"]),
                event_type=str(controller_event["event_type"]),
                actor=CommandActor(
                    str(actor["id"]),
                    str(actor["role"]),
                    (
                        str(actor["parent_id"])
                        if actor.get("parent_id") is not None
                        else None
                    ),
                ),
                command_id=str(controller_event["command_id"]),
                payload=dict(controller_event["payload"]),
            )
        )
    return tuple(events)


def _load_receipt_state(events_path: Path) -> dict[str, dict[str, Any]]:
    receipts = {}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if value.get("event_type") != "command_processed":
            continue
        payload = value.get("payload", {})
        command = payload.get("command")
        receipt = payload.get("receipt")
        if not isinstance(command, Mapping) or not isinstance(receipt, Mapping):
            raise ValueError("stored command receipt is invalid")
        if receipt.get("status") != "accepted":
            continue
        receipts[str(command["idempotency_key"])] = dict(receipt)
    return receipts


def _settle_nonterminal(kernel: ControllerKernel, result: TaskResult) -> None:
    state = kernel.snapshot()
    if state["status"] != "running" or result.status == "succeeded":
        return
    reason = result.payload.get("error") or result.payload.get("text")
    if not isinstance(reason, str) or not reason.strip():
        reason = f"coordinator terminated with status {result.status}"
    from .controller_commands import CommandEnvelope

    receipt = kernel.handle(
        CommandEnvelope(
            command_id=f"{kernel.contract.run_id}/terminal-settlement",
            run_id=kernel.contract.run_id,
            type="run.block_request",
            actor=CommandActor("coordinator-1", "run_coordinator"),
            expected_revision=kernel.revision,
            idempotency_key=f"{kernel.contract.run_id}/terminal-settlement",
            payload={"reason": reason},
        )
    )
    if not receipt.accepted:
        raise ValueError(f"controller terminal settlement failed: {receipt.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.fixture.read_text(encoding="utf-8"))
    if not isinstance(spec, Mapping):
        raise ValueError("fixture root must be an object")
    result = run_fixture_spec(spec, run_dir=args.run_dir)
    print(
        json.dumps(
            {
                "status": result.result.status,
                "run_status": result.run_view["status"],
                "run_dir": str(result.run_dir.resolve()),
                "manifest_hash": result.manifest["manifest_hash"],
            },
            sort_keys=True,
        )
    )
    return 0 if result.result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
