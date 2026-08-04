#!/usr/bin/env python3
"""Run a JSON-defined Codex phase flow with a fail-closed empty-context debug mode."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import uuid


FLOW_PROTOCOL = "codex-phase-flow/1"
CHILD_PROTOCOL = "phase-child/1"
RESULT_PROTOCOL = "codex-phase-flow/debug-result/1"
RECEIPT_PROTOCOL = "codex-phase-flow/process-receipt/1"
STATE_PROTOCOL = "codex-phase-flow/checkpoint/1"
PREFLIGHT_PROTOCOL = "codex-phase-flow/controller-preflight/1"
_CODEX_VERSION = re.compile(r"^codex-cli\s+\S+")
_TERMINAL_ERRORS = {"turn.failed", "error"}
_ALLOWED_ITEM_TYPES = {"reasoning", "agent_message", "file_change"}
_ALLOWED_EVENT_TYPES = {"thread.started", "turn.started", "item.started", "item.completed", "turn.completed"}
_FORBIDDEN_EVIDENCE = ("skill.md", "/skills/", "\\skills\\", "agents.md", "claude.md")
_OPTIONAL_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
_REQUIRED_CHILD_ENV_KEYS = {"HOME", "CODEX_HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"}
_PACKAGE = Path(__file__).resolve().parent.parent


class PhaseFlowError(RuntimeError):
    """Raised when a phase-flow contract is incomplete, contaminated, or stale."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PhaseFlowError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseFlowError(f"invalid JSON {label}: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise PhaseFlowError(f"JSON must be an object: {label}")
    return value


def _read_object(path: Path) -> dict[str, Any]:
    try:
        return _decode_object(path.read_bytes(), str(path))
    except OSError as exc:
        raise PhaseFlowError(f"unreadable JSON {path}: {type(exc).__name__}") from None


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def _schema_validate(value: Mapping[str, Any], name: str) -> None:
    try:
        import jsonschema
    except ImportError:
        raise PhaseFlowError("jsonschema is required") from None
    schema = _read_object(_PACKAGE / "schemas" / name)
    try:
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        raise PhaseFlowError(f"schema rejection at {location}: {exc.message}") from None


def _validate_flow(flow: Mapping[str, Any]) -> str:
    _schema_validate(flow, "flow.schema.json")
    ids = [phase["id"] for phase in flow["phases"]]
    if len(ids) != len(set(ids)):
        raise PhaseFlowError("phase ids must be unique")
    context_catalog = flow.get("context_catalog", {})
    mode = "debug" if not context_catalog else "project"
    if mode == "debug":
        if flow["prompt_catalog"]:
            raise PhaseFlowError("debug mode requires an empty prompt_catalog")
        if "project_root" in flow:
            raise PhaseFlowError("debug mode forbids project_root")
        for phase in flow["phases"]:
            if "prompt_id" in phase:
                raise PhaseFlowError("debug phases forbid prompt_id")
            if phase["sandbox"] != "workspace-write":
                raise PhaseFlowError("debug phases require workspace-write for the identity marker")
    else:
        for prompt_id, prompt in flow["prompt_catalog"].items():
            unknown = set(prompt["context_ids"]) - set(context_catalog)
            if unknown:
                raise PhaseFlowError(f"prompt {prompt_id} references unknown context ids")
        for phase in flow["phases"]:
            if phase.get("prompt_id") not in flow["prompt_catalog"]:
                raise PhaseFlowError(f"project phase {phase['id']} has no valid prompt_id")
    return mode


def _catalog_hash(flow: Mapping[str, Any]) -> str:
    catalog = [
        {key: phase[key] for key in ("id", "phase", "phase_detail", "model", "reasoning", "sandbox")}
        for phase in flow["phases"]
    ]
    return _sha_bytes(json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode())


def _resolve_codex() -> tuple[Path, str, str]:
    candidate = shutil.which("codex")
    if candidate is None:
        raise PhaseFlowError("codex executable is unavailable")
    executable = Path(candidate).resolve()
    completed = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=15, check=False)
    version = completed.stdout.strip()
    if completed.returncode != 0 or not _CODEX_VERSION.match(version):
        raise PhaseFlowError("resolved executable did not identify as codex-cli")
    return executable, version, _sha_file(executable)


def _controller_preflight(
    flow_path: Path, run_dir: Path
) -> tuple[dict[str, Any], bytes, tuple[Path, str, str], dict[str, Any]]:
    """Validate controller prerequisites without creating durable run state."""

    if run_dir.exists():
        raise PhaseFlowError("run directory already exists")
    _assert_outside_repository(run_dir)
    try:
        flow_bytes = flow_path.read_bytes()
    except OSError as exc:
        raise PhaseFlowError(f"flow is unreadable: {type(exc).__name__}") from None
    flow = _decode_object(flow_bytes, str(flow_path))
    mode = _validate_flow(flow)
    executable_identity = _resolve_codex()
    auth = _source_codex_home() / "auth.json"
    _assert_regular_private_auth(auth)
    required_files = [
        _PACKAGE / "builtins" / "identity-marker.prompt.txt",
        _PACKAGE / "schemas" / "flow.schema.json",
        _PACKAGE / "schemas" / "phase-child.schema.json",
        _PACKAGE / "schemas" / "controller-preflight.schema.json",
        Path(__file__).with_name("supervised_child.py"),
    ]
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise PhaseFlowError(f"controller support files missing: {','.join(missing)}")
    try:
        jsonschema_version = importlib.metadata.version("jsonschema")
    except importlib.metadata.PackageNotFoundError:
        raise PhaseFlowError("jsonschema package metadata is unavailable") from None
    executable, version, executable_hash = executable_identity
    report = {
        "protocol": PREFLIGHT_PROTOCOL,
        "status": "ready",
        "mode": mode,
        "flow_id": flow["flow_id"],
        "flow_spec_sha256": _sha_bytes(flow_bytes),
        "models": sorted({str(phase["model"]) for phase in flow["phases"]}),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "jsonschema": jsonschema_version,
        "codex_executable": str(executable),
        "codex_version": version,
        "codex_executable_sha256": executable_hash,
        "auth_file_private": True,
        "support_files_sha256": {str(path.relative_to(_PACKAGE)): _sha_file(path) for path in required_files},
        "live_model_probe": "first_child",
        "quota_headroom": "not_exposed_by_cli",
        "checked_at": _now(),
    }
    _schema_validate(report, "controller-preflight.schema.json")
    return flow, flow_bytes, executable_identity, report


def _process_fingerprint(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    started = completed.stdout.strip()
    if completed.returncode != 0 or not started:
        raise PhaseFlowError(f"cannot fingerprint child pid {pid}")
    return f"{pid}:{started}"


def _terminate_owned_group(process: subprocess.Popen[bytes], pgid: int, fingerprint: str) -> None:
    if process.poll() is not None:
        return
    try:
        if os.getpgid(process.pid) != pgid or _process_fingerprint(process.pid) != fingerprint:
            raise PhaseFlowError("refuse to terminate an unowned process group")
    except ProcessLookupError:
        return
    os.killpg(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            if os.getpgid(process.pid) == pgid and _process_fingerprint(process.pid) == fingerprint:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _process_matches_receipt(receipt: Mapping[str, Any]) -> bool:
    try:
        pid = int(receipt["pid"])
        pgid = int(receipt["process_group_id"])
        fingerprint = str(receipt["process_start_fingerprint"])
        return os.getpgid(pid) == pgid and _process_fingerprint(pid) == fingerprint
    except (KeyError, TypeError, ValueError, ProcessLookupError, PhaseFlowError):
        return False


def _pid_exists(receipt: Mapping[str, Any]) -> bool:
    try:
        os.kill(int(receipt["pid"]), 0)
        return True
    except (KeyError, TypeError, ValueError, ProcessLookupError):
        return False


def _terminate_receipt_process(receipt: Mapping[str, Any]) -> None:
    if not _process_matches_receipt(receipt):
        if _pid_exists(receipt):
            raise PhaseFlowError("refuse to terminate a process whose fingerprint changed")
        return
    pid = int(receipt["pid"])
    pgid = int(receipt["process_group_id"])
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    if _process_matches_receipt(receipt):
        os.killpg(pgid, signal.SIGKILL)


def _source_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home().resolve() / ".codex"


def _assert_regular_private_auth(auth: Path) -> None:
    try:
        details = auth.lstat()
    except OSError as exc:
        raise PhaseFlowError(f"Codex auth file unavailable: {type(exc).__name__}") from None
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise PhaseFlowError("Codex auth must be a regular, non-symlink file")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise PhaseFlowError("Codex auth permissions are broader than 0600")


def _prepare_isolation(run_dir: Path) -> dict[str, Path]:
    root = run_dir / ".child-runtime"
    if root.exists():
        raise PhaseFlowError("child runtime already exists; refuse ambiguous credential state")
    home = root / "home"
    codex_home = root / "codex-home"
    workspace = root / "workspace"
    temporary = root / "tmp"
    for path in (root, home, codex_home, workspace, workspace / "markers", temporary):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    source_auth = _source_codex_home() / "auth.json"
    _assert_regular_private_auth(source_auth)
    (codex_home / "auth.json").symlink_to(source_auth)
    return {"root": root, "home": home, "codex_home": codex_home, "workspace": workspace, "temporary": temporary}


def _remove_isolation(paths: Mapping[str, Path], run_dir: Path) -> None:
    root = Path(os.path.abspath(str(paths["root"])))
    expected = Path(os.path.abspath(str(run_dir / ".child-runtime")))
    lexical_run_dir = Path(os.path.abspath(str(run_dir)))
    if root != expected or root.parent != lexical_run_dir:
        raise PhaseFlowError("refuse unsafe child-runtime cleanup")
    try:
        details = root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise PhaseFlowError("refuse non-directory or symlink child-runtime cleanup")
    shutil.rmtree(root)


def _identity(run_nonce: str, ordinal: int, unit_nonce: str) -> dict[str, Any]:
    return {
        "protocol": CHILD_PROTOCOL,
        "run_nonce": run_nonce,
        "unit_index": ordinal,
        "unit_nonce": unit_nonce,
        "statement": "identity marker created",
    }


def _compile_prompt(identity: Mapping[str, Any]) -> tuple[bytes, bytes]:
    template = (_PACKAGE / "builtins" / "identity-marker.prompt.txt").read_bytes()
    marker = b"__IDENTITY_JSON__"
    if template.count(marker) != 1:
        raise PhaseFlowError("identity prompt template placeholder count is not one")
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    prompt = template.replace(marker, payload)
    lowered = prompt.lower()
    for forbidden in _FORBIDDEN_EVIDENCE:
        if forbidden.encode() in lowered:
            raise PhaseFlowError(f"compiled debug prompt contains forbidden evidence: {forbidden}")
    return template, prompt


def _child_argv(executable: Path, paths: Mapping[str, Path], phase: Mapping[str, Any], final: Path) -> list[str]:
    return [
        str(executable), "exec", "-C", str(paths["workspace"]), "--skip-git-repo-check",
        "--ephemeral", "--ignore-user-config", "--ignore-rules", "--disable", "plugins",
        "--strict-config", "-m", phase["model"],
        "-c", f'model_reasoning_effort="{phase["reasoning"]}"',
        "-c", 'approval_policy="never"',
        "-c", 'cli_auth_credentials_store="file"',
        "-c", "skills.bundled.enabled=false",
        "-c", "skills.include_instructions=false",
        "-c", "project_doc_max_bytes=0",
        "--sandbox", "workspace-write",
        "--output-schema", str(_PACKAGE / "schemas" / "phase-child.schema.json"),
        "-o", str(final), "--json", "-",
    ]


def _child_env(paths: Mapping[str, Path]) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    environment.update({
        "HOME": str(paths["home"]),
        "CODEX_HOME": str(paths["codex_home"]),
        "TMPDIR": str(paths["temporary"]),
        "XDG_CONFIG_HOME": str(paths["home"] / ".config"),
        "XDG_DATA_HOME": str(paths["home"] / ".local" / "share"),
        "XDG_CACHE_HOME": str(paths["home"] / ".cache"),
    })
    return environment


def _launch_contract(
    argv: Sequence[str], paths: Mapping[str, Path], environment: Mapping[str, str]
) -> dict[str, Any]:
    environment_bytes = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
    return {
        "protocol": "phase-launch/1",
        "argv": list(argv),
        "cwd": str(paths["workspace"]),
        "environment_keys": sorted(environment),
        "environment_sha256": _sha_bytes(environment_bytes),
        "controller_sha256": _sha_file(Path(__file__)),
        "supervisor_sha256": _sha_file(Path(__file__).with_name("supervised_child.py")),
    }


def _assert_outside_repository(run_dir: Path) -> None:
    resolved = run_dir.resolve()
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise PhaseFlowError("run directory must be outside every Git repository")
        for rules_name in ("AGENTS.md", "CLAUDE.md", "claude.md"):
            if (ancestor / rules_name).exists():
                raise PhaseFlowError("run directory must be outside repository instruction roots")


def _assert_private_run_dir(run_dir: Path) -> None:
    _assert_outside_repository(run_dir)
    try:
        details = run_dir.lstat()
    except OSError as exc:
        raise PhaseFlowError(f"run directory unavailable: {type(exc).__name__}") from None
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise PhaseFlowError("run directory must be a real directory")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise PhaseFlowError("run directory permissions are broader than 0700")


def _event_metrics(path: Path) -> dict[str, int]:
    metrics = {
        "command_executions": 0,
        "forbidden_evidence_events": 0,
        "unexpected_item_events": 0,
    }
    command_ids: set[str] = set()
    if not path.exists():
        return metrics
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            metrics["unexpected_item_events"] += 1
            continue
        if not isinstance(event, dict):
            metrics["unexpected_item_events"] += 1
            continue
        serialized = json.dumps(event, sort_keys=True).lower()
        if any(forbidden in serialized for forbidden in _FORBIDDEN_EVIDENCE):
            metrics["forbidden_evidence_events"] += 1
        if event.get("type") in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                metrics["unexpected_item_events"] += 1
                continue
            kind = item.get("type")
            if kind == "command_execution":
                command_ids.add(str(item.get("id", "<missing>")))
            elif kind not in _ALLOWED_ITEM_TYPES:
                metrics["unexpected_item_events"] += 1
    metrics["command_executions"] = len(command_ids)
    return metrics


def _inspect_events(path: Path, expected_marker: Path | None = None) -> tuple[str, dict[str, int], set[str]]:
    threads: set[str] = set()
    event_types: set[str] = set()
    file_change_ids: set[str] = set()
    completed_file_change_ids: set[str] = set()
    file_change_payloads: dict[str, str] = {}
    started_item_ids: set[str] = set()
    completed_item_ids: set[str] = set()
    event_sequence: list[str] = []
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise PhaseFlowError(f"Codex stdout line {line_number} is not JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise PhaseFlowError(f"Codex stdout line {line_number} is not an event")
        serialized = json.dumps(event, sort_keys=True).lower()
        for forbidden in _FORBIDDEN_EVIDENCE:
            if forbidden in serialized:
                raise PhaseFlowError(f"child evidence contains forbidden discovery token: {forbidden}")
        event_type = event["type"]
        event_types.add(event_type)
        event_sequence.append(event_type)
        if event_type not in _ALLOWED_EVENT_TYPES and event_type not in _TERMINAL_ERRORS:
            raise PhaseFlowError(f"forbidden child event type: {event_type}")
        if event_type == "thread.started":
            if not isinstance(event.get("thread_id"), str) or not event["thread_id"]:
                raise PhaseFlowError("thread.started has no thread id")
            threads.add(event["thread_id"])
        if event_type in _TERMINAL_ERRORS:
            raise PhaseFlowError(f"child reported terminal error: {event_type}")
        if event_type == "turn.completed":
            observed = event.get("usage", {})
            if not isinstance(observed, dict):
                raise PhaseFlowError("turn.completed usage is not an object")
            for key in usage:
                value = observed.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise PhaseFlowError(f"turn.completed usage is invalid: {key}")
                usage[key] += value
        if event_type in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in _ALLOWED_ITEM_TYPES:
                kind = item.get("type") if isinstance(item, dict) else None
                raise PhaseFlowError(f"forbidden child item type: {kind}")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise PhaseFlowError("child item has no id")
            if event_type == "item.started":
                if item_id in started_item_ids:
                    raise PhaseFlowError("child item started more than once")
                started_item_ids.add(item_id)
            else:
                if item_id in completed_item_ids:
                    raise PhaseFlowError("child item completed more than once")
                completed_item_ids.add(item_id)
            if item["type"] == "file_change":
                file_change_ids.add(item_id)
                if event_type == "item.completed":
                    completed_file_change_ids.add(item_id)
                changes = item.get("changes")
                if not isinstance(changes, list) or not changes:
                    raise PhaseFlowError("file_change has no changes")
                serialized_changes = json.dumps(changes, sort_keys=True, separators=(",", ":"))
                if item_id in file_change_payloads and file_change_payloads[item_id] != serialized_changes:
                    raise PhaseFlowError("file_change payload changed during its lifecycle")
                file_change_payloads[item_id] = serialized_changes
                for change in changes:
                    if not isinstance(change, dict) or change.get("kind") != "add":
                        raise PhaseFlowError("file_change targeted an unexpected path")
                    observed = Path(str(change.get("path", "")))
                    if expected_marker is None:
                        if observed.name != "identity.json":
                            raise PhaseFlowError("file_change targeted an unexpected path")
                    elif observed.resolve() != expected_marker.resolve():
                        raise PhaseFlowError("file_change targeted an unexpected path")
    if len(threads) != 1:
        raise PhaseFlowError("child evidence does not identify exactly one thread")
    if event_sequence.count("thread.started") != 1 or event_sequence.count("turn.started") != 1 or event_sequence.count("turn.completed") != 1:
        raise PhaseFlowError("child evidence has invalid thread or turn cardinality")
    thread_index = event_sequence.index("thread.started")
    turn_start_index = event_sequence.index("turn.started")
    turn_complete_index = event_sequence.index("turn.completed")
    if thread_index != 0 or not thread_index < turn_start_index < turn_complete_index or turn_complete_index != len(event_sequence) - 1:
        raise PhaseFlowError("child evidence has invalid event ordering")
    if not started_item_ids <= completed_item_ids:
        raise PhaseFlowError("child evidence has an incomplete item lifecycle")
    if len(threads) != 1 or "turn.completed" not in event_types:
        raise PhaseFlowError("child evidence is not terminal-success from one thread")
    if len(file_change_ids) != 1 or completed_file_change_ids != file_change_ids or not file_change_ids <= started_item_ids:
        raise PhaseFlowError("child evidence does not contain exactly one completed file change")
    return next(iter(threads)), usage, event_types


def _workspace_files(workspace: Path) -> list[str]:
    return sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file())


def _receipt_path(run_dir: Path, ordinal: int) -> Path:
    return run_dir / "receipts" / f"{ordinal:03d}.json"


def _unit_dir(run_dir: Path, ordinal: int, phase_id: str) -> Path:
    return run_dir / "units" / f"{ordinal:03d}-{phase_id}"


def _run_unit(state: dict[str, Any], flow: Mapping[str, Any], run_dir: Path, paths: Mapping[str, Path], ordinal: int, timeout: int) -> dict[str, Any]:
    phase = flow["phases"][ordinal]
    unit_dir = _unit_dir(run_dir, ordinal, phase["id"])
    unit_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    marker = paths["workspace"] / "markers" / "identity.json"
    if marker.exists():
        marker.unlink()
    unit_nonce = uuid.uuid4().hex
    identity = _identity(state["run_nonce"], ordinal, unit_nonce)
    _schema_validate(identity, "phase-child.schema.json")
    template, prompt = _compile_prompt(identity)
    prompt_path = unit_dir / "prompt.txt"
    runtime_io = paths["root"] / "io" / f"{ordinal:03d}-{unit_nonce}"
    runtime_io.mkdir(mode=0o700, parents=True)
    raw_stdout = runtime_io / "stdout.jsonl"
    raw_stderr = runtime_io / "stderr.txt"
    raw_final = runtime_io / "final.json"
    child_spec_path = runtime_io / "child-spec.json"
    release_path = runtime_io / "release"
    exit_path = runtime_io / "exit.json"
    stdout_path = unit_dir / "stdout.jsonl"
    stderr_path = unit_dir / "stderr.txt"
    final_path = unit_dir / "final.json"
    launch_path = unit_dir / "launch.json"
    _write_bytes(prompt_path, prompt)
    executable = Path(state["codex_executable"])
    argv = _child_argv(executable, paths, phase, raw_final)
    child_environment = _child_env(paths)
    launch = _launch_contract(argv, paths, child_environment)
    _write_json(launch_path, launch)
    _write_json(child_spec_path, {"argv": argv, "release_timeout_seconds": 30})
    receipt: dict[str, Any] = {
        "protocol": RECEIPT_PROTOCOL,
        "status": "prepared",
        "run_id": state["run_id"],
        "ordinal": ordinal,
        "phase_id": phase["id"],
        "phase": phase["phase"],
        "phase_detail": phase["phase_detail"],
        "unit_nonce": unit_nonce,
        "flow_spec_sha256": state["flow_spec_sha256"],
        "catalog_sha256": state["catalog_sha256"],
        "codex_executable_sha256": state["codex_executable_sha256"],
        "controller_sha256": state["controller_sha256"],
        "supervisor_sha256": state["supervisor_sha256"],
        "launch_sha256": _sha_file(launch_path),
        "output_schema_sha256": _sha_file(_PACKAGE / "schemas" / "phase-child.schema.json"),
        "template_sha256": _sha_bytes(template),
        "compiled_prompt_sha256": _sha_bytes(prompt),
        "model": phase["model"],
        "reasoning": phase["reasoning"],
        "sandbox": phase["sandbox"],
        "context_ids": [],
        "context_bytes": 0,
        "command_executions": 0,
        "document_reads": 0,
        "forbidden_evidence_events": 0,
        "unexpected_item_events": 0,
        "started_at": _now(),
        "status_history": [{"status": "prepared", "at": _now()}],
    }
    _write_json(_receipt_path(run_dir, ordinal), receipt)
    process: subprocess.Popen[bytes] | None = None
    pgid: int | None = None
    fingerprint: str | None = None
    try:
        with prompt_path.open("rb") as input_stream, raw_stdout.open("wb") as stdout, raw_stderr.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("supervised_child.py")),
                    str(child_spec_path),
                    str(release_path),
                    str(exit_path),
                ],
                stdin=input_stream,
                stdout=stdout,
                stderr=stderr,
                env=child_environment,
                cwd=paths["workspace"],
                start_new_session=True,
            )
            pgid = os.getpgid(process.pid)
            fingerprint = _process_fingerprint(process.pid)
            receipt.update({
                "status": "spawned_unconfirmed",
                "pid": process.pid,
                "process_group_id": pgid,
                "process_start_fingerprint": fingerprint,
            })
            receipt["status_history"].append({"status": "spawned_unconfirmed", "at": _now()})
            _write_json(_receipt_path(run_dir, ordinal), receipt)
            _write_bytes(release_path, b"release\n")
            receipt["status"] = "running"
            receipt["status_history"].append({"status": "running", "at": _now()})
            _write_json(_receipt_path(run_dir, ordinal), receipt)
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_owned_group(process, pgid, fingerprint)
                raise PhaseFlowError(f"unit {ordinal} timed out")
        receipt.update(_event_metrics(raw_stdout))
        receipt["document_reads"] = receipt["command_executions"] + receipt["forbidden_evidence_events"]
        _write_json(_receipt_path(run_dir, ordinal), receipt)
        if return_code != 0:
            raise PhaseFlowError(f"unit {ordinal} exited {return_code}")
        exit_value = _read_object(exit_path)
        if exit_value != {"exit_code": 0}:
            raise PhaseFlowError(f"unit {ordinal} supervised exit receipt mismatch")
        if raw_stderr.read_bytes():
            raise PhaseFlowError(f"unit {ordinal} wrote stderr")
        thread_id, usage, event_types = _inspect_events(raw_stdout, marker)
        final = _read_object(raw_final)
        marker_value = _read_object(marker)
        if final != identity or marker_value != identity:
            raise PhaseFlowError(f"unit {ordinal} identity mismatch")
        files = _workspace_files(paths["workspace"])
        if files != ["markers/identity.json"]:
            raise PhaseFlowError(f"unit {ordinal} changed unexpected workspace files: {files}")
        marker_archive = unit_dir / "marker.json"
        _write_bytes(stdout_path, raw_stdout.read_bytes())
        _write_bytes(stderr_path, raw_stderr.read_bytes())
        _write_bytes(final_path, raw_final.read_bytes())
        _write_bytes(marker_archive, marker.read_bytes())
        shutil.rmtree(runtime_io)
        receipt.update({
            "status": "succeeded",
            "thread_id": thread_id,
            "event_types": sorted(event_types),
            "usage": usage,
            "stdout_sha256": _sha_file(stdout_path),
            "stderr_sha256": _sha_file(stderr_path),
            "final_sha256": _sha_file(final_path),
            "marker_sha256": _sha_file(marker_archive),
            "completed_at": _now(),
        })
        receipt["status_history"].append({"status": "succeeded", "at": receipt["completed_at"]})
        _write_json(_receipt_path(run_dir, ordinal), receipt)
        return receipt
    except Exception as exc:
        if process is not None and process.poll() is None:
            if pgid is not None and fingerprint is not None:
                _terminate_owned_group(process, pgid, fingerprint)
            else:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if raw_stdout.exists():
            receipt.update(_event_metrics(raw_stdout))
            receipt["document_reads"] = receipt["command_executions"] + receipt["forbidden_evidence_events"]
        receipt.update({"status": "failed", "error_type": type(exc).__name__, "completed_at": _now()})
        receipt["status_history"].append({"status": "failed", "at": receipt["completed_at"]})
        _write_json(_receipt_path(run_dir, ordinal), receipt)
        raise


def _validate_succeeded_receipt(receipt: Mapping[str, Any], state: Mapping[str, Any], flow: Mapping[str, Any], ordinal: int, run_dir: Path) -> str:
    phase = flow["phases"][ordinal]
    expected = {
        "protocol": RECEIPT_PROTOCOL,
        "status": "succeeded",
        "run_id": state["run_id"],
        "ordinal": ordinal,
        "phase_id": phase["id"],
        "phase": phase["phase"],
        "phase_detail": phase["phase_detail"],
        "flow_spec_sha256": state["flow_spec_sha256"],
        "catalog_sha256": state["catalog_sha256"],
        "codex_executable_sha256": state["codex_executable_sha256"],
        "controller_sha256": state["controller_sha256"],
        "supervisor_sha256": state["supervisor_sha256"],
        "output_schema_sha256": _sha_file(_PACKAGE / "schemas" / "phase-child.schema.json"),
        "model": phase["model"],
        "reasoning": phase["reasoning"],
        "sandbox": phase["sandbox"],
        "context_ids": [],
        "context_bytes": 0,
        "command_executions": 0,
        "document_reads": 0,
        "forbidden_evidence_events": 0,
        "unexpected_item_events": 0,
    }
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    if mismatches or not isinstance(receipt.get("thread_id"), str):
        raise PhaseFlowError(f"receipt {ordinal} contract mismatch: {','.join(mismatches)}")
    pid = receipt.get("pid")
    pgid = receipt.get("process_group_id")
    fingerprint = receipt.get("process_start_fingerprint")
    history = receipt.get("status_history")
    if not isinstance(pid, int) or pid < 1 or not isinstance(pgid, int) or pgid < 1:
        raise PhaseFlowError(f"receipt {ordinal} has no process identity")
    if not isinstance(fingerprint, str) or not fingerprint.startswith(f"{pid}:"):
        raise PhaseFlowError(f"receipt {ordinal} has no process fingerprint")
    if not isinstance(history, list) or [item.get("status") for item in history if isinstance(item, dict)] != [
        "prepared", "spawned_unconfirmed", "running", "succeeded"
    ]:
        raise PhaseFlowError(f"receipt {ordinal} has invalid status history")
    unit_dir = _unit_dir(run_dir, ordinal, phase["id"])
    launch_path = unit_dir / "launch.json"
    if receipt.get("launch_sha256") != _sha_file(launch_path):
        raise PhaseFlowError(f"receipt {ordinal} launch hash mismatch")
    launch = _read_object(launch_path)
    runtime_root = run_dir / ".child-runtime"
    runtime_paths = {
        "root": runtime_root,
        "home": runtime_root / "home",
        "codex_home": runtime_root / "codex-home",
        "workspace": runtime_root / "workspace",
        "temporary": runtime_root / "tmp",
    }
    raw_final = runtime_root / "io" / f"{ordinal:03d}-{receipt.get('unit_nonce', '')}" / "final.json"
    expected_argv = _child_argv(Path(state["codex_executable"]), runtime_paths, phase, raw_final)
    if launch.get("protocol") != "phase-launch/1" or launch.get("argv") != expected_argv or launch.get("cwd") != str(runtime_paths["workspace"]):
        raise PhaseFlowError(f"receipt {ordinal} launch contract mismatch")
    environment_keys = launch.get("environment_keys")
    allowed_environment_keys = _OPTIONAL_ENV_KEYS | _REQUIRED_CHILD_ENV_KEYS
    if not isinstance(environment_keys, list) or not _REQUIRED_CHILD_ENV_KEYS <= set(environment_keys) <= allowed_environment_keys:
        raise PhaseFlowError(f"receipt {ordinal} launch environment policy mismatch")
    if not isinstance(launch.get("environment_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", launch["environment_sha256"]):
        raise PhaseFlowError(f"receipt {ordinal} launch environment hash is invalid")
    if launch.get("controller_sha256") != state["controller_sha256"] or launch.get("supervisor_sha256") != state["supervisor_sha256"]:
        raise PhaseFlowError(f"receipt {ordinal} launch code identity mismatch")
    for key, name in (
        ("stdout_sha256", "stdout.jsonl"),
        ("stderr_sha256", "stderr.txt"),
        ("final_sha256", "final.json"),
        ("marker_sha256", "marker.json"),
    ):
        path = unit_dir / name
        if receipt.get(key) != _sha_file(path):
            raise PhaseFlowError(f"receipt {ordinal} artifact hash mismatch: {name}")
    identity = _identity(state["run_nonce"], ordinal, receipt.get("unit_nonce", ""))
    _schema_validate(identity, "phase-child.schema.json")
    if _read_object(unit_dir / "final.json") != identity or _read_object(unit_dir / "marker.json") != identity:
        raise PhaseFlowError(f"receipt {ordinal} identity mismatch")
    template, compiled_prompt = _compile_prompt(identity)
    if receipt.get("template_sha256") != _sha_bytes(template):
        raise PhaseFlowError(f"receipt {ordinal} template hash mismatch")
    if receipt.get("compiled_prompt_sha256") != _sha_bytes(compiled_prompt):
        raise PhaseFlowError(f"receipt {ordinal} compiled prompt hash mismatch")
    if _sha_file(unit_dir / "prompt.txt") != receipt["compiled_prompt_sha256"]:
        raise PhaseFlowError(f"receipt {ordinal} persisted prompt hash mismatch")
    if (unit_dir / "stderr.txt").read_bytes():
        raise PhaseFlowError(f"receipt {ordinal} has nonempty stderr")
    expected_marker = run_dir / ".child-runtime" / "workspace" / "markers" / "identity.json"
    thread, observed_usage, observed_event_types = _inspect_events(unit_dir / "stdout.jsonl", expected_marker)
    if thread != receipt["thread_id"]:
        raise PhaseFlowError(f"receipt {ordinal} thread mismatch")
    if receipt.get("event_types") != sorted(observed_event_types):
        raise PhaseFlowError(f"receipt {ordinal} event type metrics mismatch")
    if receipt.get("usage") != observed_usage or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in observed_usage.values()
    ):
        raise PhaseFlowError(f"receipt {ordinal} usage metrics mismatch")
    return thread


def _load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    _assert_private_run_dir(run_dir)
    state = _read_object(run_dir / "checkpoint.json")
    if state.get("protocol") != STATE_PROTOCOL:
        raise PhaseFlowError("checkpoint protocol mismatch")
    if state.get("controller_sha256") != _sha_file(Path(__file__)):
        raise PhaseFlowError("controller code changed since start")
    if state.get("supervisor_sha256") != _sha_file(Path(__file__).with_name("supervised_child.py")):
        raise PhaseFlowError("supervisor code changed since start")
    preflight_hash = state.get("controller_preflight_sha256")
    if preflight_hash is not None:
        preflight_path = run_dir / "controller-preflight.json"
        if not preflight_path.is_file() or preflight_hash != _sha_file(preflight_path):
            raise PhaseFlowError("controller preflight hash mismatch")
        _schema_validate(_read_object(preflight_path), "controller-preflight.schema.json")
    flow_path = run_dir / "flow.json"
    if state.get("flow_spec_sha256") != _sha_file(flow_path):
        raise PhaseFlowError("frozen flow hash mismatch")
    flow = _read_object(flow_path)
    mode = _validate_flow(flow)
    if mode != state.get("mode") or _catalog_hash(flow) != state.get("catalog_sha256"):
        raise PhaseFlowError("checkpoint flow metadata mismatch")
    return state, flow


def _validate_prefix(state: Mapping[str, Any], flow: Mapping[str, Any], run_dir: Path) -> list[str]:
    threads: list[str] = []
    for ordinal in range(state["next_ordinal"]):
        threads.append(_validate_succeeded_receipt(_read_object(_receipt_path(run_dir, ordinal)), state, flow, ordinal, run_dir))
    if len(threads) != len(set(threads)):
        raise PhaseFlowError("child thread id was reused")
    return threads


def _reconcile(state: dict[str, Any], flow: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    ordinal = state["next_ordinal"]
    if ordinal >= len(flow["phases"]):
        return state
    receipt_path = _receipt_path(run_dir, ordinal)
    if receipt_path.exists():
        receipt = _read_object(receipt_path)
        if receipt.get("status") != "succeeded":
            raise PhaseFlowError(f"next receipt {ordinal} is not terminal-success")
        _validate_succeeded_receipt(receipt, state, flow, ordinal, run_dir)
        state["next_ordinal"] += 1
        state["state_revision"] += 1
        state["updated_at"] = _now()
        _write_json(run_dir / "checkpoint.json", state)
    return state


def _archive_failed_attempt(run_dir: Path, ordinal: int, phase_id: str, receipt: dict[str, Any]) -> None:
    now = _now()
    if receipt.get("status") != "failed":
        receipt.update({"status": "failed", "error_type": "InterruptedAttempt", "completed_at": now})
        history = receipt.setdefault("status_history", [])
        if isinstance(history, list):
            history.append({"status": "failed", "at": now})
    receipt["recovered_at"] = now
    receipt_path = _receipt_path(run_dir, ordinal)
    _write_json(receipt_path, receipt)
    attempt_dir = run_dir / "failed-attempts" / f"{ordinal:03d}-{uuid.uuid4().hex}"
    attempt_dir.mkdir(mode=0o700, parents=True)
    unit_dir = _unit_dir(run_dir, ordinal, phase_id)
    if unit_dir.exists():
        os.replace(unit_dir, attempt_dir / "unit")
    os.replace(receipt_path, attempt_dir / "receipt.json")


def _recover_for_resume(state: dict[str, Any], flow: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    runtime = run_dir / ".child-runtime"
    ordinal = state["next_ordinal"]
    if ordinal >= len(flow["phases"]):
        if runtime.exists():
            _remove_isolation({"root": runtime}, run_dir)
        return state
    receipt_path = _receipt_path(run_dir, ordinal)
    if not receipt_path.exists():
        if runtime.exists():
            _remove_isolation({"root": runtime}, run_dir)
        if state.get("status") == "blocked":
            state["status"] = "running"
            state["state_revision"] += 1
            state["updated_at"] = _now()
            _write_json(run_dir / "checkpoint.json", state)
        return state
    receipt = _read_object(receipt_path)
    if receipt.get("status") == "succeeded":
        if runtime.exists():
            _remove_isolation({"root": runtime}, run_dir)
        return state
    if receipt.get("status") not in {"prepared", "spawned_unconfirmed", "running", "failed"}:
        raise PhaseFlowError("next receipt has an unknown recovery status")
    if receipt.get("status") in {"spawned_unconfirmed", "running"}:
        _terminate_receipt_process(receipt)
    if runtime.exists():
        for stdout in runtime.glob("io/*/stdout.jsonl"):
            receipt.update(_event_metrics(stdout))
            receipt["document_reads"] = receipt["command_executions"] + receipt["forbidden_evidence_events"]
    _archive_failed_attempt(run_dir, ordinal, flow["phases"][ordinal]["id"], receipt)
    if runtime.exists():
        _remove_isolation({"root": runtime}, run_dir)
    state.update({"status": "running", "updated_at": _now()})
    state.pop("blocked_ordinal", None)
    state.pop("last_error_type", None)
    state["state_revision"] += 1
    _write_json(run_dir / "checkpoint.json", state)
    return state


def _finish(state: dict[str, Any], flow: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    threads = _validate_prefix(state, flow, run_dir)
    receipts = [str(_receipt_path(run_dir, ordinal).relative_to(run_dir)) for ordinal in range(len(flow["phases"]))]
    result = {
        "protocol": RESULT_PROTOCOL,
        "status": "done",
        "mode": "debug",
        "certification_scope": "orchestration_only",
        "flow_id": flow["flow_id"],
        "run_id": state["run_id"],
        "flow_spec_sha256": state["flow_spec_sha256"],
        "catalog_sha256": state["catalog_sha256"],
        "units_validated": len(receipts),
        "distinct_thread_ids": len(set(threads)),
        "command_executions": 0,
        "document_reads": 0,
        "repository_mutated": False,
        "git_operations": 0,
        "receipt_paths": receipts,
        "receipt_sha256": {path: _sha_file(run_dir / path) for path in receipts},
        "completed_at": _now(),
    }
    _schema_validate(result, "debug-result.schema.json")
    _write_json(run_dir / "debug-result.json", result)
    state.update({"status": "done", "result_sha256": _sha_file(run_dir / "debug-result.json"), "updated_at": _now()})
    state["state_revision"] += 1
    _write_json(run_dir / "checkpoint.json", state)
    return result


def _drive(state: dict[str, Any], flow: Mapping[str, Any], run_dir: Path, stop_after: int | None, timeout: int) -> None:
    if state["mode"] != "debug":
        raise PhaseFlowError("project-mode execution is intentionally not enabled by this debug runner")
    state = _reconcile(state, flow, run_dir)
    _validate_prefix(state, flow, run_dir)
    if state["status"] == "done":
        return
    if state["status"] != "running":
        raise PhaseFlowError(f"run status is not resumable: {state['status']}")
    if stop_after is not None and state["next_ordinal"] >= stop_after:
        return
    paths = _prepare_isolation(run_dir)
    try:
        while state["next_ordinal"] < len(flow["phases"]):
            ordinal = state["next_ordinal"]
            receipt = _run_unit(state, flow, run_dir, paths, ordinal, timeout)
            previous_threads = set(_validate_prefix(state, flow, run_dir))
            if receipt["thread_id"] in previous_threads:
                raise PhaseFlowError("child thread id was reused")
            state["next_ordinal"] += 1
            state["state_revision"] += 1
            state["updated_at"] = _now()
            _write_json(run_dir / "checkpoint.json", state)
            print(f"validated unit {ordinal + 1}/{len(flow['phases'])}", flush=True)
            if stop_after is not None and state["next_ordinal"] >= stop_after:
                return
        _finish(state, flow, run_dir)
    except Exception as exc:
        state.update({
            "status": "blocked",
            "blocked_ordinal": state["next_ordinal"],
            "last_error_type": type(exc).__name__,
            "updated_at": _now(),
        })
        state["state_revision"] += 1
        _write_json(run_dir / "checkpoint.json", state)
        raise
    finally:
        _remove_isolation(paths, run_dir)


def _start(flow_path: Path, run_dir: Path, stop_after: int | None, timeout: int) -> None:
    if stop_after is not None and stop_after < 1:
        raise PhaseFlowError("stop-after must be positive")
    if timeout < 1:
        raise PhaseFlowError("timeout must be positive")
    flow, flow_bytes, executable_identity, preflight = _controller_preflight(flow_path, run_dir)
    mode = preflight["mode"]
    if mode != "debug":
        raise PhaseFlowError("project-mode execution is intentionally not enabled by this debug runner")
    executable, version, executable_hash = executable_identity
    run_dir.mkdir(mode=0o700, parents=True)
    run_dir.chmod(0o700)
    _assert_private_run_dir(run_dir)
    _write_bytes(run_dir / "flow.json", flow_bytes)
    _write_json(run_dir / "controller-preflight.json", preflight)
    state = {
        "protocol": STATE_PROTOCOL,
        "status": "running",
        "mode": mode,
        "certification_scope": "orchestration_only",
        "run_id": uuid.uuid4().hex,
        "run_nonce": uuid.uuid4().hex,
        "flow_id": flow["flow_id"],
        "flow_spec_sha256": _sha_bytes(flow_bytes),
        "catalog_sha256": _catalog_hash(flow),
        "codex_executable": str(executable),
        "codex_version": version,
        "codex_executable_sha256": executable_hash,
        "controller_sha256": _sha_file(Path(__file__)),
        "supervisor_sha256": _sha_file(Path(__file__).with_name("supervised_child.py")),
        "controller_preflight_sha256": _sha_file(run_dir / "controller-preflight.json"),
        "next_ordinal": 0,
        "state_revision": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _write_json(run_dir / "checkpoint.json", state)
    _drive(state, flow, run_dir, stop_after, timeout)


def _resume(run_dir: Path, stop_after: int | None, timeout: int) -> None:
    if stop_after is not None and stop_after < 1:
        raise PhaseFlowError("stop-after must be positive")
    if timeout < 1:
        raise PhaseFlowError("timeout must be positive")
    state, flow = _load_run(run_dir)
    executable, version, executable_hash = _resolve_codex()
    if (str(executable), version, executable_hash) != (
        state["codex_executable"], state["codex_version"], state["codex_executable_sha256"]
    ):
        raise PhaseFlowError("Codex executable identity changed since start")
    state = _recover_for_resume(state, flow, run_dir)
    _drive(state, flow, run_dir, stop_after, timeout)


def _verify(run_dir: Path) -> dict[str, Any]:
    state, flow = _load_run(run_dir)
    if (run_dir / ".child-runtime").exists():
        raise PhaseFlowError("private child runtime still exists")
    if state.get("status") != "done" or state.get("next_ordinal") != len(flow["phases"]):
        raise PhaseFlowError("run is not complete")
    _validate_prefix(state, flow, run_dir)
    executable, version, executable_hash = _resolve_codex()
    if (str(executable), version, executable_hash) != (
        state["codex_executable"], state["codex_version"], state["codex_executable_sha256"]
    ):
        raise PhaseFlowError("Codex executable identity changed since start")
    result = _read_object(run_dir / "debug-result.json")
    _schema_validate(result, "debug-result.schema.json")
    if state.get("result_sha256") != _sha_file(run_dir / "debug-result.json"):
        raise PhaseFlowError("result hash mismatch")
    expected_paths = [str(_receipt_path(run_dir, ordinal).relative_to(run_dir)) for ordinal in range(len(flow["phases"]))]
    if result["receipt_paths"] != expected_paths or set(result["receipt_sha256"]) != set(expected_paths):
        raise PhaseFlowError("result receipt index mismatch")
    expected_result = {
        "protocol": RESULT_PROTOCOL,
        "status": "done",
        "mode": "debug",
        "certification_scope": "orchestration_only",
        "flow_id": flow["flow_id"],
        "run_id": state["run_id"],
        "flow_spec_sha256": state["flow_spec_sha256"],
        "catalog_sha256": state["catalog_sha256"],
        "units_validated": len(flow["phases"]),
        "distinct_thread_ids": len(flow["phases"]),
        "command_executions": 0,
        "document_reads": 0,
        "repository_mutated": False,
        "git_operations": 0,
    }
    mismatches = [key for key, value in expected_result.items() if result.get(key) != value]
    if mismatches:
        raise PhaseFlowError(f"result semantic mismatch: {','.join(mismatches)}")
    for path, digest in result["receipt_sha256"].items():
        if _sha_file(run_dir / path) != digest:
            raise PhaseFlowError(f"result receipt hash mismatch: {path}")
    return result


def _inspect(run_dir: Path) -> dict[str, Any]:
    state, flow = _load_run(run_dir)
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    completed = 0
    command_executions = 0
    document_reads = 0
    forbidden_evidence_events = 0
    unexpected_item_events = 0
    receipt_paths = sorted((run_dir / "receipts").glob("*.json"))
    receipt_paths.extend(sorted((run_dir / "failed-attempts").glob("*/receipt.json")))
    for receipt_path in receipt_paths:
        receipt = _read_object(receipt_path)
        if receipt.get("status") == "succeeded":
            completed += 1
            for key in totals:
                totals[key] += receipt.get("usage", {}).get(key, 0)
        command_executions += int(receipt.get("command_executions", 0))
        document_reads += int(receipt.get("document_reads", 0))
        forbidden_evidence_events += int(receipt.get("forbidden_evidence_events", 0))
        unexpected_item_events += int(receipt.get("unexpected_item_events", 0))
    return {
        "mode": state["mode"],
        "status": state["status"],
        "completed_units": completed,
        "total_units": len(flow["phases"]),
        "command_executions": command_executions,
        "document_reads": document_reads,
        "forbidden_evidence_events": forbidden_evidence_events,
        "unexpected_item_events": unexpected_item_events,
        "failed_attempts": len(list((run_dir / "failed-attempts").glob("*/receipt.json"))),
        "contamination_detected": any(
            (command_executions, document_reads, forbidden_evidence_events, unexpected_item_events)
        ),
        "private_runtime_present": (run_dir / ".child-runtime").exists(),
        "controller_preflight": (
            _read_object(run_dir / "controller-preflight.json")
            if (run_dir / "controller-preflight.json").is_file()
            else None
        ),
        "usage": totals,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--flow", type=Path, required=True)
    start.add_argument("--run-dir", type=Path, required=True)
    start.add_argument("--stop-after", type=int)
    start.add_argument("--timeout-seconds", type=int, default=600)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--flow", type=Path, required=True)
    preflight.add_argument("--run-dir", type=Path, required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--stop-after", type=int)
    resume.add_argument("--timeout-seconds", type=int, default=600)
    verify = commands.add_parser("verify")
    verify.add_argument("--run-dir", type=Path, required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "start":
            _start(arguments.flow.resolve(), arguments.run_dir.resolve(), arguments.stop_after, arguments.timeout_seconds)
        elif arguments.command == "preflight":
            _, _, _, report = _controller_preflight(arguments.flow.resolve(), arguments.run_dir.resolve())
            print(json.dumps(report, indent=2, sort_keys=True))
        elif arguments.command == "resume":
            _resume(arguments.run_dir.resolve(), arguments.stop_after, arguments.timeout_seconds)
        elif arguments.command == "verify":
            print(json.dumps(_verify(arguments.run_dir.resolve()), indent=2, sort_keys=True))
        else:
            print(json.dumps(_inspect(arguments.run_dir.resolve()), indent=2, sort_keys=True))
    except (OSError, PhaseFlowError, KeyError, TypeError, ValueError) as exc:
        print(f"phase-flow error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
