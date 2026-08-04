"""Deterministic coordinator views and bounded evidence queries."""

from __future__ import annotations

import copy
from typing import Any

from .controller_evidence import EvidenceCatalog
from .controller_kernel import ControllerKernel


QUERY_NAMES = frozenset(
    {
        "run.get_view",
        "task.get_result",
        "artifact.open",
        "event.query",
        "decision.list",
        "acceptance.get_matrix",
        "finding.list",
    }
)


def project_run_view(kernel: ControllerKernel) -> dict[str, Any]:
    """Build the coordinator's compact working view without scenario branches."""

    state = kernel.snapshot()
    tasks = []
    for task_id in sorted(state["tasks"]):
        task = state["tasks"][task_id]
        tasks.append(
            {
                "id": task["id"],
                "parent_task_id": task["parent_task_id"],
                "depth": task["depth"],
                "role": task["role"],
                "objective": task["objective"],
                "status": task["status"],
                "details_schema": task["details_schema"],
                "required_capabilities": list(task["required_capabilities"]),
                "summary": task.get("summary"),
            }
        )
    findings = []
    for finding_id in sorted(state["findings"]):
        finding = state["findings"][finding_id]
        findings.append(
            {
                "id": finding_id,
                "producer_task_id": finding["producer_task_id"],
                "category": finding["category"],
                "severity": finding["severity"],
                "statement": finding["statement"],
                "requires_disposition": finding.get(
                    "requires_disposition",
                    False,
                ),
                "disposition": copy.deepcopy(finding.get("disposition")),
                "evidence_refs": list(finding.get("evidence_refs", [])),
                "source_finding_ids": list(
                    finding.get("source_finding_ids", [])
                ),
            }
        )
    criteria = [
        copy.deepcopy(state["criteria"][criterion_id])
        for criterion_id in sorted(state["criteria"])
    ]
    claims = []
    for task_id in sorted(state["tasks"]):
        result = state["tasks"][task_id].get("result")
        if not isinstance(result, dict):
            continue
        for claim in result.get("claims", []):
            claims.append(
                {
                    **copy.deepcopy(claim),
                    "producer_task_id": task_id,
                }
            )
    return {
        "protocol": "controller-run-view/1",
        "run_id": state["run_id"],
        "objective": state["objective"],
        "revision": state["revision"],
        "status": state["status"],
        "phase": state["phase"],
        "limits": copy.deepcopy(state["limits"]),
        "criteria": criteria,
        "tasks": tasks,
        "decisions": [
            copy.deepcopy(state["decisions"][decision_id])
            for decision_id in sorted(state["decisions"])
        ],
        "claims": claims,
        "findings": findings,
        "artifacts": [
            copy.deepcopy(state["artifacts"][ref])
            for ref in sorted(state["artifacts"])
        ],
        "completion_failures": list(kernel.completion_failures()),
        "anomalies": copy.deepcopy(state["anomalies"]),
        "operator_questions": copy.deepcopy(state["operator_questions"]),
        "coordinator_dispatch": copy.deepcopy(state["coordinator_dispatch"]),
        "allowed_commands": sorted(
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
        ),
        "event_cursor": len(kernel.events),
    }


class ControllerQueries:
    """Read-only query surface exposed to the coordinator."""

    def __init__(
        self,
        kernel: ControllerKernel,
        evidence: EvidenceCatalog,
        *,
        max_artifact_bytes: int = 256_000,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self.kernel = kernel
        self.evidence = evidence
        self.max_artifact_bytes = max_artifact_bytes

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in QUERY_NAMES:
            raise ValueError(f"unknown controller query: {name}")
        if name == "run.get_view":
            return project_run_view(self.kernel)
        if name == "task.get_result":
            task = self.kernel.task(_required_text(arguments, "task_id"))
            return {
                "task_id": task["id"],
                "status": task["status"],
                "result": copy.deepcopy(task["result"]),
                "evidence": list(task["evidence"]),
            }
        if name == "artifact.open":
            ref = _required_text(arguments, "ref")
            record = self.evidence.metadata(ref)
            content = self.evidence.open(ref)
            if len(content) > self.max_artifact_bytes:
                raise ValueError("artifact exceeds coordinator query size limit")
            return {
                "metadata": record.as_dict(),
                "content": content.decode("utf-8", errors="replace"),
            }
        state = self.kernel.snapshot()
        if name == "decision.list":
            return {
                "decisions": [
                    copy.deepcopy(state["decisions"][decision_id])
                    for decision_id in sorted(state["decisions"])
                ]
            }
        if name == "acceptance.get_matrix":
            return {
                "criteria": [
                    copy.deepcopy(state["criteria"][criterion_id])
                    for criterion_id in sorted(state["criteria"])
                ]
            }
        if name == "finding.list":
            return {
                "findings": [
                    copy.deepcopy(state["findings"][finding_id])
                    for finding_id in sorted(state["findings"])
                ]
            }
        event_type = arguments.get("event_type")
        task_id = arguments.get("task_id")
        events = []
        for event in self.kernel.events:
            if event_type is not None and event.event_type != event_type:
                continue
            if task_id is not None and event.payload.get("task_id") != task_id:
                continue
            events.append(event.as_dict())
        return {"events": events}


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value
