#!/usr/bin/env python3
"""Run deterministic post-fix gates before any targeted model review."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from repair_preflight import (
    certification_runtime_sha256,
    repository_identity,
    validate_capability_manifest,
    validate_test_command,
)
from state_io import (
    StateError,
    atomic_write_json,
    canonical_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
)


PROTOCOL = "implement-v13-codex/repair-gates/1"
INPUT_PROTOCOL = "implement-v13-codex/repair-gate-input/1"
GATE_ORDER = (
    "forbidden_access",
    "pre_communication_output_bound",
    "process_evidence",
    "capability_manifest",
    "production_certification",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_schema(document: dict[str, Any], name: str) -> None:
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError as exc:
        raise StateError("jsonschema is required for repair gates") from exc
    schema = read_json(Path(__file__).resolve().parents[1] / "schemas" / name)
    try:
        jsonschema.Draft202012Validator(schema).validate(document)
    except jsonschema.ValidationError as exc:
        raise StateError(f"{name} validation failed at {list(exc.absolute_path)}") from exc


def _run_owned(artifact_dir: Path, raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise StateError(f"{label} must be an absolute run-owned path")
    path = Path(raw).resolve()
    try:
        path.relative_to(artifact_dir.resolve())
    except ValueError as exc:
        raise StateError(f"{label} must be beneath the artifact directory") from exc
    if not path.is_file():
        raise StateError(f"{label} is missing")
    return path


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StateError(f"{label} must be a positive configured integer")
    return value


def _gate(
    gate_class: str,
    check: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        evidence = check()
        return {
            "gate_class": gate_class,
            "status": "passed",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "evidence": evidence,
        }
    except Exception as exc:
        return {
            "gate_class": gate_class,
            "status": "failed",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "failure_class": f"{gate_class}_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "evidence": {},
        }


def _forbidden_access(evidence: dict[str, Any]) -> dict[str, Any]:
    observed_reads = evidence.get("observed_reads")
    observed_selectors = evidence.get("observed_selectors")
    forbidden_reads = evidence.get("forbidden_reads")
    forbidden_selectors = evidence.get("forbidden_selectors")
    if not all(
        isinstance(value, list) and all(isinstance(item, str) and item for item in value)
        for value in (
            observed_reads,
            observed_selectors,
            forbidden_reads,
            forbidden_selectors,
        )
    ):
        raise StateError("forbidden-access evidence must contain string arrays")
    read_overlap = sorted(set(observed_reads).intersection(forbidden_reads))
    selector_overlap = sorted(set(observed_selectors).intersection(forbidden_selectors))
    if read_overlap:
        raise StateError(f"repair performed forbidden reads: {read_overlap}")
    if selector_overlap:
        raise StateError(f"repair used forbidden selectors: {selector_overlap}")
    selector_contract = evidence.get("selector_contract")
    required_false = (
        "caller_selectable",
        "production_selectable",
        "caller_claim_selectable",
    )
    if not isinstance(selector_contract, dict) or any(
        selector_contract.get(field) is not False for field in required_false
    ):
        raise StateError("repair selector contract is caller- or production-selectable")
    return {
        "observed_read_count": len(observed_reads),
        "observed_selector_count": len(observed_selectors),
        "forbidden_overlap_count": 0,
    }


def _output_bound(evidence: dict[str, Any]) -> dict[str, Any]:
    limit = _positive_int(evidence.get("limit_bytes"), "output limit_bytes")
    observed = evidence.get("observed_bytes_before_communicate")
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
        raise StateError("observed pre-communication bytes must be a nonnegative integer")
    if evidence.get("bound_checked_before_communicate") is not True:
        raise StateError("output bound was not checked before communicate()")
    if evidence.get("communicate_started_at_check") is not False:
        raise StateError("output bound check occurred only after communicate()")
    if observed > limit:
        raise StateError("pre-communication output exceeded the configured bound")
    return {"limit_bytes": limit, "observed_bytes": observed}


def _process_evidence(
    artifact_dir: Path, evidence: dict[str, Any]
) -> dict[str, Any]:
    receipt_path = _run_owned(
        artifact_dir, evidence.get("receipt_path"), "process receipt"
    )
    expected = evidence.get("receipt_sha256")
    if not isinstance(expected, str) or sha256_file(receipt_path) != expected:
        raise StateError("process receipt hash mismatch")
    receipt = read_json(receipt_path)
    required_hashes = {
        "prompt",
        "schema",
        "codex_executable",
        "stdout",
        "stderr",
        "output",
        "child_spec",
        "exit",
    }
    if (
        receipt.get("status") != "succeeded"
        or receipt.get("exit_code") != 0
        or receipt.get("timed_out") is not False
        or not isinstance(receipt.get("pid"), int)
        or not isinstance(receipt.get("process_group_id"), int)
        or not isinstance(receipt.get("process_start_fingerprint"), str)
        or not receipt["process_start_fingerprint"]
        or not {"thread.started", "turn.completed"}.issubset(
            set(receipt.get("event_types", []))
        )
        or set(receipt.get("artifact_sha256", {})) != required_hashes
    ):
        raise StateError("process receipt lacks terminal supervised-process evidence")
    terminal = receipt.get("terminal_cause")
    if not isinstance(terminal, dict) or terminal.get("class") != "none":
        raise StateError("process receipt has a non-success terminal cause")
    return {
        "receipt_id": receipt.get("receipt_id"),
        "receipt_sha256": expected,
        "event_types": receipt.get("event_types"),
    }


def _production_sandbox(
    artifact_dir: Path,
    feature_run_id: str,
    repository_root: Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = _run_owned(
        artifact_dir, evidence.get("capability_manifest_path"), "capability manifest"
    )
    digest = evidence.get("capability_manifest_sha256")
    if not isinstance(digest, str):
        raise StateError("capability manifest digest is missing")
    manifest = validate_capability_manifest(
        manifest_path,
        digest,
        repository_root=repository_root,
        feature_run_id=feature_run_id,
        controller_package_digest=evidence.get("controller_package_digest"),
    )
    probes = manifest.get("probes", [])
    if (
        manifest.get("simulation_only") is not False
        or manifest.get("status") != "ready"
        or not probes
        or any(
            probe.get("production_real") is not True
            or probe.get("passed") is not True
            for probe in probes
        )
    ):
        raise StateError("simulated or incomplete sandbox smoke cannot authorize review")
    return {
        "capability_manifest_sha256": digest,
        "probe_count": len(probes),
        "production_real": True,
        "simulation_only": False,
        "broker_path": manifest["broker_path"],
        "broker_sha256": manifest["broker_sha256"],
        "certification_runtime_sha256": certification_runtime_sha256(
            manifest["certification_runtime"]
        ),
    }


def _production_certification(
    batch: dict[str, Any],
    repository_root: Path,
    evidence: dict[str, Any],
    manifest: dict[str, Any],
    command_runner: Callable[..., Any],
) -> dict[str, Any]:
    timeout = _positive_int(
        evidence.get("command_timeout_seconds"), "regression command timeout"
    )
    results: list[dict[str, Any]] = []
    commands = batch.get("selected_commands")
    if not isinstance(commands, list) or not commands:
        raise StateError("repair batch has no dependency-mapped regression commands")
    broker = Path(str(manifest["broker_path"])).resolve()
    if not broker.is_file() or sha256_file(broker) != manifest["broker_sha256"]:
        raise StateError("production broker identity changed before certification")
    for raw_command in commands:
        command = validate_test_command(raw_command, manifest)
        scratch = Path(
            tempfile.mkdtemp(prefix="implement-v13-certification-")
        ).resolve()
        profile = " ".join(
            (
                "(version 1)",
                "(allow default)",
                f"(deny file-write* (subpath {json.dumps(str(repository_root))}))",
                f"(allow file-write* (subpath {json.dumps(str(scratch))}))",
            )
        )
        environment = dict(os.environ)
        environment.update(
            TMPDIR=str(scratch),
            TMP=str(scratch),
            TEMP=str(scratch),
            PYTHONDONTWRITEBYTECODE="1",
        )
        wrapped = [str(broker), "-p", profile, *command["argv"]]
        try:
            completed = command_runner(
                wrapped,
                cwd=repository_root,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=environment,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            scratch_sha256 = sha256_bytes(
                canonical_bytes(
                    [
                        {
                            "path": str(path.relative_to(scratch)),
                            "sha256": sha256_file(path),
                        }
                        for path in sorted(scratch.rglob("*"))
                        if path.is_file() and not path.is_symlink()
                    ]
                )
            )
            result = {
                "test_node_id": batch["selected_test_nodes"][len(results)],
                "command": command,
                "command_sha256": sha256_bytes(canonical_bytes(command)),
                "wrapped_argv": wrapped,
                "broker_path": str(broker),
                "broker_sha256": manifest["broker_sha256"],
                "policy_sha256": sha256_bytes(profile.encode("utf-8")),
                "certification_runtime_sha256": (
                    command["certification_runtime_sha256"]
                ),
                "cwd": str(repository_root),
                "repository_identity": repository_identity(repository_root),
                "rc": completed.returncode,
                "stdout_sha256": sha256_bytes(stdout or b""),
                "stderr_sha256": sha256_bytes(stderr or b""),
                "scratch_contents_sha256": scratch_sha256,
            }
            results.append(result)
            if completed.returncode != 0:
                raise StateError(
                    "production certification failed: "
                    f"{command['argv']!r} rc={completed.returncode}"
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            if results:
                results[-1]["scratch_removed"] = not scratch.exists()
    return {
        "selected_test_nodes": batch["selected_test_nodes"],
        "commands_run": len(results),
        "results": results,
    }


def run_repair_gates(
    batch_path: Path,
    evidence_path: Path,
    receipt_path: Path,
    *,
    command_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Persist a fail-closed, ordered receipt; no model review is invoked here."""
    artifact_dir = receipt_path.resolve().parent
    batch_path = _run_owned(artifact_dir, str(batch_path.resolve()), "repair batch")
    evidence_path = _run_owned(
        artifact_dir, str(evidence_path.resolve()), "repair gate input"
    )
    batch = read_json(batch_path)
    evidence = read_json(evidence_path)
    _validate_schema(evidence, "repair-gate-input.schema.json")
    if batch.get("protocol") != "implement-v13-codex/repair-batch/2":
        raise StateError("unsupported repair batch protocol")
    if evidence.get("protocol") != INPUT_PROTOCOL:
        raise StateError("unsupported repair gate input protocol")
    if (
        evidence.get("feature_run_id") != batch.get("feature_run_id")
        or evidence.get("batch_sha256") != sha256_file(batch_path)
    ):
        raise StateError("repair gate input batch subject mismatch")
    graph_path = _run_owned(
        artifact_dir, batch.get("dependency_graph_path"), "repair dependency graph"
    )
    if sha256_file(graph_path) != batch.get("dependency_graph_sha256"):
        raise StateError("repair batch dependency graph hash mismatch")
    graph = read_json(graph_path)
    repository_root = Path(str(graph.get("repository_root", ""))).resolve()
    if not repository_root.is_dir():
        raise StateError("repair dependency graph repository root is missing")

    manifest_holder: dict[str, Any] = {}

    def capability_check() -> dict[str, Any]:
        result = _production_sandbox(
            artifact_dir,
            str(batch["feature_run_id"]),
            repository_root,
            evidence.get("production_sandbox", {}),
        )
        manifest_path = _run_owned(
            artifact_dir,
            evidence.get("production_sandbox", {}).get(
                "capability_manifest_path"
            ),
            "capability manifest",
        )
        manifest_holder["manifest"] = read_json(manifest_path)
        return result

    checks = (
        lambda: _forbidden_access(evidence.get("forbidden_access", {})),
        lambda: _output_bound(evidence.get("output_bound", {})),
        lambda: _process_evidence(artifact_dir, evidence.get("process_evidence", {})),
        capability_check,
        lambda: _production_certification(
            batch,
            repository_root,
            evidence.get("dependency_regression", {}),
            manifest_holder["manifest"],
            command_runner,
        ),
    )
    gates: list[dict[str, Any]] = []
    for gate_class, check in zip(GATE_ORDER, checks, strict=True):
        outcome = _gate(gate_class, check)
        gates.append(outcome)
        if outcome["status"] != "passed":
            break
    receipt = {
        "protocol": PROTOCOL,
        "status": "passed" if len(gates) == len(GATE_ORDER) and all(
            gate["status"] == "passed" for gate in gates
        ) else "failed",
        "failure_class": next(
            (gate.get("failure_class", "") for gate in gates if gate["status"] == "failed"),
            "",
        ),
        "feature_run_id": batch["feature_run_id"],
        "batch_id": batch["batch_id"],
        "batch_sha256": sha256_file(batch_path),
        "batch_closure_ids": batch["closure_ids"],
        "affected_closure_ids": batch["component_closure_ids"],
        "dependency_graph_sha256": batch["dependency_graph_sha256"],
        "selected_test_nodes": batch["selected_test_nodes"],
        "targeted_review_permitted": len(gates) == len(GATE_ORDER) and all(
            gate["status"] == "passed" for gate in gates
        ),
        "gates": gates,
        "events": [
            {
                "event": "repair_gate_completed",
                "gate_class": gate["gate_class"],
                "status": gate["status"],
                "elapsed_ms": gate["elapsed_ms"],
            }
            for gate in gates
        ],
        "created_at": _utc_now(),
        "input_sha256": sha256_file(evidence_path),
    }
    _validate_schema(receipt, "repair-gate-receipt.schema.json")
    atomic_write_json(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)
    result = run_repair_gates(args.batch, args.evidence, args.receipt)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
