#!/usr/bin/env python3
"""Derive and enforce bounded implementation worker groups from a validated plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from state_io import StateError, atomic_write_json, read_json


PROTOCOL = "implement-v13-codex/implementation-partition/1"
MAX_GROUPS = 3
MAX_EFFORT = 12.0
MAX_WRITE_PATHS = 18


def _ordered_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise StateError("implementation partition requires a nonempty plan.steps")
    by_id: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not step["id"]:
            raise StateError("every implementation step requires a nonempty id")
        if step["id"] in by_id:
            raise StateError("implementation plan contains duplicate step ids")
        dependencies = step.get("dependencies")
        paths = step.get("write_paths")
        tests = step.get("targeted_tests")
        effort = step.get("effort")
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(item, str) and item for item in dependencies)
            or not isinstance(paths, list)
            or not all(isinstance(item, str) and item for item in paths)
            or not isinstance(tests, list)
            or not all(isinstance(item, str) and item for item in tests)
            or isinstance(effort, bool)
            or not isinstance(effort, (int, float))
            or effort <= 0
        ):
            raise StateError(f"implementation step {step['id']} has an invalid bounded-work contract")
        by_id[step["id"]] = step
    for step in steps:
        if not set(step["dependencies"]).issubset(by_id) or step["id"] in step["dependencies"]:
            raise StateError(f"implementation step {step['id']} has invalid dependencies")
    ordered: list[dict[str, Any]] = []
    remaining = dict(by_id)
    complete: set[str] = set()
    while remaining:
        ready = [step for step in steps if step["id"] in remaining and set(step["dependencies"]).issubset(complete)]
        if not ready:
            raise StateError("implementation plan step dependencies contain a cycle")
        for step in ready:
            ordered.append(step)
            complete.add(step["id"])
            remaining.pop(step["id"])
    return ordered


def derive_partition(plan: dict[str, Any]) -> dict[str, Any]:
    steps = _ordered_steps(plan)
    raw_groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_effort = 0.0
    current_paths: set[str] = set()
    for step in steps:
        next_effort = current_effort + float(step["effort"])
        next_paths = current_paths.union(step["write_paths"])
        if current and (next_effort > MAX_EFFORT or len(next_paths) > MAX_WRITE_PATHS):
            raw_groups.append(current)
            current, current_effort, current_paths = [], 0.0, set()
        current.append(step)
        current_effort += float(step["effort"])
        current_paths.update(step["write_paths"])
    if current:
        raw_groups.append(current)
    if len(raw_groups) > MAX_GROUPS:
        raw_groups = [*raw_groups[: MAX_GROUPS - 1], sum(raw_groups[MAX_GROUPS - 1 :], [])]

    step_group: dict[str, str] = {}
    groups: list[dict[str, Any]] = []
    for index, group_steps in enumerate(raw_groups, 1):
        group_id = f"implementation_group_{index}"
        step_ids = [step["id"] for step in group_steps]
        write_paths = list(dict.fromkeys(path for step in group_steps for path in step["write_paths"]))
        targeted_tests = list(dict.fromkeys(test for step in group_steps for test in step["targeted_tests"]))
        effort = sum(float(step["effort"]) for step in group_steps)
        exceptions: list[str] = []
        if effort > MAX_EFFORT:
            exceptions.append(f"effort {effort:g} exceeds {MAX_EFFORT:g} because the three-group ceiling was reached")
        if len(write_paths) > MAX_WRITE_PATHS:
            exceptions.append(
                f"write path count {len(write_paths)} exceeds {MAX_WRITE_PATHS} because the three-group ceiling was reached"
            )
        groups.append({
            "group_id": group_id,
            "step_ids": step_ids,
            "depends_on": [],
            "write_paths": write_paths,
            "targeted_tests": targeted_tests,
            "effort": effort,
            "exceptions": exceptions,
        })
        for step_id in step_ids:
            step_group[step_id] = group_id
    by_step = {step["id"]: step for step in steps}
    for group in groups:
        dependencies = {
            step_group[dependency]
            for step_id in group["step_ids"]
            for dependency in by_step[step_id]["dependencies"]
            if step_group[dependency] != group["group_id"]
        }
        group["depends_on"] = [item["group_id"] for item in groups if item["group_id"] in dependencies]
    return {
        "protocol": PROTOCOL,
        "limits": {
            "max_groups": MAX_GROUPS,
            "max_effort_per_group": MAX_EFFORT,
            "max_write_paths_per_group": MAX_WRITE_PATHS,
        },
        "groups": groups,
    }


def ensure_partition(plan_path: Path, output_path: Path) -> dict[str, Any]:
    derived = derive_partition(read_json(plan_path))
    if output_path.exists():
        existing = read_json(output_path)
        if existing != derived:
            raise StateError("implementation partition differs from the deterministic plan-derived contract")
        return existing
    atomic_write_json(output_path, derived)
    return derived


def validate_worker_spec(spec: dict[str, Any], partition: dict[str, Any]) -> None:
    group_id = spec.get("implementation_group_id")
    groups = [item for item in partition.get("groups", []) if item.get("group_id") == group_id]
    if len(groups) != 1:
        raise StateError("implementation worker must name exactly one plan-derived group")
    group = groups[0]
    if spec.get("assigned_step_ids") != group["step_ids"]:
        raise StateError("implementation worker assigned_step_ids mismatch the bounded group")
    if spec.get("allowed_write_paths") != group["write_paths"]:
        raise StateError("implementation worker allowed_write_paths mismatch the bounded group")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    print(json.dumps(ensure_partition(Path(args.plan).resolve(), Path(args.output).resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
