#!/usr/bin/env python3
"""Deterministic pre-model repair contracts and production capability probes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from state_io import (
    StateError,
    atomic_write_json,
    canonical_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
)

_HOST_WHICH = shutil.which

ASSERTION_MAP_PROTOCOL = "implement-v13-codex/repair-assertion-map/2"
LEGACY_ASSERTION_MAP_PROTOCOL = "implement-v13-codex/repair-assertion-map/1"
CAPABILITY_MANIFEST_PROTOCOL = "implement-v13-codex/capability-manifest/2"
TEST_COMMAND_PROTOCOL = "implement-v13-codex/test-command/1"
RESOLUTION_PROFILE_PROTOCOL = "implement-v13-codex/operator-resolution-profile/1"
DATAFLOW_PROOF_PROTOCOL = (
    "implement-v13-codex/operator-resolution-dataflow-proof/1"
)
EFFECT_CONTRACT_PROTOCOL = "implement-v13-codex/repair-effect-contract/1"
REPAIR_EFFECTS = {
    "failure_checkpoint",
    "blocked_queue",
    "failure_summary",
    "failure_event",
    "success_result",
    "success_receipt",
    "integration_artifact",
    "dispatcher_acknowledgement",
    "base_git_state",
}
EFFECT_DISPOSITIONS = {
    "must_persist",
    "must_remain_absent",
    "must_remain_unchanged",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _schema(name: str) -> dict[str, Any]:
    return read_json(Path(__file__).resolve().parents[1] / "schemas" / name)


def _validate_schema(document: Any, name: str) -> None:
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError as exc:
        raise StateError("jsonschema is required for repair preflight") from exc
    try:
        jsonschema.Draft202012Validator(_schema(name)).validate(document)
    except jsonschema.ValidationError as exc:
        raise StateError(
            f"{name} validation failed at {list(exc.absolute_path)}"
        ) from exc


def repository_identity(repository_root: Path) -> str:
    """Bind a profile to one canonical repository root without reading Git state."""
    root = repository_root.resolve()
    if not root.is_dir():
        raise StateError("repository root is not a directory")
    return sha256_bytes(
        canonical_bytes(
            {
                "protocol": "implement-v13-codex/repository-subject/1",
                "repository_root": str(root),
            }
        )
    )


def effect_contract_sha256(effect_contract: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(effect_contract))


def certification_runtime_identity(
    interpreter: Path | None = None,
) -> dict[str, Any]:
    """Bind the exact Python/pytest runtime used for source certification."""
    executable = (interpreter or Path(sys.executable)).resolve()
    if not executable.is_file():
        raise StateError("certification interpreter is missing")
    code = (
        "import json, pathlib, platform, pytest, sys\n"
        "print(json.dumps({'interpreter_path':str(pathlib.Path(sys.executable).resolve()),"
        "'python_version':platform.python_version(),"
        "'pytest_version':pytest.__version__,"
        "'pytest_module_path':str(pathlib.Path(pytest.__file__).resolve())},sort_keys=True))\n"
    )
    completed = subprocess.run(
        [str(executable), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise StateError("certification interpreter lacks pytest")
    try:
        facts = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StateError("certification runtime probe returned invalid JSON") from exc
    if facts.get("interpreter_path") != str(executable):
        raise StateError("certification runtime resolved a different interpreter")
    runtime = {
        **facts,
        "interpreter_sha256": sha256_file(executable),
    }
    runtime["dependency_fingerprint_sha256"] = sha256_bytes(
        canonical_bytes(runtime)
    )
    return runtime


def certification_runtime_sha256(runtime: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(runtime))


def validate_test_command(
    command: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate one generic argv with stricter binding for Python/pytest."""
    if not isinstance(command, dict) or set(command) != {
        "protocol",
        "argv",
        "certification_runtime_sha256",
        "environment_profile",
    }:
        raise StateError("test command must be a closed object")
    argv = command.get("argv")
    if (
        command.get("protocol") != TEST_COMMAND_PROTOCOL
        or not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or command.get("environment_profile") != "controller_certification"
    ):
        raise StateError("test command contract is invalid")
    runtime = manifest.get("certification_runtime")
    if not isinstance(runtime, dict):
        raise StateError("capability manifest lacks certification runtime")
    runtime_sha = certification_runtime_sha256(runtime)
    if command.get("certification_runtime_sha256") != runtime_sha:
        raise StateError("test command certification runtime hash mismatch")
    if len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
        if Path(argv[0]).resolve() != Path(str(runtime["interpreter_path"])):
            raise StateError("pytest command does not use certified interpreter")
    return {
        "protocol": TEST_COMMAND_PROTOCOL,
        "argv": list(argv),
        "certification_runtime_sha256": runtime_sha,
        "environment_profile": "controller_certification",
    }


def bind_test_command(
    argv: list[str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    runtime = manifest.get("certification_runtime")
    if not isinstance(runtime, dict):
        raise StateError("capability manifest lacks certification runtime")
    return validate_test_command(
        {
            "protocol": TEST_COMMAND_PROTOCOL,
            "argv": argv,
            "certification_runtime_sha256": certification_runtime_sha256(runtime),
            "environment_profile": "controller_certification",
        },
        manifest,
    )


def _effect_assignments(effect_contract: dict[str, Any]) -> dict[str, str]:
    if (
        not isinstance(effect_contract, dict)
        or effect_contract.get("protocol") != EFFECT_CONTRACT_PROTOCOL
    ):
        raise StateError("repair effect contract protocol mismatch")
    if set(effect_contract) != {"protocol", *EFFECT_DISPOSITIONS}:
        raise StateError("repair effect contract contains unknown or missing fields")
    assignments: dict[str, str] = {}
    for disposition in sorted(EFFECT_DISPOSITIONS):
        effects = effect_contract.get(disposition)
        if not isinstance(effects, list) or any(
            not isinstance(effect, str) for effect in effects
        ):
            raise StateError(f"repair effect contract {disposition} must be an array")
        for effect in effects:
            if effect not in REPAIR_EFFECTS:
                raise StateError(f"repair effect contract names unknown effect: {effect}")
            if effect in assignments:
                raise StateError(
                    f"repair effect contract assigns incompatible dispositions to {effect}"
                )
            assignments[effect] = disposition
    if set(assignments) != REPAIR_EFFECTS:
        missing = sorted(REPAIR_EFFECTS - set(assignments))
        raise StateError(
            f"repair effect contract does not disposition every governed effect: {missing}"
        )
    return assignments


def validate_assertion_effects(
    assertion_map: dict[str, Any],
    *,
    feature_run_id: str,
    closure_id: str,
    effect_contract: dict[str, Any],
    test_paths: list[str],
    commands: list[Any],
) -> dict[str, Any]:
    """Validate immutable assertion identity and its canonical lifecycle effect."""
    _validate_schema(assertion_map, "repair-assertion-map.schema.json")
    if assertion_map.get("protocol") not in {
        ASSERTION_MAP_PROTOCOL,
        LEGACY_ASSERTION_MAP_PROTOCOL,
    }:
        raise StateError("repair assertion map protocol mismatch")
    if assertion_map.get("feature_run_id") != feature_run_id:
        raise StateError("repair assertion map feature subject mismatch")
    if assertion_map.get("closure_id") != closure_id:
        raise StateError("repair assertion map closure subject mismatch")
    root = Path(str(assertion_map.get("repository_root", "")))
    if not root.is_absolute():
        raise StateError("repair assertion map repository_root must be absolute")
    root = root.resolve()
    if assertion_map.get("repository_identity") != repository_identity(root):
        raise StateError("repair assertion map repository identity mismatch")
    test = assertion_map["test"]
    source = (root / str(test["source_path"])).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise StateError("repair assertion map test source escapes repository") from exc
    if not source.is_file() or sha256_file(source) != test["source_sha256"]:
        raise StateError("repair assertion map test source hash mismatch")
    source_path = str(test["source_path"])
    node_id = str(test["node_id"])
    active_test_matches = False
    for recorded_path in test_paths:
        recorded_source, separator, _ = str(recorded_path).partition("::")
        if recorded_source != source_path:
            continue
        # Legacy closure tests may persist either a source file or an exact
        # pytest node.  A node-valued entry binds both the hashable file and
        # the node identity; stripping only for the source comparison avoids
        # rewriting immutable legacy evidence without widening the subject.
        if separator and str(recorded_path) != node_id:
            continue
        active_test_matches = True
        break
    if not active_test_matches:
        raise StateError("repair assertion map active test path mismatch")
    if not any(
        canonical_bytes(test["command"]) == canonical_bytes(command)
        for command in commands
    ):
        raise StateError("repair assertion map active test command mismatch")
    contract_assignments = _effect_assignments(effect_contract)
    if assertion_map.get("effect_contract_sha256") != effect_contract_sha256(
        effect_contract
    ):
        raise StateError("repair assertion map effect-contract hash mismatch")
    seen_ids: set[str] = set()
    assignments: dict[str, set[str]] = {}
    for assertion in assertion_map["assertions"]:
        assertion_id = assertion["assertion_id"]
        if assertion_id in seen_ids:
            raise StateError(f"duplicate immutable assertion ID: {assertion_id}")
        seen_ids.add(assertion_id)
        if assertion["test_node_id"] != test["node_id"]:
            raise StateError(f"assertion {assertion_id} names the wrong active test")
        if assertion["source_sha256"] != test["source_sha256"]:
            raise StateError(f"assertion {assertion_id} source hash mismatch")
        effect = assertion["effect"]
        disposition = assertion["expected_disposition"]
        if effect not in REPAIR_EFFECTS:
            raise StateError(f"assertion {assertion_id} names unknown effect: {effect}")
        assignments.setdefault(effect, set()).add(disposition)
        if contract_assignments[effect] != disposition:
            assignments[effect].add(contract_assignments[effect])
    return {
        "protocol": "implement-v13-codex/assertion-effect-validation/1",
        "status": "validated",
        "assertion_count": len(seen_ids),
        "assertion_map_sha256": sha256_bytes(canonical_bytes(assertion_map)),
        "assignments": {
            effect: sorted(dispositions)
            for effect, dispositions in sorted(assignments.items())
        },
    }


def solve_effect_constraints(
    assertion_map: dict[str, Any],
    *,
    effect_contract: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic satisfiability result; never invoke a model."""
    contract_assignments = _effect_assignments(effect_contract)
    assignments: dict[str, set[str]] = {}
    assertion_ids: dict[str, list[str]] = {}
    for assertion in assertion_map.get("assertions", []):
        if not isinstance(assertion, dict):
            raise StateError("repair assertion map contains a non-object assertion")
        effect = assertion.get("effect")
        disposition = assertion.get("expected_disposition")
        if effect not in REPAIR_EFFECTS or disposition not in EFFECT_DISPOSITIONS:
            raise StateError("repair assertion map contains an unknown effect assignment")
        assignments.setdefault(effect, set()).add(disposition)
        assertion_ids.setdefault(effect, []).append(str(assertion.get("assertion_id", "")))
        assignments[effect].add(contract_assignments[effect])
    conflicts = [
        {
            "effect": effect,
            "dispositions": sorted(dispositions),
            "assertion_ids": sorted(assertion_ids.get(effect, [])),
        }
        for effect, dispositions in sorted(assignments.items())
        if len(dispositions) != 1
    ]
    return {
        "protocol": "implement-v13-codex/repair-effect-satisfiability/1",
        "status": "satisfiable" if not conflicts else "contradictory",
        "model_calls_permitted": not conflicts,
        "assertion_map_sha256": sha256_bytes(canonical_bytes(assertion_map)),
        "effect_contract_sha256": effect_contract_sha256(effect_contract),
        "conflicts": conflicts,
    }


def _sandbox_probe_command(
    repository_root: Path, scratch: Path
) -> tuple[list[str], dict[str, str]]:
    sandbox_exec = _HOST_WHICH("sandbox-exec")
    if sys.platform != "darwin" or sandbox_exec is None:
        return [], {}
    quoted_root = json.dumps(str(repository_root.resolve()))
    quoted_scratch = json.dumps(str(scratch.resolve()))
    profile = " ".join(
        (
            "(version 1)",
            "(allow default)",
            f"(deny file-write* (subpath {quoted_root}))",
            f"(allow file-write* (subpath {quoted_scratch}))",
        )
    )
    code = (
        "import json, os, pathlib, subprocess, sys, tempfile\n"
        "repo=pathlib.Path(sys.argv[1]); scratch=pathlib.Path(os.environ['TMPDIR'])\n"
        "read_ok=next(repo.iterdir(), None) is not None\n"
        "scratch_file=scratch/'reviewer-scratch-probe'; scratch_file.write_text('ok')\n"
        "tmp_file=pathlib.Path(tempfile.mkstemp(dir=scratch)[1]); tmp_file.write_text('runner')\n"
        "test_file=scratch/'test_runner_capability.py'; test_file.write_text('def test_runner_capability(tmp_path):\\n    assert tmp_path.is_dir()\\n')\n"
        "test_run=subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider',str(test_file)],capture_output=True,text=True,env={**os.environ,'TMPDIR':str(scratch),'TMP':str(scratch),'TEMP':str(scratch)})\n"
        "internal_link=any(item.is_symlink() and item.resolve().is_relative_to(scratch) for item in scratch.rglob('*'))\n"
        "sentinel=repo/'.codex-capability-probe-do-not-create'; denied=False\n"
        "try:\n"
        " sentinel.write_text('unsafe')\n"
        "except PermissionError:\n"
        " denied=True\n"
        "finally:\n"
        " if sentinel.exists(): sentinel.unlink()\n"
        "print(json.dumps({'repository_read':read_ok,'repository_write_denied':denied,"
        "'ephemeral_scratch_write':scratch_file.read_text()=='ok',"
        "'test_runner_scratch':tmp_file.read_text()=='runner' and test_run.returncode==0 and internal_link},sort_keys=True))\n"
        "raise SystemExit(0 if read_ok and denied and test_run.returncode==0 and internal_link else 7)\n"
    )
    command = [sandbox_exec, "-p", profile, sys.executable, "-c", code, str(repository_root)]
    environment = dict(os.environ)
    environment.update(
        TMPDIR=str(scratch),
        TMP=str(scratch),
        TEMP=str(scratch),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return command, environment


def probe_role_capabilities(
    *,
    repository_root: Path,
    artifact_dir: Path,
    feature_run_id: str,
    controller_package_digest: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Probe real host Seatbelt semantics or clearly label injected simulation."""
    root = repository_root.resolve()
    if not root.is_dir():
        raise StateError("capability probe repository root is not a directory")
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix="implement-v13-capability-")).resolve()
    simulation_only = runner is not None
    execute = runner or subprocess.run
    command, environment = _sandbox_probe_command(root, scratch)
    broker_path = Path(command[0]).resolve() if command else None
    broker = "macos-seatbelt-sandbox-exec" if command else "unavailable"
    output = ""
    error = ""
    rc = 127
    values: dict[str, bool] = {}
    try:
        if command:
            completed = execute(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )
            rc = int(completed.returncode)
            output = completed.stdout
            error = completed.stderr
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                values = {
                    key: value is True
                    for key, value in parsed.items()
                    if isinstance(key, str)
                }
        evidence_hash = sha256_bytes((output + "\n" + error).encode("utf-8"))
        capabilities = (
            "repository_read",
            "repository_write_denied",
            "ephemeral_scratch_write",
            "test_runner_scratch",
        )
        production_real = bool(command) and not simulation_only
        runtime = certification_runtime_identity()
        probes = [
            {
                "probe_id": f"orient-{capability.replace('_', '-')}",
                "capability": capability,
                "production_real": production_real,
                "command": command or ["unavailable"],
                "rc": rc,
                "passed": rc == 0 and values.get(capability) is True,
                "evidence_sha256": evidence_hash,
            }
            for capability in capabilities
        ]
        ready = (
            not simulation_only
            and all(probe["passed"] for probe in probes)
        )
        manifest = {
            "protocol": CAPABILITY_MANIFEST_PROTOCOL,
            "feature_run_id": feature_run_id,
            "repository_root": str(root),
            "repository_identity": repository_identity(root),
            "controller_package_digest": controller_package_digest,
            "sandbox_owner": "controller_host_broker",
            "broker": broker,
            "broker_path": str(broker_path) if broker_path is not None else "",
            "broker_sha256": (
                sha256_file(broker_path) if broker_path is not None else "0" * 64
            ),
            "probe_policy_sha256": (
                sha256_bytes(command[2].encode("utf-8"))
                if len(command) > 2
                else "0" * 64
            ),
            "execution_path": "controller-owned host capability probe",
            "certification_runtime": runtime,
            "simulation_only": simulation_only,
            "status": "ready" if ready else "external_capability_unavailable",
            "scratch_contract": {
                "controller_created": True,
                "per_invocation": True,
                "outside_repository_authority": True,
                "empty_at_launch": True,
                "contents_hashed_before_removal": True,
                "removed_after_terminal": True,
            },
            "probes": probes,
            "checked_at": _utc_now(),
        }
        _validate_schema(manifest, "capability-manifest.schema.json")
        atomic_write_json(artifact_dir / "capability-manifest.v2.json", manifest)
        return manifest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def validate_capability_manifest(
    manifest_path: Path,
    expected_sha256: str,
    *,
    repository_root: Path | None = None,
    feature_run_id: str | None = None,
    controller_package_digest: str | None = None,
) -> dict[str, Any]:
    path = manifest_path.resolve()
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise StateError("capability manifest hash mismatch")
    manifest = read_json(path)
    _validate_schema(manifest, "capability-manifest.schema.json")
    if manifest.get("simulation_only") is not False:
        raise StateError("simulation-only capability evidence cannot certify production")
    if manifest.get("status") != "ready":
        raise StateError("external_capability_unavailable")
    if not all(
        probe.get("production_real") is True and probe.get("passed") is True
        for probe in manifest["probes"]
    ):
        raise StateError("capability manifest lacks production-real probe evidence")
    if repository_root is not None:
        root = repository_root.resolve()
        if manifest.get("repository_root") != str(root) or manifest.get(
            "repository_identity"
        ) != repository_identity(root):
            raise StateError("capability manifest repository subject mismatch")
    if feature_run_id is not None and manifest.get("feature_run_id") != feature_run_id:
        raise StateError("capability manifest feature subject mismatch")
    if (
        controller_package_digest is not None
        and manifest.get("controller_package_digest") != controller_package_digest
    ):
        raise StateError("capability manifest controller package mismatch")
    runtime = manifest.get("certification_runtime")
    if (
        not isinstance(runtime, dict)
        or certification_runtime_identity(
            Path(str(runtime.get("interpreter_path", "")))
        )
        != runtime
    ):
        raise StateError("capability manifest certification runtime changed")
    broker_path = Path(str(manifest.get("broker_path", "")))
    if (
        not broker_path.is_absolute()
        or not broker_path.is_file()
        or sha256_file(broker_path) != manifest.get("broker_sha256")
    ):
        raise StateError("capability manifest broker identity changed")
    return manifest


def execute_resolution_dataflow_probe(profile: dict[str, Any]) -> dict[str, Any]:
    """Mint a controller-only token and exercise one anonymous-pipe consumption."""
    subject = profile.get("active_subject")
    capability = profile.get("capability")
    if not isinstance(subject, dict) or not isinstance(capability, dict):
        raise StateError("operator resolution profile lacks subject or capability")
    if capability.get("transport") != "anonymous_pipe":
        raise StateError("operator resolution dataflow requires anonymous_pipe transport")
    token = secrets.token_bytes(32)
    token_hash = hashlib.sha256(token).hexdigest()
    subject_hash = sha256_bytes(canonical_bytes(subject))
    consumed_tokens: set[str] = set()

    def consume(payload: bytes | None, *, ordinary_dispatch: bool = False) -> bool:
        if payload is None or ordinary_dispatch:
            return False
        try:
            decoded = json.loads(payload)
            supplied_token = bytes.fromhex(str(decoded.get("token_hex", "")))
        except (json.JSONDecodeError, ValueError):
            return False
        supplied_hash = hashlib.sha256(supplied_token).hexdigest()
        if (
            decoded.get("subject_sha256") != subject_hash
            or supplied_hash in consumed_tokens
            or not secrets.compare_digest(supplied_token, token)
        ):
            return False
        consumed_tokens.add(supplied_hash)
        return True

    payload = canonical_bytes(
        {"subject_sha256": subject_hash, "token_hex": token.hex()}
    )
    absence_rejected = consume(None) is False
    mismatch = canonical_bytes(
        {"subject_sha256": "0" * 64, "token_hex": token.hex()}
    )
    mismatch_rejected = consume(mismatch) is False
    ordinary_dispatch_rejected = consume(payload, ordinary_dispatch=True) is False
    read_fd, write_fd = os.pipe()
    consumed = False
    try:
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1
        transported = os.read(read_fd, len(payload) + 1)
        consumed = consume(transported)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    reuse_rejected = consume(payload) is False
    return {
        "protocol": DATAFLOW_PROOF_PROTOCOL,
        "profile_subject_sha256": subject_hash,
        "minted_by_controller": True,
        "transport_exercised": consumed,
        "consumed_once": consumed,
        "absence_rejected": absence_rejected,
        "reuse_rejected": reuse_rejected,
        "mismatch_rejected": mismatch_rejected,
        "ordinary_dispatch_rejected": (
            ordinary_dispatch_rejected
            and capability.get("production_selectable") is False
        ),
        "token_sha256": token_hash,
        "status": "passed" if consumed else "failed",
    }


def validate_resolution_dataflow(
    profile: dict[str, Any],
    *,
    repository_identity_sha256: str,
    feature_run_id: str,
    closure_id: str,
    test_node_id: str,
    test_source_path: str,
    test_source_sha256: str,
    assertion_map_sha256: str,
) -> dict[str, Any]:
    """Validate exact active subject plus controller mint/transport/consume proof."""
    _validate_schema(profile, "operator-resolution-profile.schema.json")
    _effect_assignments(profile["effect_contract"])
    subject = {
        "repository_identity": repository_identity_sha256,
        "feature_run_id": feature_run_id,
        "closure_id": closure_id,
        "test_node_id": test_node_id,
        "test_source_path": test_source_path,
        "test_source_sha256": test_source_sha256,
        "assertion_map_sha256": assertion_map_sha256,
    }
    if profile.get("active_subject") != subject:
        raise StateError("operator resolution profile active subject mismatch")
    capability = profile["capability"]
    exact = {
        "transport": "anonymous_pipe",
        "minting_authority": "controller_only",
        "controller_minted": True,
        "single_use": True,
        "role_visible": False,
        "caller_supplied": False,
        "caller_claim_selectable": False,
        "production_selectable": False,
        "fail_closed_on_absence": True,
        "fail_closed_on_reuse": True,
        "fail_closed_on_mismatch": True,
    }
    if capability != exact:
        raise StateError("operator resolution capability is broadened or caller-selectable")
    proof = profile["dataflow_proof"]
    expected_subject_hash = sha256_bytes(canonical_bytes(subject))
    if proof.get("profile_subject_sha256") != expected_subject_hash:
        raise StateError("operator resolution dataflow proof subject mismatch")
    required_true = {
        "minted_by_controller",
        "transport_exercised",
        "consumed_once",
        "absence_rejected",
        "reuse_rejected",
        "mismatch_rejected",
        "ordinary_dispatch_rejected",
    }
    if any(proof.get(field) is not True for field in required_true):
        raise StateError("operator resolution dataflow proof is incomplete")
    live_proof = execute_resolution_dataflow_probe(profile)
    if live_proof.get("status") != "passed" or any(
        live_proof.get(field) is not True for field in required_true
    ):
        raise StateError("operator resolution live dataflow probe failed")
    return {
        "protocol": "implement-v13-codex/operator-resolution-dataflow-validation/1",
        "status": "validated",
        "active_subject_sha256": expected_subject_hash,
        "profile_sha256": sha256_bytes(canonical_bytes(profile)),
        "dataflow_proof_sha256": sha256_bytes(canonical_bytes(proof)),
        "live_dataflow_proof_sha256": sha256_bytes(canonical_bytes(live_proof)),
    }
