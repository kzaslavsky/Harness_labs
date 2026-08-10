#!/usr/bin/env python3
"""Run the normal finding-closure chain without coordinator turns between stages."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from review_closure import (
    attempt_history_sha256,
    finish_attempt,
    next_action,
    record_design,
    record_design_review,
    record_escalation,
    record_gate_failure,
    record_review,
    record_test,
    reconcile_interrupted_attempts,
    start_attempt,
    validate_pre_model_closure,
)
from repair_gates import run_repair_gates
from state_io import StateError, atomic_write_json, read_json, sha256_file


PROTOCOL = "implement-v13-codex/closure-program/1"
ACTIONS = ("author_test", "design", "design_review", "fix", "targeted_review")
ROUTINE_OUTCOMES = {
    "design_rejected",
    "fix_rejected",
    "retryable_failure",
    "escalation_required",
    "next_ready",
}
LEGAL_UNBOUND_ROUTES = frozenset({"next_ready", "retry_fix", "redesign"})


def _path_under(artifact_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise StateError(f"closure program {label} must be an absolute path")
    path = Path(value).resolve()
    try:
        path.relative_to(artifact_dir)
    except ValueError:
        raise StateError(f"closure program {label} must be beneath the artifact directory") from None
    return path


def _invoke(
    action: str,
    spec_path: Path,
    invoke: Callable[[str, Path], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = invoke(action, spec_path)
    if result.get("status") != "succeeded":
        raise StateError(f"closure program {action} failed: {result.get('error', 'unknown error')}")
    output_path = Path(str(result.get("output_path", ""))).resolve()
    output = read_json(output_path)
    return result, output


def route_routine_transition(
    ledger_path: Path,
    closure_id: str,
    outcome: str,
    *,
    escalation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route non-semantic repair state without spending a coordinator turn."""
    if outcome not in ROUTINE_OUTCOMES:
        raise StateError("closure driver received a non-routine outcome")
    action = next_action(ledger_path)
    if action.get("closure_id") not in {closure_id, ""}:
        return {
            "status": "next_ready",
            "next_action": action,
            "coordinator_turns": 0,
            "judgment_reason": None,
        }
    status = action.get("status")
    if status == "design_required":
        route = "redesign"
    elif status == "ready_for_fix":
        route = "retry_fix"
    elif status == "escalation_required":
        if not isinstance(escalation, dict):
            return {
                "status": "judgment_required",
                "next_action": action,
                "coordinator_turns": 0,
                "judgment_reason": "ambiguous_dependency_decomposition",
            }
        record_escalation(ledger_path, closure_id, escalation)
        action = next_action(ledger_path)
        route = f"escalated_{escalation.get('action')}"
    elif status == "complete":
        route = "complete"
    else:
        route = "next_ready"
    return {
        "status": route,
        "next_action": action,
        "coordinator_turns": 0,
        "judgment_reason": None,
    }


def continue_without_bound_program(
    result: dict[str, Any], route: dict[str, Any]
) -> dict[str, Any]:
    """Return lawful scheduling routes to the outer coordinator.

    A pre-bound ``routine_programs`` edge remains the zero-turn fast path.  If
    the program did not bind a new strategy for a deterministic retry or
    redesign, the outer controller must expose the durable route to the
    coordinator so it can bind a fresh source-hashed program.  Missing an
    optimization edge is not itself a repository blocker.
    """
    status = route.get("status")
    if status in LEGAL_UNBOUND_ROUTES:
        if status != "next_ready":
            result["coordinator_followup"] = {
                "required": True,
                "route": status,
                "reason": "no pre-bound routine program for the selected strategy route",
            }
        return result
    if (
        result.get("status") != "deterministic_blocked"
        and status not in {"complete", "judgment_required"}
    ):
        result.update(
            status="deterministic_blocked",
            failure_class="routine_program_missing",
            blocker={
                "blocker_class": "routine_program_missing",
                "reason": (
                    "deterministic closure routing selected "
                    f"{status} without a bound next program"
                ),
                "resume_condition": (
                    "bind an acyclic run-owned closure program for the selected route"
                ),
            },
        )
    return result


def run_closure_program(
    program_path: Path,
    artifact_dir: Path,
    invoke: Callable[[str, Path], dict[str, Any]],
    _visited: set[Path] | None = None,
) -> dict[str, Any]:
    resolved_program_path = program_path.resolve()
    visited = set() if _visited is None else set(_visited)
    if resolved_program_path in visited:
        raise StateError("closure routine program graph contains a cycle")
    visited.add(resolved_program_path)
    program = read_json(program_path)
    if program.get("protocol") != PROTOCOL:
        raise StateError("unsupported closure program protocol")
    ledger_path = _path_under(artifact_dir, program.get("closure_ledger_path"), "ledger")
    closure_id = program.get("closure_id")
    if not isinstance(closure_id, str) or not closure_id:
        raise StateError("closure program requires closure_id")
    specs = program.get("specs")
    if not isinstance(specs, dict):
        raise StateError("closure program requires specs")
    ledger = read_json(ledger_path)
    closure = next((item for item in ledger.get("closures", []) if item.get("closure_id") == closure_id), None)
    if not isinstance(closure, dict) or ledger.get("active_closure_id") != closure_id:
        raise StateError("closure program does not target the active closure")
    initial_status = closure.get("status")
    if initial_status == "fix_running":
        receipt_path = program.get("interrupted_receipt_path")
        receipt_sha256 = program.get("interrupted_receipt_sha256")
        closure_ids = program.get("interrupted_closure_ids")
        if (
            not isinstance(receipt_path, str)
            or not isinstance(receipt_sha256, str)
            or not isinstance(closure_ids, list)
        ):
            return {
                "protocol": PROTOCOL,
                "status": "deterministic_blocked",
                "closure_id": closure_id,
                "receipts": [],
                "blocker": {
                    "blocker_class": "interrupted_attempt_unreconciled",
                    "reason": "running fixer attempt lacks exact interrupted receipt evidence",
                    "resume_condition": (
                        "bind the run-owned terminal receipt and complete ordered batch"
                    ),
                },
            }
        try:
            reconciled = reconcile_interrupted_attempts(
                ledger_path,
                {
                    "receipt_path": receipt_path,
                    "receipt_sha256": receipt_sha256,
                    "closure_ids": closure_ids,
                },
            )
        except StateError as exc:
            return {
                "protocol": PROTOCOL,
                "status": "deterministic_blocked",
                "closure_id": closure_id,
                "receipts": [],
                "blocker": {
                    "blocker_class": "interrupted_attempt_unreconciled",
                    "reason": str(exc),
                    "resume_condition": (
                        "supply verified whole-batch interruption evidence"
                    ),
                },
            }
        result = {
            "protocol": PROTOCOL,
            "status": "retry_fix",
            "closure_id": closure_id,
            "receipts": [str(receipt_path)],
            "reconciled_ledger_revision": reconciled["state_revision"],
            "metrics": {
                "deterministic_transitions": 1,
                "coordinator_turns_avoided": 1,
                "model_calls_suppressed": 1,
            },
        }
        return continue_without_bound_program(
            result,
            {
                "status": "retry_fix",
                "next_action": next_action(ledger_path),
                "coordinator_turns": 0,
                "judgment_reason": None,
            },
        )
    if initial_status == "escalation_required":
        route = route_routine_transition(
            ledger_path,
            closure_id,
            "escalation_required",
            escalation=program.get("routine_escalation"),
        )
        return {
            "protocol": PROTOCOL,
            "status": route["status"],
            "closure_id": closure_id,
            "receipts": [],
            "routine_route": route,
            "metrics": {
                "deterministic_transitions": 1,
                "coordinator_turns_avoided": 1,
            },
        }
    if initial_status == "test_required":
        required_actions = list(ACTIONS)
        if closure.get("complexity") != "architectural":
            required_actions.remove("design")
            required_actions.remove("design_review")
    elif initial_status == "design_required":
        required_actions = ["design", "design_review", "fix", "targeted_review"]
    elif initial_status == "ready_for_fix":
        required_actions = ["fix", "targeted_review"]
    else:
        raise StateError(
            "closure program must begin at test_required, design_required, "
            "ready_for_fix, or escalation_required"
        )
    if set(specs) != set(required_actions):
        raise StateError("closure program specs do not exactly match the complexity route")
    spec_paths = {action: _path_under(artifact_dir, specs[action], f"{action} spec") for action in required_actions}
    graph_aware = ledger.get("dependency_graph_status") == "validated"
    batch: dict[str, Any] | None = None
    batch_closure_ids = [closure_id]
    targeted_spec_paths: dict[str, Path] = {
        closure_id: spec_paths["targeted_review"]
    }
    if graph_aware:
        batch_path = _path_under(
            artifact_dir, program.get("repair_batch_path"), "repair batch"
        )
        batch = read_json(batch_path)
        if (
            batch.get("protocol") != "implement-v13-codex/repair-batch/2"
            or closure_id not in batch.get("closure_ids", [])
            or batch.get("dependency_graph_sha256")
            != ledger.get("dependency_graph_sha256")
        ):
            raise StateError("closure program repair batch subject mismatch")
        batch_closure_ids = list(batch["closure_ids"])
        raw_review_specs = program.get("batch_targeted_review_specs", {})
        if len(batch_closure_ids) > 1:
            if not isinstance(raw_review_specs, dict) or set(raw_review_specs) != (
                set(batch_closure_ids) - {closure_id}
            ):
                raise StateError(
                    "multi-closure program requires one independent targeted-review spec per secondary closure"
                )
            targeted_spec_paths.update({
                member: _path_under(
                    artifact_dir,
                    raw_review_specs[member],
                    f"{member} targeted-review spec",
                )
                for member in raw_review_specs
            })
    receipts: list[str] = []

    def continue_routine(
        result: dict[str, Any], route: dict[str, Any]
    ) -> dict[str, Any]:
        routine_programs = program.get("routine_programs", {})
        if not isinstance(routine_programs, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in routine_programs.items()
        ):
            raise StateError("closure routine_programs must map states to paths")
        next_raw = routine_programs.get(route.get("status"))
        if next_raw is None:
            return continue_without_bound_program(result, route)
        next_path = _path_under(
            artifact_dir, next_raw, f"{route.get('status')} routine program"
        )
        continued = run_closure_program(
            next_path,
            artifact_dir,
            invoke,
            _visited=visited,
        )
        continued["receipts"] = result.get("receipts", []) + continued.get(
            "receipts", []
        )
        prior_metrics = result.get("metrics", {})
        next_metrics = continued.get("metrics", {})
        continued["metrics"] = {
            key: int(prior_metrics.get(key, 0)) + int(next_metrics.get(key, 0))
            for key in set(prior_metrics) | set(next_metrics)
        }
        continued.setdefault("routine_history", []).insert(
            0,
            {
                "from_program": str(resolved_program_path),
                "route": route["status"],
                "to_program": str(next_path),
            },
        )
        return continued

    if "author_test" in required_actions:
        test_result, test_output = _invoke("author_test", spec_paths["author_test"], invoke)
        receipts.append(str(test_result.get("receipt_id", "")))
        author_spec = read_json(spec_paths["author_test"])
        record_test(ledger_path, closure_id, {
            "author_role": author_spec.get("role"),
            "author_receipt_id": test_result.get("receipt_id"),
            "test_paths": test_output.get("test_paths"),
            "commands": test_output.get("commands"),
            "observed_failure": test_output.get("observed_failure"),
            "evidence": test_output.get("evidence"),
            "effect_contract": test_output.get("effect_contract"),
            "repository_root": test_output.get("repository_root"),
            "repository_identity": test_output.get("repository_identity"),
            "test_node_id": test_output.get("test_node_id"),
            "test_source_path": test_output.get("test_source_path"),
            "test_source_sha256": test_output.get("test_source_sha256"),
            "assertions": test_output.get("assertions"),
            "capability_manifest_path": author_spec.get("capability_manifest_path"),
            "capability_manifest_sha256": author_spec.get(
                "capability_manifest_sha256"
            ),
        })
    pre_model = validate_pre_model_closure(ledger_path, closure_id)
    gate_path = artifact_dir / (
        hashlib.sha256(closure_id.encode("utf-8")).hexdigest()
        + ".repair-pre-model-gates.v1.json"
    )
    atomic_write_json(gate_path, pre_model)
    if pre_model["model_calls_permitted"] is not True:
        return {
            "protocol": PROTOCOL,
            "status": "deterministic_blocked",
            "closure_id": closure_id,
            "receipts": receipts,
            "gate_path": str(gate_path),
            "gate_sha256": sha256_file(gate_path),
            "metrics": {
                "deterministic_transitions": 1,
                "coordinator_turns_avoided": 1,
                "model_calls_suppressed": len(required_actions) - 1,
            },
        }

    if "design" in required_actions:
        design_result, design_output = _invoke("design", spec_paths["design"], invoke)
        receipts.append(str(design_result.get("receipt_id", "")))
        record_design(ledger_path, closure_id, {
            "designer_receipt_id": design_result.get("receipt_id"),
            "strategy_family": design_output.get("strategy_family"),
            "result_path": design_result.get("output_path"),
            "result_sha256": sha256_file(Path(str(design_result.get("output_path")))),
            "effect_contract": design_output.get("effect_contract"),
        })
        designed = next(
            item for item in read_json(ledger_path)["closures"]
            if item.get("closure_id") == closure_id
        )
        if designed["status"] != "design_review_required":
            route = route_routine_transition(
                ledger_path, closure_id, "design_rejected",
                escalation=program.get("routine_escalation"),
            )
            rejected_result = {
                "protocol": PROTOCOL, "status": "design_rejected", "closure_id": closure_id,
                "receipts": receipts,
                "routine_route": route,
                "metrics": {"deterministic_transitions": 2, "coordinator_turns_avoided": 1},
            }
            return continue_routine(rejected_result, route)
        review_result, review_output = _invoke("design_review", spec_paths["design_review"], invoke)
        receipts.append(str(review_result.get("receipt_id", "")))
        review_spec = read_json(spec_paths["design_review"])
        record_design_review(ledger_path, closure_id, {
            "reviewer_role": review_spec.get("role"),
            "reviewer_receipt_id": review_result.get("receipt_id"),
            "approved": review_output.get("approved"),
            "evidence": review_output.get("evidence"),
            "effect_contract": review_output.get("effect_contract"),
        })
        reviewed = next(
            item for item in read_json(ledger_path)["closures"]
            if item.get("closure_id") == closure_id
        )
        if review_output.get("approved") is not True or reviewed["status"] != "ready_for_fix":
            route = route_routine_transition(
                ledger_path, closure_id, "design_rejected",
                escalation=program.get("routine_escalation"),
            )
            rejected_result = {
                "protocol": PROTOCOL, "status": "design_rejected", "closure_id": closure_id,
                "receipts": receipts,
                "routine_route": route,
                "metrics": {"deterministic_transitions": 3, "coordinator_turns_avoided": 2},
            }
            return continue_routine(rejected_result, route)

    pre_fix = validate_pre_model_closure(ledger_path, closure_id)
    atomic_write_json(gate_path, pre_fix)
    if pre_fix["model_calls_permitted"] is not True:
        return {
            "protocol": PROTOCOL,
            "status": "deterministic_blocked",
            "closure_id": closure_id,
            "receipts": receipts,
            "gate_path": str(gate_path),
            "gate_sha256": sha256_file(gate_path),
            "metrics": {
                "deterministic_transitions": len(receipts),
                "coordinator_turns_avoided": max(1, len(receipts)),
                "model_calls_suppressed": 2,
            },
        }

    ledger = read_json(ledger_path)
    closure = next(item for item in ledger["closures"] if item["closure_id"] == closure_id)
    fix_spec = read_json(spec_paths["fix"])
    history_hash = attempt_history_sha256(closure)
    fix_spec.update(
        closure_strategy_family=program.get("strategy_family"),
        closure_attempt_history_sha256=history_hash,
    )
    collision = closure.get("active_collision")
    if isinstance(collision, dict):
        fix_spec.update(
            collision_packet_path=collision["packet_path"],
            collision_packet_sha256=collision["packet_sha256"],
        )
        prompt_path = _path_under(
            artifact_dir, fix_spec.get("prompt_path"), "collision fixer prompt"
        )
        directive = (
            "\n\nThis is a bounded collision repair. Read the frozen packet and satisfy "
            "every listed closure simultaneously; do not repair either closure in isolation.\n"
            f"COLLISION_REPAIR_PACKET_PATH={collision['packet_path']}\n"
            f"COLLISION_REPAIR_PACKET_SHA256={collision['packet_sha256']}\n"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        if directive not in prompt_text:
            prompt_path.write_text(prompt_text + directive, encoding="utf-8")
    atomic_write_json(spec_paths["fix"], fix_spec)
    for member in batch_closure_ids:
        member_closure = next(
            item for item in read_json(ledger_path)["closures"]
            if item["closure_id"] == member
        )
        start_attempt(ledger_path, member, {
            "attempt_history_sha256": attempt_history_sha256(member_closure),
            "strategy_family": program.get("strategy_family"),
            "strategy_summary": program.get("strategy_summary"),
            "invocation_id": fix_spec.get("receipt_id"),
            "fixer_identity": program.get("fixer_identity"),
        })
    fix_result, _ = _invoke("fix", spec_paths["fix"], invoke)
    receipts.append(str(fix_result.get("receipt_id", "")))
    for member in batch_closure_ids:
        finish_attempt(ledger_path, member, {
            "result_path": fix_result.get("output_path"),
            "result_sha256": sha256_file(Path(str(fix_result.get("output_path")))),
        })

    current_ledger = read_json(ledger_path)
    if graph_aware:
        evidence_path = _path_under(
            artifact_dir, program.get("gate_evidence_path"), "gate evidence"
        )
        gate_receipt_path = _path_under(
            artifact_dir, program.get("gate_receipt_path"), "gate receipt"
        )
        gate_receipt = run_repair_gates(
            batch_path, evidence_path, gate_receipt_path
        )
        if gate_receipt["status"] != "passed":
            for member in batch_closure_ids:
                record_gate_failure(
                    ledger_path,
                    member,
                    gate_receipt_path=gate_receipt_path,
                )
            route = route_routine_transition(
                ledger_path,
                closure_id,
                "fix_rejected",
                escalation=program.get("routine_escalation"),
            )
            failed_result = {
                "protocol": PROTOCOL,
                "status": "deterministic_blocked",
                "closure_id": closure_id,
                "receipts": receipts,
                "gate_path": str(gate_receipt_path),
                "gate_sha256": sha256_file(gate_receipt_path),
                "failure_class": gate_receipt["failure_class"],
                "routine_route": route,
                "metrics": {
                    "deterministic_transitions": len(receipts) + len(gate_receipt["gates"]),
                    "coordinator_turns_avoided": max(1, len(receipts)),
                    "model_calls_suppressed": 1,
                },
            }
            return continue_routine(failed_result, route)

    for member in batch_closure_ids:
        targeted_result, targeted_output = _invoke(
            "targeted_review" if member == closure_id else f"targeted_review:{member}",
            targeted_spec_paths[member],
            invoke,
        )
        receipts.append(str(targeted_result.get("receipt_id", "")))
        targeted_spec = read_json(targeted_spec_paths[member])
        statuses = {
            item["fingerprint"]: item["status"]
            for item in targeted_output.get("findings", [])
            if isinstance(item, dict) and "fingerprint" in item and "status" in item
        }
        review_record = {
            "reviewer_role": targeted_spec.get("role"),
            "reviewer_receipt_id": targeted_result.get("receipt_id"),
            "finding_statuses": statuses,
            "regression_checks": targeted_output.get("regression_checks", {}),
            "evidence": targeted_output.get("evidence"),
        }
        if graph_aware:
            review_record.update(
                gate_receipt_path=str(gate_receipt_path),
                gate_receipt_sha256=sha256_file(gate_receipt_path),
            )
        record_review(ledger_path, member, review_record)
    finals = [
        item
        for item in read_json(ledger_path)["closures"]
        if item["closure_id"] in batch_closure_ids
    ]
    final = next(item for item in finals if item["closure_id"] == closure_id)
    transition_count = len(required_actions)
    route = (
        route_routine_transition(
            ledger_path,
            closure_id,
            "next_ready" if final["status"] == "closed" else "fix_rejected",
            escalation=program.get("routine_escalation"),
        )
        if any(item["status"] != "closed" for item in finals)
        or next_action(ledger_path)["status"] != "complete"
        else {
            "status": "complete",
            "next_action": next_action(ledger_path),
            "coordinator_turns": 0,
            "judgment_reason": None,
        }
    )
    result = {
        "protocol": PROTOCOL,
        "status": (
            "closed"
            if all(item["status"] == "closed" for item in finals)
            else final["status"]
        ),
        "closure_id": closure_id,
        "batch_closure_ids": batch_closure_ids,
        "receipts": receipts,
        "routine_route": route,
        "metrics": {
            "deterministic_transitions": transition_count,
            "coordinator_turns_avoided": max(0, transition_count - 1),
        },
    }
    return continue_routine(result, route)
