"""Deterministic state authority for the hybrid controller."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .attempts import TaskResult
from .audit import AuditActor, AuditJournal
from .controller_commands import (
    CommandActor,
    CommandEnvelope,
    CommandReceipt,
    KernelEvent,
)
from .controller_evidence import EvidenceCatalog, EvidenceError
from .controller_results import SemanticResultError, validate_semantic_result


COORDINATOR_COMMANDS = frozenset(
    {
        "criterion.propose",
        "task.dispatch",
        "decision.record",
        "finding.disposition",
        "phase.advance_request",
        "retry.request",
        "replan.request",
        "operator_input.request",
        "run.complete_request",
        "run.block_request",
    }
)
OPERATOR_COMMANDS = frozenset(
    {
        "run.pause",
        "run.resume",
        "run.cancel",
        "budget.change",
        "permission.grant",
        "permission.revoke",
        "decision.approve",
        "decision.reject",
    }
)
DISPATCHER_COMMANDS = frozenset(
    {
        "coordinator.schema_register",
        "coordinator.session_start",
        "coordinator.session_end",
        "run.block_request",
    }
)


class KernelError(RuntimeError):
    """Raised when trusted controller integration violates kernel invariants."""


@dataclass(frozen=True)
class RunLimits:
    max_depth: int = 5
    max_subagents: int = 5
    max_parallelism: int | None = None
    max_tasks: int | None = None

    def __post_init__(self) -> None:
        if self.max_depth < 1 or self.max_subagents < 1:
            raise ValueError("depth and subagent limits must be positive")
        for name in ("max_parallelism", "max_tasks"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ValueError(f"{name} must be positive or unbounded")

    def as_dict(self) -> dict[str, int | None]:
        return {
            "max_depth": self.max_depth,
            "max_subagents": self.max_subagents,
            "max_parallelism": self.max_parallelism,
            "max_tasks": self.max_tasks,
        }


@dataclass(frozen=True)
class RunContract:
    run_id: str
    objective: str
    phases: tuple[str, ...] = ("orient", "plan", "review", "report")
    criteria: tuple[Mapping[str, Any], ...] = ()
    terminal_artifact_kinds: tuple[str, ...] = ()
    limits: RunLimits = field(default_factory=RunLimits)
    repository: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.objective.strip():
            raise ValueError("run identity and objective must be non-empty")
        if not self.phases or not all(
            isinstance(phase, str) and phase.strip() for phase in self.phases
        ):
            raise ValueError("run phases must be non-empty strings")
        if len(set(self.phases)) != len(self.phases):
            raise ValueError("run phases must be unique")
        if not all(
            isinstance(kind, str) and kind.strip()
            for kind in self.terminal_artifact_kinds
        ):
            raise ValueError("terminal artifact kinds must be non-empty")


class ControllerKernel:
    """Process typed commands and own the authoritative run state."""

    def __init__(
        self,
        contract: RunContract,
        *,
        evidence: EvidenceCatalog,
        audit: AuditJournal | None = None,
        initial_artifacts: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self.contract = contract
        self.evidence = evidence
        self.audit = audit
        self._mutex = threading.RLock()
        self._events: list[KernelEvent] = []
        self._receipts: dict[str, CommandReceipt] = {}
        criteria: dict[str, dict[str, Any]] = {}
        for item in contract.criteria:
            criterion = _criterion(item)
            if criterion["id"] in criteria:
                raise ValueError(f"duplicate run criterion: {criterion['id']}")
            criteria[criterion["id"]] = criterion
        artifacts: dict[str, dict[str, Any]] = {}
        for item in initial_artifacts:
            ref = str(item.get("ref", ""))
            if not ref or not evidence.contains(ref):
                raise KernelError("initial artifact references unknown evidence")
            record = evidence.metadata(ref).as_dict()
            if dict(item) != record:
                raise KernelError("initial artifact metadata does not match evidence")
            artifacts[ref] = record
        self._state: dict[str, Any] = {
            "protocol": "controller-state/1",
            "run_id": contract.run_id,
            "objective": contract.objective,
            "repository": dict(contract.repository),
            "revision": 0,
            "kernel_event_count": 0,
            "status": "running",
            "phase": contract.phases[0],
            "phases": list(contract.phases),
            "limits": contract.limits.as_dict(),
            "criteria": criteria,
            "tasks": {},
            "decisions": {},
            "findings": {},
            "artifacts": artifacts,
            "operator_questions": [],
            "anomalies": [],
            "budgets": {},
            "receipts": {},
            "coordinator_dispatch": {
                "schema": None,
                "active_session": None,
                "sessions": [],
            },
            "rejected_task_dispatch_refs": [],
        }
        if audit is not None:
            audit.append(
                "controller_initialized",
                status="started",
                payload={
                    "objective": contract.objective,
                    "phases": list(contract.phases),
                    "limits": contract.limits.as_dict(),
                },
                actor=AuditActor("kernel", "controller_kernel"),
            )
            audit.merge_checkpoint(updates={"controller": self.snapshot()})

    @classmethod
    def from_snapshot(
        cls,
        contract: RunContract,
        *,
        evidence: EvidenceCatalog,
        snapshot: Mapping[str, Any],
        audit: AuditJournal | None = None,
        events: Iterable[KernelEvent] = (),
    ) -> ControllerKernel:
        """Resume authoritative state without interpreting prior chat history."""

        state = copy.deepcopy(dict(snapshot))
        if state.get("protocol") != "controller-state/1":
            raise KernelError("controller snapshot protocol is invalid")
        if state.get("run_id") != contract.run_id:
            raise KernelError("controller snapshot run identity does not match")
        if state.get("objective") != contract.objective:
            raise KernelError("controller snapshot objective does not match")
        if state.get("phases") != list(contract.phases):
            raise KernelError("controller snapshot phases do not match")
        if state.get("repository") != dict(contract.repository):
            raise KernelError("controller snapshot repository does not match")
        if not isinstance(state.get("revision"), int):
            raise KernelError("controller snapshot revision is invalid")
        instance = cls.__new__(cls)
        instance.contract = contract
        instance.evidence = evidence
        instance.audit = audit
        instance._mutex = threading.RLock()
        stored_events = list(events)
        checkpoint_event_count = state.get("kernel_event_count", 0)
        if (
            not isinstance(checkpoint_event_count, int)
            or checkpoint_event_count < 0
            or checkpoint_event_count > len(stored_events)
        ):
            raise KernelError("controller snapshot event count is invalid")
        instance._events = stored_events[:checkpoint_event_count]
        state.setdefault(
            "coordinator_dispatch",
            {"schema": None, "active_session": None, "sessions": []},
        )
        state.setdefault("rejected_task_dispatch_refs", [])
        instance._state = state
        instance._receipts = {}
        for key, value in state.get("receipts", {}).items():
            if not isinstance(value, Mapping):
                raise KernelError("controller snapshot receipt is invalid")
            instance._receipts[str(key)] = CommandReceipt(
                command_id=str(value["command_id"]),
                run_id=str(value["run_id"]),
                status="accepted",
                revision=int(value["revision"]),
                event_ids=tuple(value.get("event_ids", ())),
                effect_refs=tuple(value.get("effect_refs", ())),
                error_code=value.get("error_code"),
                message=str(value.get("message", "")),
            )
        for event in stored_events[checkpoint_event_count:]:
            instance._apply(event)
            instance._state["revision"] = max(
                int(instance._state["revision"]),
                event.revision,
            )
        return instance

    @property
    def revision(self) -> int:
        with self._mutex:
            return int(self._state["revision"])

    @property
    def events(self) -> tuple[KernelEvent, ...]:
        with self._mutex:
            return tuple(self._events)

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            return copy.deepcopy(self._state)

    def state_digest(self) -> str:
        encoded = json.dumps(
            self.snapshot(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def handle(self, command: CommandEnvelope) -> CommandReceipt:
        with self._mutex:
            duplicate = self._receipts.get(command.idempotency_key)
            if duplicate is not None:
                return CommandReceipt(
                    command_id=command.command_id,
                    run_id=command.run_id,
                    status="duplicate",
                    revision=duplicate.revision,
                    event_ids=duplicate.event_ids,
                    effect_refs=duplicate.effect_refs,
                    message="idempotent command already accepted",
                )
            rejection = self._validate_envelope(command)
            if rejection is not None:
                return self._reject(command, *rejection)
            try:
                effects, refs = self._evaluate(command)
            except (ValueError, EvidenceError) as exc:
                return self._reject(command, "invalid_command", str(exc))
            return self._commit(command, effects, refs)

    def record_task_results(
        self,
        results: Iterable[tuple[str, TaskResult]],
    ) -> tuple[KernelEvent, ...]:
        """Promote validated executor results through one kernel transaction."""

        with self._mutex:
            prepared: list[tuple[str, TaskResult, Any]] = []
            for task_id, result in results:
                task = self._state["tasks"].get(task_id)
                if task is None:
                    raise KernelError(f"unknown task result target: {task_id}")
                if task["status"] != "running":
                    raise KernelError(f"task is not running: {task_id}")
                if result.attempt_id != task["attempt_id"]:
                    raise KernelError(f"task result attempt mismatch: {task_id}")
                if result.status == "succeeded":
                    try:
                        semantic = validate_semantic_result(
                            result,
                            expected_details_schema=task["details_schema"],
                        )
                    except SemanticResultError as exc:
                        result = TaskResult(
                            attempt_id=result.attempt_id,
                            status="failed",
                            payload={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                            evidence=result.evidence,
                        )
                        semantic = None
                else:
                    semantic = None
                promotions = None
                if semantic is not None:
                    self._validate_result_references(task_id, semantic)
                    promotions = self._semantic_promotions(task_id, semantic)
                prepared.append((task_id, result, promotions))

            revision = int(self._state["revision"]) + 1
            actor = CommandActor("kernel", "controller_kernel")
            events: list[KernelEvent] = []
            for task_id, result, promotions in prepared:
                payload = {
                    "task_id": task_id,
                    "attempt_id": result.attempt_id,
                    "status": result.status,
                    "result": dict(result.payload),
                    "evidence": list(result.evidence),
                    "promotions": promotions,
                }
                event = self._new_event(
                    revision=revision,
                    event_type="task.result_recorded",
                    actor=actor,
                    command_id=f"system-result:{task_id}:{revision}",
                    payload=payload,
                )
                self._apply(event)
                events.append(event)
            self._state["revision"] = revision
            self._persist(events)
            return tuple(events)

    def mark_tasks_running(self, task_ids: Iterable[str]) -> tuple[KernelEvent, ...]:
        """Reserve ready tasks before executor launch."""

        with self._mutex:
            ids = tuple(task_ids)
            if not ids:
                raise KernelError("at least one task must be marked running")
            for task_id in ids:
                task = self._state["tasks"].get(task_id)
                if task is None or task["status"] != "ready":
                    raise KernelError(f"task is not ready: {task_id}")
            revision = int(self._state["revision"]) + 1
            actor = CommandActor("kernel", "controller_kernel")
            events = []
            for task_id in ids:
                event = self._new_event(
                    revision=revision,
                    event_type="task.started",
                    actor=actor,
                    command_id=f"system-start:{task_id}:{revision}",
                    payload={"task_id": task_id},
                )
                self._apply(event)
                events.append(event)
            self._state["revision"] = revision
            self._persist(events)
            return tuple(events)

    def task(self, task_id: str) -> dict[str, Any]:
        with self._mutex:
            task = self._state["tasks"].get(task_id)
            if task is None:
                raise KernelError(f"unknown task: {task_id}")
            return copy.deepcopy(task)

    def _validate_envelope(
        self,
        command: CommandEnvelope,
    ) -> tuple[str, str] | None:
        if command.run_id != self.contract.run_id:
            return "run_mismatch", "command run_id does not match the kernel"
        if command.expected_revision != self._state["revision"]:
            return "stale_revision", "command expected_revision is stale"
        if (
            self._state["status"] in {"succeeded", "failed", "cancelled"}
            and command.type != "coordinator.session_end"
        ):
            return "terminal_run", "terminal run cannot accept commands"
        if command.actor.role == "operator":
            allowed = OPERATOR_COMMANDS
        elif command.actor.role == "run_coordinator":
            allowed = COORDINATOR_COMMANDS
        elif command.actor.role == "dispatcher":
            allowed = DISPATCHER_COMMANDS
        elif command.actor.role == "worker":
            allowed = frozenset({"task.dispatch", "operator_input.request"})
        else:
            return "unauthorized_actor", "actor role may not issue commands"
        if command.type not in allowed:
            return "unauthorized_command", "actor may not issue this command"
        for ref in command.provenance.evidence_refs:
            if self.evidence.contains(ref):
                continue
            if ref in self._state["rejected_task_dispatch_refs"]:
                continue
            return "unknown_evidence", f"unknown provenance reference: {ref}"
        return None

    def _evaluate(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        handler = {
            "criterion.propose": self._criterion_propose,
            "task.dispatch": self._task_dispatch,
            "decision.record": self._decision_record,
            "finding.disposition": self._finding_disposition,
            "phase.advance_request": self._phase_advance,
            "retry.request": self._recovery_request,
            "replan.request": self._recovery_request,
            "operator_input.request": self._operator_input_request,
            "run.complete_request": self._run_complete,
            "run.block_request": self._run_block,
            "run.pause": self._run_pause,
            "run.resume": self._run_resume,
            "run.cancel": self._run_cancel,
            "budget.change": self._budget_change,
            "permission.grant": self._administrative_record,
            "permission.revoke": self._administrative_record,
            "decision.approve": self._administrative_record,
            "decision.reject": self._administrative_record,
            "coordinator.schema_register": self._coordinator_schema_register,
            "coordinator.session_start": self._coordinator_session_start,
            "coordinator.session_end": self._coordinator_session_end,
        }[command.type]
        return handler(command)

    def _coordinator_schema_register(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        protocol = _payload_text(
            command.payload,
            "protocol",
            "coordinator schema",
        )
        if protocol != "coordinator-dispatch-schema/1":
            raise ValueError("coordinator schema protocol is invalid")
        schema_id = _payload_text(
            command.payload,
            "schema_id",
            "coordinator schema",
        )
        digest = _payload_text(
            command.payload,
            "sha256",
            "coordinator schema",
        )
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("coordinator schema sha256 is invalid")
        phases = _string_list(command.payload, "phases")
        if phases != list(self.contract.phases):
            raise ValueError("coordinator schema phases do not match the run")
        segments = command.payload.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("coordinator schema segments must be non-empty")
        covered: list[str] = []
        normalized = []
        seen_ids: set[str] = set()
        for item in segments:
            if not isinstance(item, Mapping):
                raise ValueError("coordinator schema segments must be objects")
            segment_id = _payload_text(item, "id", "coordinator segment")
            if segment_id in seen_ids:
                raise ValueError("coordinator segment ids must be unique")
            seen_ids.add(segment_id)
            segment_phases = _string_list(item, "phases")
            max_attempts = item.get("max_attempts")
            if max_attempts is not None and (
                not isinstance(max_attempts, int)
                or isinstance(max_attempts, bool)
                or max_attempts < 1
            ):
                raise ValueError(
                    "coordinator segment max_attempts must be positive or unbounded"
                )
            covered.extend(segment_phases)
            normalized.append(
                {
                    "id": segment_id,
                    "phases": segment_phases,
                    "max_attempts": max_attempts,
                }
            )
        if covered != list(self.contract.phases):
            raise ValueError(
                "coordinator segments must exactly cover run phases in order"
            )
        existing = self._state["coordinator_dispatch"]["schema"]
        if existing is not None and existing.get("sha256") != digest:
            raise ValueError("a different coordinator schema is already registered")
        schema = {
            "protocol": protocol,
            "schema_id": schema_id,
            "sha256": digest,
            "phases": phases,
            "segments": normalized,
        }
        return [("coordinator.schema_registered", {"schema": schema})], (
            f"coordinator-schema:{schema_id}",
        )

    def _coordinator_session_start(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        dispatch = self._state["coordinator_dispatch"]
        if dispatch["schema"] is None:
            raise ValueError("coordinator schema is not registered")
        if dispatch["active_session"] is not None:
            raise ValueError("a coordinator session is already active")
        session_id = _payload_text(
            command.payload,
            "session_id",
            "coordinator session",
        )
        segment_id = _payload_text(
            command.payload,
            "segment_id",
            "coordinator session",
        )
        attempt = command.payload.get("attempt")
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
        ):
            raise ValueError("coordinator session attempt must be positive")
        segment = next(
            (
                item
                for item in dispatch["schema"]["segments"]
                if item["id"] == segment_id
            ),
            None,
        )
        if segment is None:
            raise ValueError("coordinator session segment is unknown")
        if self._state["phase"] not in segment["phases"]:
            raise ValueError("coordinator segment does not own the current phase")
        prior_attempts = sum(
            1
            for item in dispatch["sessions"]
            if item["segment_id"] == segment_id
        )
        if attempt != prior_attempts + 1:
            raise ValueError("coordinator session attempt is not sequential")
        if (
            segment["max_attempts"] is not None
            and attempt > segment["max_attempts"]
        ):
            raise ValueError("coordinator session exceeds segment attempts")
        value = {
            "session_id": session_id,
            "segment_id": segment_id,
            "attempt": attempt,
            "starting_phase": self._state["phase"],
            "backend_id": str(command.payload.get("backend_id", "unspecified")),
        }
        return [("coordinator.session_started", value)], (
            f"coordinator-session:{session_id}",
        )

    def _coordinator_session_end(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        dispatch = self._state["coordinator_dispatch"]
        active = dispatch["active_session"]
        if not isinstance(active, Mapping):
            raise ValueError("no coordinator session is active")
        session_id = _payload_text(
            command.payload,
            "session_id",
            "coordinator session",
        )
        if session_id != active["session_id"]:
            raise ValueError("coordinator session identity does not match")
        outcome = _payload_text(
            command.payload,
            "outcome",
            "coordinator session",
        )
        if outcome not in {
            "boundary",
            "terminal",
            "recoverable_failure",
            "blocked",
            "interrupted",
        }:
            raise ValueError("coordinator session outcome is invalid")
        value = {
            **copy.deepcopy(dict(active)),
            "outcome": outcome,
            "ending_phase": self._state["phase"],
            "run_status": self._state["status"],
            "result_status": str(command.payload.get("result_status", "")),
            "reason": str(command.payload.get("reason", "")),
        }
        return [("coordinator.session_ended", value)], (
            f"coordinator-session:{session_id}",
        )

    def _criterion_propose(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        criterion = _criterion(command.payload)
        if criterion["id"] in self._state["criteria"]:
            raise ValueError(f"criterion already exists: {criterion['id']}")
        return [("criterion.registered", {"criterion": criterion})], (
            f"criterion:{criterion['id']}",
        )

    def _task_dispatch(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        tasks = command.payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("task.dispatch requires a non-empty tasks list")
        max_parallelism = command.payload.get("max_parallelism", 1)
        if not isinstance(max_parallelism, int) or max_parallelism < 1:
            raise ValueError("task.dispatch max_parallelism must be positive")
        if (
            self.contract.limits.max_parallelism is not None
            and max_parallelism > self.contract.limits.max_parallelism
        ):
            raise ValueError("task.dispatch exceeds max_parallelism")
        if len(tasks) > self.contract.limits.max_subagents:
            raise ValueError("task.dispatch exceeds max_subagents")
        if (
            self.contract.limits.max_tasks is not None
            and len(self._state["tasks"]) + len(tasks)
            > self.contract.limits.max_tasks
        ):
            raise ValueError("task.dispatch exceeds max_tasks")
        normalized: list[dict[str, Any]] = []
        supplied_ids: set[str] = set()
        sibling_counts: dict[str, int] = {}
        for item in tasks:
            if not isinstance(item, Mapping):
                raise ValueError("task.dispatch tasks must be objects")
            task = _task(item)
            task["acceptance_criteria"] = _resolve_task_criteria(
                task["acceptance_criteria"], self._state["criteria"]
            )
            task_id = task["id"]
            if task_id in supplied_ids or task_id in self._state["tasks"]:
                raise ValueError(f"duplicate task id: {task_id}")
            supplied_ids.add(task_id)
            supersedes_task_id = task["supersedes_task_id"]
            if supersedes_task_id is not None:
                superseded = self._state["tasks"].get(supersedes_task_id)
                if superseded is None:
                    raise ValueError(
                        f"unknown superseded task: {supersedes_task_id}"
                    )
                if superseded["status"] != "failed":
                    raise ValueError(
                        f"superseded task is not failed: {supersedes_task_id}"
                    )
                frozen_fields = (
                    "role",
                    "details_schema",
                    "acceptance_criteria",
                    "dependencies",
                    "parent_task_id",
                    "optional",
                    "may_delegate",
                )
                for field in frozen_fields:
                    if task[field] != superseded[field]:
                        raise ValueError(
                            "superseding task changes frozen authority: "
                            f"{field}"
                        )
                if not set(task["required_capabilities"]).issubset(
                    set(superseded["required_capabilities"])
                ):
                    raise ValueError(
                        "superseding task changes frozen authority: "
                        "required_capabilities"
                    )
            parent_id = task["parent_task_id"]
            depth = 1
            if parent_id is not None:
                parent = self._state["tasks"].get(parent_id)
                if parent is None:
                    raise ValueError(f"unknown parent task: {parent_id}")
                if not parent["may_delegate"]:
                    raise ValueError(f"parent task may not delegate: {parent_id}")
                if (
                    command.actor.role == "worker"
                    and command.actor.id != parent_id
                ):
                    raise ValueError("worker may delegate only from its own task")
                if (
                    command.actor.role == "worker"
                    and parent["status"] != "running"
                ):
                    raise ValueError("worker may delegate only while running")
                depth = int(parent["depth"]) + 1
            elif command.actor.role == "worker":
                raise ValueError("worker delegation requires parent_task_id")
            if depth > self.contract.limits.max_depth:
                raise ValueError("task.dispatch exceeds max_depth")
            sibling_key = parent_id or "<root>"
            sibling_counts[sibling_key] = sibling_counts.get(sibling_key, 0) + 1
            existing = sum(
                1
                for prior in self._state["tasks"].values()
                if (prior["parent_task_id"] or "<root>") == sibling_key
            )
            if (
                existing + sibling_counts[sibling_key]
                > self.contract.limits.max_subagents
            ):
                raise ValueError("task.dispatch exceeds parent max_subagents")
            for dependency in task["dependencies"]:
                dependency_task = self._state["tasks"].get(dependency)
                if dependency_task is None or dependency_task["status"] != "succeeded":
                    raise ValueError(
                        f"task dependency is not satisfied: {dependency}"
                    )
            for criterion_id in task["acceptance_criteria"]:
                if criterion_id not in self._state["criteria"]:
                    raise ValueError(f"unknown task criterion: {criterion_id}")
            task["depth"] = depth
            task["status"] = "ready"
            task["attempt_id"] = f"{task_id}/attempt-1"
            task["max_parallelism"] = max_parallelism
            normalized.append(task)
        effects = [("task.registered", {"task": task}) for task in normalized]
        return effects, tuple(f"task:{task['id']}" for task in normalized)

    def _decision_record(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        decision_id = _payload_text(command.payload, "id", "decision")
        if decision_id in self._state["decisions"]:
            raise ValueError(f"decision already exists: {decision_id}")
        decision = {
            "id": decision_id,
            "question": _payload_text(command.payload, "question", "decision"),
            "choice": _payload_text(command.payload, "choice", "decision"),
            "alternatives": _string_list(command.payload, "alternatives"),
            "rationale": _payload_text(command.payload, "rationale", "decision"),
            "evidence_refs": list(command.provenance.evidence_refs),
            "actor": command.actor.as_dict(),
        }
        return [("decision.recorded", {"decision": decision})], (
            f"decision:{decision_id}",
        )

    def _finding_disposition(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        finding_id = _payload_text(command.payload, "finding_id", "disposition")
        finding = self._state["findings"].get(finding_id)
        if finding is None:
            raise ValueError(f"unknown finding: {finding_id}")
        disposition = _payload_text(command.payload, "disposition", "disposition")
        if disposition not in {
            "resolved",
            "rejected",
            "duplicate",
            "deferred",
            "accepted",
        }:
            raise ValueError("finding disposition is invalid")
        rationale = _payload_text(command.payload, "rationale", "disposition")
        resolution_refs = _string_list(command.payload, "resolution_refs")
        for ref in resolution_refs:
            if not self.evidence.contains(ref):
                raise ValueError(f"unknown resolution reference: {ref}")
        if disposition == "resolved" and not resolution_refs:
            raise ValueError("resolved finding requires resolution evidence")
        value = {
            "finding_id": finding_id,
            "disposition": disposition,
            "rationale": rationale,
            "resolution_refs": resolution_refs,
            "actor": command.actor.as_dict(),
        }
        return [("finding.disposition_recorded", value)], (f"finding:{finding_id}",)

    def _phase_advance(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        target = _payload_text(command.payload, "target", "phase request")
        phases = self._state["phases"]
        current_index = phases.index(self._state["phase"])
        if current_index + 1 >= len(phases) or phases[current_index + 1] != target:
            raise ValueError("phase request must target the next declared phase")
        active = [
            task["id"]
            for task in self._state["tasks"].values()
            if task["status"] in {"ready", "running"}
        ]
        if active:
            raise ValueError("phase cannot advance while tasks are active")
        return [(
            "phase.advanced",
            {"prior": self._state["phase"], "next": target},
        )], (f"phase:{target}",)

    def _recovery_request(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        reason = _payload_text(command.payload, "reason", "recovery request")
        return [(
            command.type,
            {"reason": reason, "payload": dict(command.payload)},
        )], ()

    def _operator_input_request(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        question = _payload_text(command.payload, "question", "operator request")
        question_id = _payload_text(command.payload, "id", "operator request")
        return [(
            "operator_input.requested",
            {"id": question_id, "question": question},
        )], (f"operator-question:{question_id}",)

    def _run_complete(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        failures = self.completion_failures()
        if failures:
            raise ValueError("completion gates failed: " + "; ".join(failures))
        return [("run.completed", {"status": "succeeded"})], (
            f"run:{self.contract.run_id}",
        )

    def _run_block(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        reason = _payload_text(command.payload, "reason", "block request")
        return [("run.blocked", {"reason": reason})], (f"run:{self.contract.run_id}",)

    def _run_pause(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        if self._state["status"] != "running":
            raise ValueError("only a running run may be paused")
        return [("run.paused", {"reason": command.payload.get("reason", "")})], ()

    def _run_resume(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        if self._state["status"] not in {"paused", "blocked"}:
            raise ValueError("only a paused or blocked run may be resumed")
        return [("run.resumed", {"reason": command.payload.get("reason", "")})], ()

    def _run_cancel(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        return [("run.cancelled", {"reason": command.payload.get("reason", "")})], ()

    def _budget_change(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        name = _payload_text(command.payload, "name", "budget change")
        value = command.payload.get("value")
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError("budget value must be a non-negative number")
        return [("budget.changed", {"name": name, "value": value})], ()

    def _administrative_record(
        self,
        command: CommandEnvelope,
    ) -> tuple[list[tuple[str, dict[str, Any]]], tuple[str, ...]]:
        return [(command.type, {"payload": dict(command.payload)})], ()

    def completion_failures(self) -> tuple[str, ...]:
        with self._mutex:
            failures = []
            for criterion in self._state["criteria"].values():
                if criterion["status"] != "satisfied":
                    failures.append(f"criterion {criterion['id']} is not satisfied")
            artifact_kinds = {
                artifact["kind"] for artifact in self._state["artifacts"].values()
            }
            for kind in self.contract.terminal_artifact_kinds:
                if kind not in artifact_kinds:
                    failures.append(f"terminal artifact is missing: {kind}")
            superseded_task_ids = {
                task["supersedes_task_id"]
                for task in self._state["tasks"].values()
                if task["status"] == "succeeded"
                and task.get("supersedes_task_id") is not None
            }
            for task in self._state["tasks"].values():
                if task["id"] in superseded_task_ids:
                    continue
                if task["status"] in {"ready", "running"}:
                    failures.append(f"task is still active: {task['id']}")
                elif task["status"] != "succeeded" and not task["optional"]:
                    failures.append(f"required task did not succeed: {task['id']}")
            for finding in self._state["findings"].values():
                if not finding.get("requires_disposition", False):
                    continue
                disposition = finding.get("disposition")
                if disposition is None or disposition["disposition"] == "accepted":
                    failures.append(f"finding is unresolved: {finding['id']}")
                if (
                    finding["severity"] == "critical"
                    and disposition is not None
                    and disposition["disposition"] == "deferred"
                ):
                    failures.append(f"critical finding is deferred: {finding['id']}")
            return tuple(failures)

    def _validate_result_references(self, task_id: str, semantic: Any) -> None:
        references: list[str] = []
        for claim in semantic.claims:
            references.extend(claim.get("evidence_refs", []))
        for finding in semantic.findings:
            references.extend(finding.get("evidence_refs", []))
            for source_id in finding.get("source_finding_ids", []):
                if source_id not in self._state["findings"]:
                    raise KernelError(
                        f"finding references unknown source finding: {source_id}"
                    )
        for item in semantic.criterion_coverage:
            references.extend(item.get("evidence_refs", []))
        for ref in references:
            if not self.evidence.contains(ref):
                raise KernelError(f"semantic result references unknown evidence: {ref}")
        for artifact in semantic.artifacts:
            record = self.evidence.metadata(artifact["ref"])
            if record.sha256 != artifact["sha256"]:
                raise KernelError("semantic result artifact digest does not match")
            if record.kind != artifact["kind"]:
                raise KernelError("semantic result artifact kind does not match")
            if record.producer_task_id != task_id:
                raise KernelError("semantic result artifact producer does not match")
        task = self._state["tasks"][task_id]
        for finding in semantic.findings:
            global_id = f"{task_id}/{finding['id']}"
            if global_id in self._state["findings"]:
                raise KernelError(f"duplicate promoted finding: {global_id}")
        for coverage in semantic.criterion_coverage:
            criterion = self._state["criteria"].get(coverage["criterion_id"])
            if criterion is None:
                raise KernelError(
                    f"result covers unknown criterion: {coverage['criterion_id']}"
                )
            if coverage["criterion_id"] not in task["acceptance_criteria"]:
                raise KernelError(
                    f"task was not assigned criterion: {coverage['criterion_id']}"
                )

    def _semantic_promotions(self, task_id: str, semantic: Any) -> dict[str, Any]:
        return {
            "summary": semantic.summary,
            "artifacts": [
                {**dict(artifact), "producer_task_id": task_id}
                for artifact in semantic.artifacts
            ],
            "findings": [
                {
                    **dict(finding),
                    "id": f"{task_id}/{finding['id']}",
                    "local_id": finding["id"],
                    "producer_task_id": task_id,
                    "disposition": None,
                }
                for finding in semantic.findings
            ],
            "criterion_coverage": [
                dict(coverage) for coverage in semantic.criterion_coverage
            ],
        }

    def _apply_promotions(
        self,
        task_id: str,
        promotions: Mapping[str, Any],
    ) -> None:
        task = self._state["tasks"][task_id]
        task["summary"] = promotions["summary"]
        for artifact in promotions["artifacts"]:
            self._state["artifacts"][artifact["ref"]] = copy.deepcopy(artifact)
        for finding in promotions["findings"]:
            self._state["findings"][finding["id"]] = copy.deepcopy(finding)
        for coverage in promotions["criterion_coverage"]:
            criterion = self._state["criteria"][coverage["criterion_id"]]
            if coverage["status"] == "satisfied":
                if task_id not in criterion["satisfied_by"]:
                    criterion["satisfied_by"].append(task_id)
                criterion["evidence_refs"] = sorted(
                    set(criterion["evidence_refs"]).union(
                        coverage["evidence_refs"]
                    )
                )
                if len(criterion["satisfied_by"]) >= criterion["minimum_satisfiers"]:
                    criterion["status"] = "satisfied"

    def _commit(
        self,
        command: CommandEnvelope,
        effects: list[tuple[str, dict[str, Any]]],
        refs: tuple[str, ...],
    ) -> CommandReceipt:
        revision = int(self._state["revision"]) + 1
        events = []
        for event_type, payload in effects:
            event = self._new_event(
                revision=revision,
                event_type=event_type,
                actor=command.actor,
                command_id=command.command_id,
                payload=payload,
            )
            self._apply(event)
            events.append(event)
        self._state["revision"] = revision
        receipt = CommandReceipt(
            command_id=command.command_id,
            run_id=command.run_id,
            status="accepted",
            revision=revision,
            event_ids=tuple(event.event_id for event in events),
            effect_refs=refs,
        )
        self._receipts[command.idempotency_key] = receipt
        self._state["receipts"][command.idempotency_key] = receipt.as_dict()
        self._persist(events, command=command, receipt=receipt)
        return receipt

    def _reject(
        self,
        command: CommandEnvelope,
        code: str,
        message: str,
    ) -> CommandReceipt:
        receipt = CommandReceipt(
            command_id=command.command_id,
            run_id=command.run_id,
            status="rejected",
            revision=int(self._state["revision"]),
            error_code=code,
            message=message,
        )
        if self.audit is not None:
            self.audit.append(
                "command_rejected",
                status="failed",
                payload={
                    "command": command.as_dict(),
                    "receipt": receipt.as_dict(),
                },
                actor=AuditActor(
                    command.actor.id,
                    command.actor.role,
                    command.actor.parent_id,
                ),
            )
        if code == "invalid_command" and command.type == "task.dispatch":
            self._state["rejected_task_dispatch_refs"].append(
                f"command:{command.command_id}"
            )
            if self.audit is not None:
                self.audit.merge_checkpoint(updates={"controller": self.snapshot()})
        return receipt

    def _new_event(
        self,
        *,
        revision: int,
        event_type: str,
        actor: CommandActor,
        command_id: str,
        payload: Mapping[str, Any],
    ) -> KernelEvent:
        return KernelEvent(
            event_id=f"controller-event-{len(self._events) + 1:06d}",
            run_id=self.contract.run_id,
            revision=revision,
            event_type=event_type,
            actor=actor,
            command_id=command_id,
            payload=dict(payload),
        )

    def _apply(self, event: KernelEvent) -> None:
        event_type = event.event_type
        payload = event.payload
        if event_type == "criterion.registered":
            criterion = copy.deepcopy(payload["criterion"])
            self._state["criteria"][criterion["id"]] = criterion
        elif event_type == "task.registered":
            task = copy.deepcopy(payload["task"])
            self._state["tasks"][task["id"]] = task
        elif event_type == "task.started":
            self._state["tasks"][payload["task_id"]]["status"] = "running"
        elif event_type == "task.result_recorded":
            task = self._state["tasks"][payload["task_id"]]
            task["status"] = payload["status"]
            task["result"] = copy.deepcopy(payload["result"])
            task["evidence"] = list(payload["evidence"])
            promotions = payload.get("promotions")
            if isinstance(promotions, Mapping):
                self._apply_promotions(payload["task_id"], promotions)
        elif event_type == "decision.recorded":
            decision = copy.deepcopy(payload["decision"])
            self._state["decisions"][decision["id"]] = decision
        elif event_type == "finding.disposition_recorded":
            finding = self._state["findings"][payload["finding_id"]]
            finding["disposition"] = copy.deepcopy(dict(payload))
        elif event_type == "phase.advanced":
            self._state["phase"] = payload["next"]
        elif event_type == "operator_input.requested":
            self._state["operator_questions"].append(copy.deepcopy(dict(payload)))
            self._state["status"] = "blocked"
        elif event_type == "run.completed":
            self._state["status"] = "succeeded"
        elif event_type == "run.blocked":
            self._state["status"] = "blocked"
            self._state["blocker"] = payload["reason"]
        elif event_type == "run.paused":
            self._state["status"] = "paused"
        elif event_type == "run.resumed":
            self._state["status"] = "running"
        elif event_type == "run.cancelled":
            self._state["status"] = "cancelled"
        elif event_type == "budget.changed":
            self._state["budgets"][payload["name"]] = payload["value"]
        elif event_type == "coordinator.schema_registered":
            self._state["coordinator_dispatch"]["schema"] = copy.deepcopy(
                payload["schema"]
            )
        elif event_type == "coordinator.session_started":
            self._state["coordinator_dispatch"]["active_session"] = (
                copy.deepcopy(dict(payload))
            )
        elif event_type == "coordinator.session_ended":
            self._state["coordinator_dispatch"]["sessions"].append(
                copy.deepcopy(dict(payload))
            )
            self._state["coordinator_dispatch"]["active_session"] = None
        elif event_type in {"retry.request", "replan.request"}:
            self._state["anomalies"].append(
                {"type": event_type, **copy.deepcopy(dict(payload))}
            )
        self._events.append(event)
        self._state["kernel_event_count"] = len(self._events)

    def _persist(
        self,
        events: Iterable[KernelEvent],
        *,
        command: CommandEnvelope | None = None,
        receipt: CommandReceipt | None = None,
    ) -> None:
        if self.audit is None:
            return
        for event in events:
            self.audit.append(
                "controller_event",
                status="succeeded",
                payload={"controller_event": event.as_dict()},
                actor=AuditActor(
                    event.actor.id,
                    event.actor.role,
                    event.actor.parent_id,
                ),
            )
        if command is not None and receipt is not None:
            self.audit.append(
                "command_processed",
                status="succeeded",
                payload={
                    "command": command.as_dict(),
                    "receipt": receipt.as_dict(),
                },
                actor=AuditActor(
                    command.actor.id,
                    command.actor.role,
                    command.actor.parent_id,
                ),
            )
        self.audit.merge_checkpoint(updates={"controller": self.snapshot()})


def _criterion(item: Mapping[str, Any]) -> dict[str, Any]:
    criterion_id = _payload_text(item, "id", "criterion")
    statement = _payload_text(item, "statement", "criterion")
    source = _payload_text(item, "source", "criterion")
    if source not in {"operator", "repository", "coordinator", "plan"}:
        raise ValueError("criterion source is invalid")
    minimum_satisfiers = item.get("minimum_satisfiers", 1)
    if not isinstance(minimum_satisfiers, int) or minimum_satisfiers < 1:
        raise ValueError("criterion minimum_satisfiers must be positive")
    return {
        "id": criterion_id,
        "statement": statement,
        "source": source,
        "rationale": str(item.get("rationale", "")),
        "status": "pending",
        "evidence_refs": [],
        "satisfied_by": [],
        "minimum_satisfiers": minimum_satisfiers,
    }


def _task(item: Mapping[str, Any]) -> dict[str, Any]:
    parent = item.get("parent_task_id")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ValueError("task parent_task_id must be non-empty when supplied")
    supersedes = item.get("supersedes_task_id")
    if supersedes is not None and (
        not isinstance(supersedes, str) or not supersedes.strip()
    ):
        raise ValueError("task supersedes_task_id must be non-empty when supplied")
    capabilities = _string_list(item, "required_capabilities")
    criteria = _string_list(item, "acceptance_criteria")
    dependencies = _string_list(item, "dependencies")
    optional = item.get("optional", False)
    may_delegate = item.get("may_delegate", False)
    if not isinstance(optional, bool) or not isinstance(may_delegate, bool):
        raise ValueError("task optional and may_delegate must be boolean")
    return {
        "id": _payload_text(item, "id", "task"),
        "role": _payload_text(item, "role", "task"),
        "objective": _payload_text(item, "objective", "task"),
        "context": str(item.get("context", "")),
        "details_schema": _payload_text(item, "details_schema", "task"),
        "required_capabilities": capabilities,
        "acceptance_criteria": criteria,
        "dependencies": dependencies,
        "parent_task_id": parent,
        "supersedes_task_id": supersedes,
        "optional": optional,
        "may_delegate": may_delegate,
        "depth": 0,
        "status": "proposed",
        "attempt_id": "",
        "result": None,
        "evidence": [],
        "summary": None,
    }


def _payload_text(item: Mapping[str, Any], name: str, owner: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{owner} {name} must be non-empty")
    return value


def _string_list(item: Mapping[str, Any], name: str) -> list[str]:
    values = item.get(name, [])
    if (
        not isinstance(values, list)
        or not all(isinstance(value, str) and value.strip() for value in values)
    ):
        raise ValueError(f"{name} must be a string list")
    return list(values)


def _resolve_task_criteria(
    entries: Iterable[str],
    known_criteria: Mapping[str, Any],
) -> list[str]:
    """Resolve "<id>" or "<id>: <statement>" entries to declared criterion ids.

    A literal match against a declared criterion always wins over the
    "<id>: <statement>" split, so a criterion id that itself contains ": "
    is never mistaken for an annotated entry and truncated.
    """

    resolved: list[str] = []
    for value in entries:
        if value in known_criteria:
            resolved.append(value)
        else:
            resolved.append(_parse_criterion_ref(value))
    return resolved


def _parse_criterion_ref(value: str) -> str:
    prefix, separator, _ = value.partition(": ")
    criterion_id = (prefix if separator else value).strip()
    if not criterion_id:
        raise ValueError("acceptance_criteria entry must declare a criterion id")
    return criterion_id
