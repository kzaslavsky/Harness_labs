#!/usr/bin/env python3
"""Run one schema-bound Codex subprocess with durable process receipts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import re
from pathlib import Path
from typing import Any

from response_schema import (
    COMPILER_VERSION,
    compile_transport_schema,
    validate_provider_schema,
)
from controller_package import (
    PACKAGE_VERSION,
    migration_authority_lock,
    source_package_digest,
    validate_committed_migration,
    verify_controller_package,
)
from repair_preflight import validate_capability_manifest
from state_io import StateError, atomic_write_bytes, atomic_write_json, canonical_bytes, cas_update, locked, read_json, sha256_bytes, sha256_file


TERMINAL_FAILURE_EVENTS = {"turn.failed", "error"}
_CODEX_VERSION_RE = re.compile(r"^codex-cli\s+\S+")
VALIDATOR_UNAVAILABLE_ERROR = "jsonschema is required for subprocess output validation"
PROCESS_RECEIPT_PROTOCOL = "implement-v13-codex/process-receipt/3"
UI_PLAYWRIGHT_CAPABILITY = "browser.playwright.local"
TERMINAL_RETRY_POLICY = {
    "none": False,
    "controller_interrupted": False,
    "response_schema_transport_rejected": False,
    "provider_auth_rejected": False,
    "provider_rate_limited": True,
    "wall_timeout": True,
    "child_process_failure": True,
    "terminal_protocol_failure": False,
    "output_validation_failure": False,
}


class _CombinedValidator:
    """Validate provider shape first and normative semantics second."""

    def __init__(self, transport: Any, normative: Any):
        self.transport = transport
        self.normative = normative

    def validate(self, document: Any) -> None:
        self.transport.validate(document)
        self.normative.validate(document)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _process_fingerprint(pid: int) -> str:
    """Return a PID-reuse witness available on macOS and Linux."""
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    started = result.stdout.strip()
    if result.returncode != 0 or not started:
        raise StateError(f"cannot fingerprint child pid {pid}")
    return f"{pid}:{started}"


def _resolve_codex() -> tuple[str, str, str]:
    executable_text = shutil.which("codex")
    if not executable_text or Path(executable_text).name != "codex":
        raise StateError("real codex executable was not found")
    executable = str(Path(executable_text).resolve())
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or _CODEX_VERSION_RE.match(version) is None:
        raise StateError("resolved executable did not identify as codex-cli")
    return executable, version, sha256_file(Path(executable))


def _write_release(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, b"release\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _event_type(event: dict[str, Any]) -> str | None:
    value = event.get("type")
    return value if isinstance(value, str) else None


def _thread_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if _event_type(event) == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                return value
    return None


def _check_schema_references(schema: dict[str, Any]) -> None:
    """Allow only resolvable, root-local JSON-pointer references."""

    def walk(value: Any, *, root: bool = False) -> None:
        if isinstance(value, dict):
            if not root and "$id" in value:
                raise StateError("output schema nested resources are unsupported")
            reference = value.get("$ref")
            if reference is not None:
                if not isinstance(reference, str) or not reference.startswith("#/"):
                    raise StateError("output schema reference must be a local JSON pointer")
                current: Any = schema
                for encoded in reference[2:].split("/"):
                    token = encoded.replace("~1", "/").replace("~0", "~")
                    if not isinstance(current, dict) or token not in current:
                        raise StateError(f"output schema local reference is unresolved: {reference}")
                    current = current[token]
            if "$dynamicRef" in value or "$recursiveRef" in value:
                raise StateError("output schema dynamic references are unsupported")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema, root=True)


def _validator_for_schema(
    schema: dict[str, Any], *, provider_transport: bool = True
) -> tuple[Any, type[Exception]]:
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError as exc:
        raise StateError(VALIDATOR_UNAVAILABLE_ERROR) from exc
    _check_schema_references(schema)
    if provider_transport:
        _check_codex_response_schema(schema)
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        validator = validator_class(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise StateError(f"output schema is invalid: {exc.message}") from exc
    return validator, jsonschema.exceptions.ValidationError


def _build_validator(
    schema_path: Path, *, provider_transport: bool = False
) -> tuple[Any, type[Exception], str, dict[str, Any]]:
    try:
        schema_bytes = schema_path.read_bytes()
        schema = json.loads(schema_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError(f"output schema is unreadable: {type(exc).__name__}") from exc
    if not isinstance(schema, dict):
        raise StateError("output schema must be an object")
    validator, validation_error = _validator_for_schema(
        schema, provider_transport=provider_transport
    )
    return validator, validation_error, sha256_bytes(schema_bytes), schema


def _bind_expected_schema(schema: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Make scalar semantic identity constraints part of the child output schema."""
    bound = copy.deepcopy(schema)
    properties = bound.get("properties")
    if not isinstance(properties, dict):
        if expected:
            raise StateError("spec.expected cannot bind a schema without object properties")
        return bound
    required = bound.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise StateError("output schema required must be an array of property names")
    json_types = {
        str: "string",
        bool: "boolean",
        int: "integer",
        float: "number",
        type(None): "null",
    }
    for key, value in expected.items():
        if not isinstance(key, str) or not key:
            raise StateError("spec.expected keys must be nonempty strings")
        if type(value) not in json_types:
            raise StateError(f"spec.expected value for {key} must be scalar")
        property_schema = properties.get(key)
        if not isinstance(property_schema, dict):
            raise StateError(f"expected semantic field is absent from output schema: {key}")
        expected_type = json_types[type(value)]
        declared_type = property_schema.get("type")
        compatible_types = declared_type if isinstance(declared_type, list) else [declared_type]
        if expected_type == "integer" and "number" in compatible_types:
            pass
        elif expected_type not in compatible_types:
            raise StateError(f"expected semantic field has incompatible schema type: {key}")
        if "const" in property_schema and property_schema["const"] != value:
            raise StateError(f"expected semantic field conflicts with schema const: {key}")
        if "enum" in property_schema and value not in property_schema["enum"]:
            raise StateError(f"expected semantic field conflicts with schema enum: {key}")
        property_schema["const"] = value
        if key not in required:
            required.append(key)
    bound["required"] = required
    _validator_for_schema(bound, provider_transport=False)
    return bound


def _check_codex_response_schema(schema: dict[str, Any]) -> None:
    """Validate the exact provider transport schema recursively."""
    validate_provider_schema(schema)


def _validate_schema(document: Any, validator: Any, validation_error: type[Exception]) -> None:
    try:
        validator.validate(document)
    except validation_error as exc:
        raise StateError(f"schema validation failed at {list(exc.absolute_path)}") from exc
    except Exception as exc:
        raise StateError(f"schema validation could not execute: {type(exc).__name__}") from exc


def _semantic_validate(document: dict[str, Any], spec: dict[str, Any]) -> None:
    expected = spec.get("expected", {})
    if not isinstance(expected, dict):
        raise StateError("spec.expected must be an object")
    for key, value in expected.items():
        if document.get(key) != value:
            raise StateError(f"semantic mismatch for {key}")


def _writable_roots(spec: dict[str, Any]) -> list[Path]:
    raw = spec.get("writable_roots", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise StateError("spec.writable_roots must be an array of absolute directory paths")
    if raw and spec.get("sandbox", "read-only") != "workspace-write":
        raise StateError("writable_roots require the workspace-write sandbox")
    roots: list[Path] = []
    for item in raw:
        source = Path(item)
        if not source.is_absolute():
            raise StateError("every writable root must be absolute")
        root = source.resolve()
        if not root.is_dir():
            raise StateError(f"writable root is not a directory: {root}")
        if root not in roots:
            roots.append(root)
    return roots


def _required_capabilities(spec: dict[str, Any]) -> list[str]:
    raw = spec.get("required_capabilities", [])
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise StateError("spec.required_capabilities must be an array of names")
    return raw


def _network_access_enabled(spec: dict[str, Any]) -> bool:
    """Derive Codex network authority from the task capability contract."""

    if "network_access" in spec:
        raise StateError(
            "spec.network_access is not an authority field; network access is "
            "derived from required_capabilities"
        )
    enabled = UI_PLAYWRIGHT_CAPABILITY in _required_capabilities(spec)
    if enabled and spec.get("sandbox", "read-only") != "workspace-write":
        raise StateError(
            "browser.playwright.local requires the workspace-write sandbox"
        )
    return enabled


def _capability_manifest_identity(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    path_raw = spec.get("capability_manifest_path")
    digest = spec.get("capability_manifest_sha256")
    if path_raw is None and digest is None:
        if spec.get("ephemeral_scratch") is True:
            raise StateError("ephemeral_scratch requires a capability manifest")
        return None, None
    if (
        not isinstance(path_raw, str)
        or not Path(path_raw).is_absolute()
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise StateError("capability manifest identity is incomplete")
    validate_capability_manifest(
        Path(path_raw),
        digest,
        repository_root=Path(str(spec["cwd"])),
        feature_run_id=spec.get("feature_run_id"),
        controller_package_digest=spec.get("controller_package_digest"),
    )
    return str(Path(path_raw).resolve()), digest


def _ephemeral_scratch_path(spec: dict[str, Any]) -> Path | None:
    requested = spec.get("ephemeral_scratch", False)
    if not isinstance(requested, bool):
        raise StateError("spec.ephemeral_scratch must be a boolean")
    if not requested:
        return None
    receipt_id = spec.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        raise StateError("ephemeral_scratch requires receipt_id")
    digest = hashlib.sha256(receipt_id.encode("utf-8")).hexdigest()
    path = (
        Path(tempfile.gettempdir()).resolve()
        / "implement-v13-codex-scratch"
        / digest
    )
    protected = [Path(str(spec["cwd"])).resolve(), *_writable_roots(spec)]
    for root in protected:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        raise StateError("ephemeral scratch overlaps repository write authority")
    return path


def _prepare_ephemeral_scratch(
    spec: dict[str, Any], *, create: bool, allow_consumed: bool = False
) -> Path | None:
    path = _ephemeral_scratch_path(spec)
    if path is None:
        return None
    _capability_manifest_identity(spec)
    if allow_consumed and not path.exists():
        return path
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise StateError(
                "controller-owned ephemeral scratch already exists before attempt"
            ) from exc
    if not path.is_dir():
        raise StateError("controller-owned ephemeral scratch is missing")
    if path.stat().st_uid != os.getuid() or (path.stat().st_mode & 0o077):
        raise StateError("controller-owned ephemeral scratch is not private")
    if create and any(path.iterdir()):
        raise StateError("controller-owned ephemeral scratch is not empty at launch")
    return path


def _scratch_contents_sha256(path: Path) -> str:
    entries: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            try:
                target = item.resolve(strict=True)
                target_relative = target.relative_to(path.resolve()).as_posix()
            except (FileNotFoundError, RuntimeError, ValueError):
                raise StateError("ephemeral scratch contains an escaping or invalid symlink") from None
            entries.append(
                {
                    "path": str(item.relative_to(path)),
                    "type": "symlink",
                    "target": target_relative,
                }
            )
            continue
        relative = str(item.relative_to(path))
        if item.is_dir():
            entries.append({"path": relative, "type": "directory"})
        elif item.is_file():
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": item.stat().st_size,
                    "sha256": sha256_file(item),
                }
            )
        else:
            raise StateError("ephemeral scratch contains an unsupported entry")
    return sha256_bytes(canonical_bytes(entries))


def _controller_child_environment(
    spec: dict[str, Any], scratch: Path | None = None
) -> dict[str, str]:
    marker = spec.get("controller_child", False)
    if not isinstance(marker, bool):
        raise StateError("spec.controller_child must be a boolean")
    environment: dict[str, str] = {}
    if marker:
        if not (
            spec.get("phase") == "COORDINATOR"
            and spec.get("role") == "feature_coordinator"
        ):
            raise StateError("controller_child is restricted to the feature coordinator")
        environment["IMPLEMENT_V13_RUN_FEATURE_CHILD"] = "1"
    if scratch is not None:
        environment.update(
            {
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "CODEX_EPHEMERAL_SCRATCH": str(scratch),
            }
        )
    return environment


def _controller_package_identity(spec: dict[str, Any]) -> tuple[str, str, str]:
    digest = spec.get("controller_package_digest")
    package_path = spec.get("controller_package_path")
    if digest is None and package_path is None:
        source_parent = Path(__file__).resolve().parents[2]
        return source_package_digest(source_parent), PACKAGE_VERSION, str(source_parent)
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(package_path, str)
        or not Path(package_path).is_absolute()
    ):
        raise StateError("invocation controller package identity is incomplete")
    root = Path(package_path).resolve()
    verify_controller_package(root, digest)
    try:
        Path(__file__).resolve().relative_to(root / "implement-v13-codex")
    except ValueError as exc:
        raise StateError("child launch is not executing from the run-owned controller package") from exc
    migration_path = spec.get("controller_migration_journal_path")
    if migration_path is not None:
        receipt_hash = spec.get("controller_migration_receipt_sha256")
        if (
            not isinstance(migration_path, str)
            or not Path(migration_path).is_absolute()
            or not isinstance(receipt_hash, str)
        ):
            raise StateError("child launch migration identity is incomplete")
        journal_path = Path(migration_path)
        with locked(migration_authority_lock(journal_path)):
            validate_committed_migration(
                journal_path,
                expected_package_digest=digest,
                expected_receipt_sha256=receipt_hash,
                allow_queue_advance=True,
            )
    return digest, PACKAGE_VERSION, str(root)


def _child_spec(
    spec: dict[str, Any], argv: list[str], scratch: Path | None = None
) -> dict[str, Any]:
    child = {"argv": argv, "release_timeout_seconds": 30}
    environment = _controller_child_environment(spec, scratch)
    if environment:
        child["environment"] = environment
    return child


def _preflight(spec: dict[str, Any], prompt_path: Path, schema_path: Path) -> tuple[Any, type[Exception], float, str, str, dict[str, Any], bytes]:
    """Validate every deterministic child-output dependency before an attempt exists."""
    cwd = Path(spec["cwd"]).resolve()
    if not cwd.is_dir():
        raise StateError(f"invocation cwd is not a directory: {cwd}")
    _writable_roots(spec)
    _controller_child_environment(spec)
    _controller_package_identity(spec)
    _capability_manifest_identity(spec)
    _ephemeral_scratch_path(spec)
    for path in (prompt_path, schema_path, Path(__file__).with_name("supervised_child.py")):
        if not path.is_file():
            raise StateError(f"required invocation input missing: {path}")
    if not isinstance(spec.get("expected", {}), dict):
        raise StateError("spec.expected must be an object")
    if (
        spec.get("phase") == "PLAN_REVIEW"
        and spec.get("phase_detail") == "review_dispatch"
        and spec.get("role")
        in {"source_binding_reviewer", "necessity_reviewer", "frame_reviewer"}
    ):
        canonical = Path(__file__).resolve().parents[1] / "schemas" / "plan-review.schema.json"
        try:
            if schema_path.read_bytes() != canonical.read_bytes():
                raise StateError("plan reviewer must use canonical plan-review.schema.json")
        except OSError as exc:
            raise StateError(f"canonical plan-review schema is unreadable: {type(exc).__name__}") from exc
    try:
        timeout = float(spec.get("wall_timeout_seconds", 3600))
    except (TypeError, ValueError) as exc:
        raise StateError("wall_timeout_seconds must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise StateError("wall_timeout_seconds must be a positive finite number")
    source_validator, validation_error, schema_hash, source_schema = _build_validator(
        schema_path, provider_transport=False
    )
    # Surface provider-dialect defects before semantic expected-field binding.
    compile_transport_schema(source_schema)
    bound_source = _bind_expected_schema(source_schema, spec.get("expected", {}))
    source_validator, validation_error = _validator_for_schema(
        bound_source, provider_transport=False
    )
    schema = compile_transport_schema(bound_source)
    transport_validator, validation_error = _validator_for_schema(
        schema, provider_transport=True
    )
    validator = _CombinedValidator(transport_validator, source_validator)
    try:
        prompt = prompt_path.read_bytes()
    except OSError as exc:
        raise StateError(f"prompt is unreadable: {type(exc).__name__}") from exc
    return validator, validation_error, timeout, sha256_bytes(prompt), schema_hash, schema, prompt


def preflight_response_schema(
    schema_path: Path, expected: dict[str, Any] | None = None
) -> dict[str, str]:
    """Exercise one canonical schema through production binding and compilation."""
    _, _, source_hash, source_schema = _build_validator(
        schema_path, provider_transport=False
    )
    compile_transport_schema(source_schema)
    bound_source = _bind_expected_schema(source_schema, expected or {})
    transport = compile_transport_schema(bound_source)
    _validator_for_schema(transport, provider_transport=True)
    return {
        "schema_source_sha256": source_hash,
        "schema_transport_sha256": sha256_bytes(canonical_bytes(transport)),
        "schema_compiler_version": COMPILER_VERSION,
    }


def _group_has_live_members(pgid: int) -> bool:
    """Return whether a process group still contains any non-zombie member."""
    completed = subprocess.run(
        ["ps", "-axo", "pgid=,stat="],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return True
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit():
            if int(fields[0]) == pgid and not fields[1].startswith("Z"):
                return True
    return False


def _terminate_owned_group(pid: int, pgid: int, fingerprint: str) -> bool:
    """Terminate only the positively identified process group."""
    try:
        if _process_fingerprint(pid) != fingerprint or os.getpgid(pid) != pgid:
            return False
    except (ProcessLookupError, StateError):
        return False
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _group_has_live_members(pgid):
            return True
        time.sleep(0.1)
    try:
        if _process_fingerprint(pid) == fingerprint and os.getpgid(pid) == pgid:
            os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, StateError):
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _group_has_live_members(pgid):
            return True
        time.sleep(0.1)
    return False


def _process_matches(receipt: dict[str, Any]) -> bool:
    """Return whether a nonterminal receipt still names the same live process."""
    try:
        pid = int(receipt["pid"])
        pgid = int(receipt["process_group_id"])
        fingerprint = str(receipt["process_start_fingerprint"])
        return os.getpgid(pid) == pgid and _process_fingerprint(pid) == fingerprint
    except (KeyError, TypeError, ValueError, ProcessLookupError, StateError):
        return False


def _provider_error_fields(events: list[dict[str, Any]]) -> tuple[str, str | None, int | None]:
    """Extract only structured provider terminal fields from stdout JSONL."""
    for event in reversed(events):
        if _event_type(event) not in TERMINAL_FAILURE_EVENTS:
            continue
        error = event.get("error")
        candidates = [error, event]
        message = ""
        code: str | None = None
        status: int | None = None
        for candidate in candidates:
            if isinstance(candidate, str) and not message:
                message = candidate
            elif isinstance(candidate, dict):
                for key in ("message", "detail", "error"):
                    value = candidate.get(key)
                    if isinstance(value, str) and value and not message:
                        message = value
                for key in ("code", "type"):
                    value = candidate.get(key)
                    if isinstance(value, str) and value and value not in TERMINAL_FAILURE_EVENTS:
                        code = value
                        break
                for key in ("status", "status_code", "http_status"):
                    value = candidate.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        status = value
                        break
        if message or code is not None or status is not None:
            return message, code, status
    return "", None, None


def classify_terminal_cause(
    *,
    events: list[dict[str, Any]],
    exit_code: int,
    timed_out: bool,
    validation_errors: list[str],
) -> dict[str, Any]:
    """Normalize a terminal cause without interpreting stderr diagnostics."""
    message, provider_code, http_status = _provider_error_fields(events)
    normalized = message.strip().replace("\x00", "")
    lowered = f"{provider_code or ''} {normalized}".lower()
    if timed_out:
        failure_class = "wall_timeout"
    elif (
        "invalid_json_schema" in lowered
        or "invalid response schema" in lowered
        or (
            http_status is not None
            and 400 <= http_status < 500
            and "schema" in lowered
        )
    ):
        failure_class = "response_schema_transport_rejected"
    elif http_status in {401, 403} or any(
        token in lowered for token in ("authentication", "unauthorized", "forbidden", "invalid_api_key")
    ):
        failure_class = "provider_auth_rejected"
    elif http_status == 429 or any(
        token in lowered for token in ("rate_limit", "rate limit", "quota")
    ):
        failure_class = "provider_rate_limited"
    elif exit_code != 0:
        failure_class = "child_process_failure"
    elif any(error.startswith("schema validation") or error.startswith("semantic mismatch") for error in validation_errors):
        failure_class = "output_validation_failure"
    elif validation_errors:
        failure_class = "terminal_protocol_failure"
    else:
        failure_class = "none"
    return {
        "class": failure_class,
        "retryable": TERMINAL_RETRY_POLICY[failure_class],
        "provider_code": provider_code,
        "http_status": http_status,
        "message": normalized,
    }


def terminal_retry_allowed(cause: dict[str, Any]) -> bool:
    failure_class = cause.get("class")
    if failure_class not in TERMINAL_RETRY_POLICY:
        raise StateError("unknown terminal failure class")
    if cause.get("retryable") is not TERMINAL_RETRY_POLICY[failure_class]:
        raise StateError("terminal cause retry flag conflicts with controller policy")
    return TERMINAL_RETRY_POLICY[failure_class]


def _stderr_diagnostics(stderr_path: Path) -> list[dict[str, str]]:
    if not stderr_path.is_file():
        return []
    diagnostics: list[dict[str, str]] = []
    for line in stderr_path.read_text(encoding="utf-8", errors="replace").splitlines():
        message = line.strip()
        if message:
            diagnostics.append({"source": "stderr", "message": message[:4096]})
    return diagnostics


def _provider_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return authoritative terminal usage without manufacturing zeroes."""
    observed: list[dict[str, int]] = []
    for event in events:
        if _event_type(event) != "turn.completed" or "usage" not in event:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            raise StateError("turn.completed usage is not an object")
        normalized: dict[str, int] = {}
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = usage.get(field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise StateError(f"turn.completed usage is invalid: {field}")
            normalized[field] = value
        observed.append(normalized)
    if not observed:
        return {
            "status": "unknown",
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
        }
    if any(item != observed[-1] for item in observed[:-1]):
        raise StateError("conflicting turn.completed usage")
    return {"status": "recorded", **observed[-1]}


def _provider_usage_or_unknown(events: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        return _provider_usage(events)
    except StateError:
        return {
            "status": "unknown",
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
        }


def _audit_scratch(receipt: dict[str, Any]) -> tuple[str | None, bool, list[str]]:
    """Hash and remove controller-created scratch, returning durable evidence."""
    errors: list[str] = []
    scratch_path_raw = receipt.get("ephemeral_scratch")
    contents_sha256 = receipt.get("ephemeral_scratch_contents_sha256")
    removed = receipt.get("ephemeral_scratch_removed") is True
    if not isinstance(scratch_path_raw, str):
        return contents_sha256, removed, errors
    scratch_path = Path(scratch_path_raw)
    if scratch_path.is_dir():
        try:
            contents_sha256 = _scratch_contents_sha256(scratch_path)
        except StateError as exc:
            errors.append(str(exc))
        shutil.rmtree(scratch_path, ignore_errors=True)
    elif not removed:
        errors.append("ephemeral scratch disappeared before terminal hashing")
    removed = not scratch_path.exists()
    if contents_sha256 is not None and not removed:
        errors.append("ephemeral scratch removal failed")
    return contents_sha256, removed, errors


def _finalize_interrupted_receipt(
    *,
    receipt_path: Path,
    receipt: dict[str, Any],
    process: subprocess.Popen[bytes],
    stdout_path: Path,
    stderr_path: Path,
    output_path: Path,
    child_spec_path: Path,
    exit_path: Path,
    termination_verified: bool,
) -> dict[str, Any]:
    """Terminalize an interrupted owned invocation before re-raising."""
    reaped = False
    try:
        process.wait(timeout=5)
        reaped = True
    except subprocess.TimeoutExpired:
        reaped = False
    exit_code = process.returncode if process.returncode is not None else -signal.SIGKILL
    if not exit_path.exists():
        atomic_write_json(
            exit_path,
            {"exit_code": int(exit_code), "controller_interrupted": True},
        )
    scratch_sha256, scratch_removed, scratch_errors = _audit_scratch(receipt)
    available = {
        name: sha256_file(path)
        for name, path in {
            "stdout": stdout_path,
            "stderr": stderr_path,
            "output": output_path,
            "child_spec": child_spec_path,
            "exit": exit_path,
        }.items()
        if path.is_file()
    }
    verified = termination_verified and reaped
    receipt.update(
        status="failed",
        completed_at=_utc_now(),
        exit_code=int(exit_code),
        event_types=[_event_type(item) for item in _read_events(stdout_path)],
        validation_errors=["controller interrupted owned child", *scratch_errors],
        terminal_cause={
            "class": "controller_interrupted",
            "retryable": False,
            "provider_code": None,
            "http_status": None,
            "message": "controller interrupted owned child",
        },
        diagnostics=_stderr_diagnostics(stderr_path),
        ephemeral_scratch_contents_sha256=scratch_sha256,
        ephemeral_scratch_removed=scratch_removed,
        interruption={
            "marker": True,
            "termination_status": "verified" if verified else "unverified",
            "supervisor_reaped": reaped,
            "available_artifact_sha256": available,
        },
        provider_usage=_provider_usage_or_unknown(_read_events(stdout_path)),
        timed_out=False,
        state_revision=int(receipt.get("state_revision", 0)) + 1,
    )
    atomic_write_json(receipt_path, receipt)
    return receipt


def _terminal_validation_errors(
    *,
    exit_code: int,
    stdout_path: Path,
    output_path: Path,
    spec: dict[str, Any],
    validator: Any,
    validation_error: type[Exception],
    stdout_bytes: bytes | None = None,
    output_bytes: bytes | None = None,
    timed_out: bool = False,
) -> tuple[list[str], list[str], dict[str, Any] | None]:
    if stdout_bytes is None:
        stdout_bytes = stdout_path.read_bytes() if stdout_path.exists() else b""
    events = []
    for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    event_types = [_event_type(event) for event in events]
    errors: list[str] = []
    if timed_out:
        errors.append("wall timeout")
    if exit_code != 0:
        errors.append(f"exit code {exit_code}")
    if "thread.started" not in event_types:
        errors.append("missing thread.started")
    if "turn.completed" not in event_types:
        errors.append("missing turn.completed")
    if any(event_type in TERMINAL_FAILURE_EVENTS for event_type in event_types):
        errors.append("terminal error event")
    document: dict[str, Any] | None = None
    if output_bytes is None:
        output_bytes = output_path.read_bytes() if output_path.exists() else b""
    if not output_bytes:
        errors.append("missing final output")
    else:
        try:
            parsed = json.loads(output_bytes)
            if not isinstance(parsed, dict):
                raise StateError("final output must be an object")
            document = parsed
            _validate_schema(document, validator, validation_error)
            _semantic_validate(document, spec)
        except (OSError, json.JSONDecodeError, StateError) as exc:
            errors.append(str(exc))
    return errors, event_types, document


def _finalize_receipt(
    *,
    receipt_path: Path,
    receipt: dict[str, Any],
    exit_code: int,
    stdout_path: Path,
    stderr_path: Path,
    output_path: Path,
    child_spec_path: Path,
    exit_path: Path,
    spec: dict[str, Any],
    validator: Any,
    validation_error: type[Exception],
    timed_out: bool = False,
) -> dict[str, Any]:
    errors, event_types, document = _terminal_validation_errors(
        exit_code=exit_code,
        stdout_path=stdout_path,
        output_path=output_path,
        spec=spec,
        validator=validator,
        validation_error=validation_error,
        timed_out=timed_out,
    )
    events = _read_events(stdout_path)
    try:
        provider_usage = _provider_usage(events)
    except StateError as exc:
        errors.append(str(exc))
        provider_usage = {
            "status": "unknown",
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
        }
    terminal_cause = classify_terminal_cause(
        events=events,
        exit_code=exit_code,
        timed_out=timed_out,
        validation_errors=errors,
    )
    scratch_contents_sha256, scratch_removed, scratch_errors = _audit_scratch(receipt)
    errors.extend(scratch_errors)
    terminal_cause = classify_terminal_cause(
        events=events,
        exit_code=exit_code,
        timed_out=timed_out,
        validation_errors=errors,
    )
    if _thread_id(events) and receipt.get("status") != "running":
        receipt.update(
            status="running",
            thread_id=_thread_id(events),
            running_at=receipt.get("running_at", _utc_now()),
            state_revision=int(receipt.get("state_revision", 2)) + 1,
        )
        atomic_write_json(receipt_path, receipt)
    receipt.update(
        status="failed" if errors else "succeeded",
        completed_at=_utc_now(),
        exit_code=exit_code,
        thread_id=_thread_id(events),
        event_types=event_types,
        validation_errors=errors,
        terminal_cause=terminal_cause,
        diagnostics=_stderr_diagnostics(stderr_path),
        provider_usage=provider_usage,
        ephemeral_scratch_contents_sha256=scratch_contents_sha256,
        ephemeral_scratch_removed=scratch_removed,
        output_sha256=sha256_file(output_path) if output_path.exists() else None,
        artifact_sha256={
            "prompt": str(receipt["prompt_sha256"]),
            "schema": str(receipt["schema_sha256"]),
            "codex_executable": str(receipt["codex_executable_sha256"]),
            **{
                name: sha256_file(path)
                for name, path in {
                    "stdout": stdout_path,
                    "stderr": stderr_path,
                    "output": output_path,
                    "child_spec": child_spec_path,
                    "exit": exit_path,
                }.items()
                if path.is_file()
            },
        },
        timed_out=timed_out,
        state_revision=int(receipt.get("state_revision", 2)) + 1,
    )
    atomic_write_json(receipt_path, receipt)
    if errors:
        raise StateError("; ".join(errors))
    if document is None:
        raise StateError("validated output unexpectedly absent")
    return receipt


def _assert_receipt_matches(
    receipt: dict[str, Any],
    spec: dict[str, Any],
    prompt_path: Path,
    schema_path: Path,
    prompt_sha256: str,
    schema_sha256: str,
    prompt_source_path: Path,
    prompt_source_sha256: str,
    schema_source_path: Path,
    schema_source_sha256: str,
    codex_version: str,
    codex_sha256: str,
    argv: list[str],
) -> None:
    legacy = receipt.get("protocol") == "implement-v13-codex/process-receipt/1"
    if not legacy and receipt.get("protocol") != PROCESS_RECEIPT_PROTOCOL:
        raise StateError("existing receipt protocol is unsupported")
    expected = {
        "protocol": receipt.get("protocol"),
        "receipt_id": spec["receipt_id"],
        "phase": spec.get("phase"),
        "phase_detail": spec.get("phase_detail"),
        "role": spec.get("role"),
        "attempt": spec.get("attempt", 1),
        "cwd": str(Path(spec["cwd"]).resolve()),
        "model": spec["model"],
        "reasoning": spec["reasoning"],
        "sandbox": spec.get("sandbox", "read-only"),
        "resume_thread_id": spec.get("resume_thread_id"),
        "controller_child": spec.get("controller_child", False),
        "argv": argv,
        "prompt_path": str(prompt_path),
        "schema_path": str(schema_path),
        "prompt_sha256": prompt_sha256,
        "schema_sha256": schema_sha256,
        "codex_version": codex_version,
        "codex_executable_sha256": codex_sha256,
    }
    network_access = _network_access_enabled(spec)
    if "network_access" in receipt:
        expected["network_access"] = network_access
        expected["required_capabilities"] = _required_capabilities(spec)
    elif network_access:
        raise StateError("existing receipt lacks network access authority evidence")
    if not legacy:
        package_digest, package_version, package_path = _controller_package_identity(spec)
        capability_path, capability_digest = _capability_manifest_identity(spec)
        scratch = _ephemeral_scratch_path(spec)
        expected.update(
            {
                "controller_package_digest": package_digest,
                "controller_package_version": package_version,
                "controller_package_path": package_path,
                "schema_transport_sha256": schema_sha256,
                "schema_compiler_version": COMPILER_VERSION,
                "capability_manifest_path": capability_path,
                "capability_manifest_sha256": capability_digest,
                "ephemeral_scratch": str(scratch) if scratch is not None else None,
            }
        )
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise StateError(f"existing receipt invocation mismatch: {field}")
    if "schema_source_path" in receipt:
        if receipt.get("schema_source_path") != str(schema_source_path):
            raise StateError("existing receipt invocation mismatch: schema_source_path")
        if receipt.get("schema_source_sha256") != schema_source_sha256:
            raise StateError("existing receipt invocation mismatch: schema_source_sha256")
    if "prompt_source_path" in receipt:
        if receipt.get("prompt_source_path") != str(prompt_source_path):
            raise StateError("existing receipt invocation mismatch: prompt_source_path")
        if receipt.get("prompt_source_sha256") != prompt_source_sha256:
            raise StateError("existing receipt invocation mismatch: prompt_source_sha256")


def _recover_validator_failure(
    *,
    receipt_path: Path,
    receipt: dict[str, Any],
    spec: dict[str, Any],
    validator: Any,
    validation_error: type[Exception],
    stdout_path: Path,
    stderr_path: Path,
    output_path: Path,
    child_spec_path: Path,
    exit_path: Path,
) -> dict[str, Any]:
    if receipt.get("validation_errors") != [VALIDATOR_UNAVAILABLE_ERROR]:
        raise StateError("receipt is terminal-failed; use a new attempt ID")
    expected_hash = sha256_bytes(canonical_bytes(spec.get("expected", {})))
    if receipt.get("expected_sha256") != expected_hash:
        raise StateError("validator-only receipt lacks its original semantic contract")
    required_hashes = {"prompt", "schema", "codex_executable", "stdout", "stderr", "output", "child_spec", "exit"}
    recorded_hashes = receipt.get("artifact_sha256")
    if not isinstance(recorded_hashes, dict) or set(recorded_hashes) != required_hashes:
        raise StateError("validator-only receipt lacks a complete artifact hash manifest")
    if receipt.get("exit_code") != 0 or receipt.get("timed_out") is not False:
        raise StateError("validator-only receipt has conflicting failure evidence")
    paths = {"stdout": stdout_path, "stderr": stderr_path, "output": output_path, "child_spec": child_spec_path, "exit": exit_path}
    if any(not path.is_file() for path in paths.values()):
        raise StateError("validator-only receipt artifact is missing")
    payloads = {name: path.read_bytes() for name, path in paths.items()}
    try:
        current_hashes = {
            "prompt": sha256_file(Path(str(receipt["prompt_path"]))),
            "schema": sha256_file(Path(str(receipt["schema_path"]))),
            "codex_executable": sha256_file(Path(str(receipt["argv"][0]))),
            **{name: sha256_bytes(payload) for name, payload in payloads.items()},
        }
    except (KeyError, IndexError, OSError, TypeError) as exc:
        raise StateError("validator-only receipt invocation evidence is unreadable") from exc
    if current_hashes != recorded_hashes:
        raise StateError("validator-only receipt artifact hash mismatch")
    try:
        child_spec = json.loads(payloads["child_spec"])
        exit_record = json.loads(payloads["exit"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("validator-only receipt JSON evidence is unreadable") from exc
    if child_spec != _child_spec(spec, receipt["argv"]):
        raise StateError("validator-only receipt child invocation mismatch")
    if exit_record != {"exit_code": 0}:
        raise StateError("validator-only receipt exit evidence mismatch")
    errors, event_types, document = _terminal_validation_errors(
        exit_code=0,
        stdout_path=stdout_path,
        output_path=output_path,
        spec=spec,
        validator=validator,
        validation_error=validation_error,
        stdout_bytes=payloads["stdout"],
        output_bytes=payloads["output"],
    )
    if errors or document is None or receipt.get("event_types") != event_types:
        raise StateError(f"same-attempt revalidation rejected: {'; '.join(errors) or 'event inventory mismatch'}")
    revision = receipt.get("state_revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise StateError("validator-only receipt has invalid state revision")
    revalidated_at = _utc_now()
    history = [{"prior_state_revision": revision, "prior_validation_errors": [VALIDATOR_UNAVAILABLE_ERROR], "artifact_sha256": current_hashes, "revalidated_at": revalidated_at}]
    return cas_update(
        receipt_path,
        revision,
        {"status": "succeeded", "validation_errors": [], "revalidated_at": revalidated_at, "revalidation_history": history},
    )


def _codex_argv(spec: dict[str, Any], codex: str, output: Path, schema: Path) -> list[str]:
    cwd = Path(spec["cwd"]).resolve()
    sandbox = spec.get("sandbox", "read-only")
    if sandbox not in {"read-only", "workspace-write"}:
        raise StateError("production subprocess sandbox must be read-only or workspace-write")
    writable_roots = _writable_roots(spec)
    network_access = _network_access_enabled(spec)
    common = [
        "--ignore-user-config",
        "--strict-config",
        "--disable",
        "multi_agent",
        "-m",
        str(spec["model"]),
        "-c",
        f'model_reasoning_effort="{spec["reasoning"]}"',
        "-c",
        'approval_policy="never"',
        "--output-schema",
        str(schema),
        "-o",
        str(output),
        "--json",
    ]
    if network_access:
        common.extend(["-c", "sandbox_workspace_write.network_access=true"])
    resume_thread_id = spec.get("resume_thread_id")
    if resume_thread_id is not None:
        if not isinstance(resume_thread_id, str) or not resume_thread_id:
            raise StateError("resume_thread_id must be a nonempty explicit thread ID")
        resume = [
            codex,
            "exec",
            "resume",
            *common,
            "-c",
            f'sandbox_mode="{sandbox}"',
        ]
        if writable_roots:
            resume.extend(
                [
                    "-c",
                    "sandbox_workspace_write.writable_roots="
                    + json.dumps([str(root) for root in writable_roots]),
                ]
            )
        return [*resume, resume_thread_id, "-"]
    launch = [
        codex,
        "exec",
        "-C",
        str(cwd),
        *common,
        "--sandbox",
        sandbox,
    ]
    for root in writable_roots:
        launch.extend(["--add-dir", str(root)])
    return [*launch, "-"]


def run(spec_path: Path) -> dict[str, Any]:
    """Execute one invocation spec and return its terminal receipt."""
    spec = read_json(spec_path)
    required = {"receipt_id", "cwd", "prompt_path", "schema_path", "artifact_dir", "model", "reasoning"}
    missing = sorted(required - spec.keys())
    if missing:
        raise StateError(f"invocation spec missing: {', '.join(missing)}")
    reasoning = spec["reasoning"]
    terra_medium_implementation_role = (
        (
            spec.get("phase") == "IMPLEMENTING"
            and spec.get("role") == "implementation_worker"
        )
        or (
            spec.get("phase") == "REVIEWING"
            and spec.get("role") == "code_fixer"
        )
    )
    if terra_medium_implementation_role and (
        spec.get("model"), reasoning
    ) != ("gpt-5.6-terra", "medium"):
        raise StateError(
            "implementation workers and code fixers require the Terra-medium implementation identity"
        )
    if reasoning not in {"low", "medium"}:
        raise StateError("reasoning must be low or medium")
    artifact_dir = Path(spec["artifact_dir"]).resolve()
    receipt_slug = str(spec["receipt_id"]).replace(":", "-").replace("/", "-")
    receipt_path = artifact_dir / f"{receipt_slug}.receipt.json"
    stdout_path = artifact_dir / f"{receipt_slug}.stdout.jsonl"
    stderr_path = artifact_dir / f"{receipt_slug}.stderr.log"
    output_path = artifact_dir / f"{receipt_slug}.output.json"
    child_spec_path = artifact_dir / f"{receipt_slug}.child.json"
    release_path = artifact_dir / f"{receipt_slug}.release"
    exit_path = artifact_dir / f"{receipt_slug}.exit.json"
    prompt_source_path = Path(spec["prompt_path"]).resolve()
    prompt_snapshot_path = artifact_dir / f"{receipt_slug}.prompt.txt"
    schema_source_path = Path(spec["schema_path"]).resolve()
    schema_snapshot_path = artifact_dir / f"{receipt_slug}.schema.json"
    existing_receipt = read_json(receipt_path) if receipt_path.exists() else None
    validator, validation_error, timeout, prompt_source_hash, schema_source_hash, schema, prompt = _preflight(
        spec, prompt_source_path, schema_source_path
    )
    codex, codex_version, codex_sha256 = _resolve_codex()
    if sha256_file(prompt_source_path) != prompt_source_hash or sha256_file(schema_source_path) != schema_source_hash:
        raise StateError("invocation input changed during controller preflight")
    scratch = _prepare_ephemeral_scratch(
        spec,
        create=existing_receipt is None,
        allow_consumed=(
            existing_receipt is not None
            and existing_receipt.get("status") in {"succeeded", "failed", "orphaned"}
            and existing_receipt.get("ephemeral_scratch_removed") is True
        ),
    )
    if existing_receipt is None:
        prompt_path = prompt_snapshot_path
        prompt_hash = prompt_source_hash
        schema_path = schema_snapshot_path
    else:
        prompt_path = Path(str(existing_receipt.get("prompt_path", prompt_source_path))).resolve()
        prompt_hash = sha256_file(prompt_path) if prompt_path != prompt_source_path else prompt_source_hash
        schema_path = Path(str(existing_receipt.get("schema_path", schema_source_path))).resolve()
        if schema_path != schema_source_path:
            _, _, schema_hash, _ = _build_validator(
                schema_path, provider_transport=True
            )
        else:
            schema_hash = schema_source_hash
    argv = _codex_argv(spec, codex, output_path, schema_path)
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(artifact_dir, 0o700)
    if existing_receipt is not None:
        receipt = existing_receipt
        _assert_receipt_matches(
            receipt,
            spec,
            prompt_path,
            schema_path,
            prompt_hash,
            schema_hash,
            prompt_source_path,
            prompt_source_hash,
            schema_source_path,
            schema_source_hash,
            codex_version,
            codex_sha256,
            argv,
        )
        status = receipt.get("status")
        if status == "succeeded":
            artifact_hashes = receipt.get("artifact_sha256")
            if (
                not isinstance(artifact_hashes, dict)
                or artifact_hashes.get("stdout") != sha256_file(stdout_path)
            ):
                raise StateError("terminal receipt stdout artifact hash mismatch")
            current_provider_usage = _provider_usage(_read_events(stdout_path))
            if (
                receipt.get("protocol") == PROCESS_RECEIPT_PROTOCOL
                and receipt.get("provider_usage") != current_provider_usage
            ):
                raise StateError("terminal receipt provider usage mismatch")
            errors, _, document = _terminal_validation_errors(
                exit_code=int(receipt.get("exit_code", -1)),
                stdout_path=stdout_path,
                output_path=output_path,
                spec=spec,
                validator=validator,
                validation_error=validation_error,
            )
            if errors or document is None or receipt.get("output_sha256") != sha256_file(output_path):
                raise StateError("terminal receipt artifact revalidation failed")
            return receipt
        if status == "failed":
            if (
                isinstance(receipt.get("interruption"), dict)
                and receipt["interruption"].get("marker") is True
            ):
                return receipt
            return _recover_validator_failure(
                receipt_path=receipt_path,
                receipt=receipt,
                spec=spec,
                validator=validator,
                validation_error=validation_error,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                output_path=output_path,
                child_spec_path=child_spec_path,
                exit_path=exit_path,
            )
        if status == "orphaned":
            raise StateError(f"receipt is terminal-{status}; use a new attempt ID")
        if status not in {"spawned_unconfirmed", "released", "running"}:
            raise StateError("prepared receipt has no proven child; recovery audit required")
        deadline = time.monotonic() + timeout
        try:
            while _process_matches(receipt) and time.monotonic() < deadline:
                time.sleep(0.1)
        except BaseException:
            verified = _terminate_owned_group(
                int(receipt["pid"]),
                int(receipt["process_group_id"]),
                str(receipt["process_start_fingerprint"]),
            )
            scratch_sha256, scratch_removed, scratch_errors = _audit_scratch(
                receipt
            )
            available = {
                name: sha256_file(path)
                for name, path in {
                    "stdout": stdout_path,
                    "stderr": stderr_path,
                    "output": output_path,
                    "child_spec": child_spec_path,
                    "exit": exit_path,
                }.items()
                if path.is_file()
            }
            # Recovery did not create this Popen object, so positive reaping
            # cannot be proven here. Persist terminal process evidence but do
            # not authorize ledger reconciliation.
            receipt.update(
                status="failed",
                completed_at=_utc_now(),
                validation_errors=[
                    "controller interrupted recovery",
                    *scratch_errors,
                ],
                terminal_cause={
                    "class": "controller_interrupted",
                    "retryable": False,
                    "provider_code": None,
                    "http_status": None,
                    "message": "controller interrupted recovery",
                },
                interruption={
                    "marker": True,
                    "termination_status": "unverified",
                    "supervisor_reaped": False,
                    "owned_group_termination_requested": verified,
                    "available_artifact_sha256": available,
                },
                provider_usage=_provider_usage_or_unknown(_read_events(stdout_path)),
                ephemeral_scratch_contents_sha256=scratch_sha256,
                ephemeral_scratch_removed=scratch_removed,
                timed_out=False,
                state_revision=int(receipt.get("state_revision", 0)) + 1,
            )
            atomic_write_json(receipt_path, receipt)
            raise
        if _process_matches(receipt):
            _terminate_owned_group(
                int(receipt["pid"]),
                int(receipt["process_group_id"]),
                str(receipt["process_start_fingerprint"]),
            )
            receipt.update(status="failed", completed_at=_utc_now(), validation_errors=["wall timeout during recovery"], state_revision=int(receipt.get("state_revision", 0)) + 1)
            atomic_write_json(receipt_path, receipt)
            raise StateError("wall timeout during recovery")
        if not exit_path.is_file():
            receipt.update(status="orphaned", completed_at=_utc_now(), validation_errors=["process exited without durable exit status"], state_revision=int(receipt.get("state_revision", 0)) + 1)
            atomic_write_json(receipt_path, receipt)
            raise StateError("nonterminal receipt became orphaned without exit status")
        exit_record = read_json(exit_path)
        return _finalize_receipt(
            receipt_path=receipt_path,
            receipt=receipt,
            exit_code=int(exit_record["exit_code"]),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            child_spec_path=child_spec_path,
            exit_path=exit_path,
            spec=spec,
            validator=validator,
            validation_error=validation_error,
        )
    atomic_write_bytes(prompt_snapshot_path, prompt)
    atomic_write_json(schema_snapshot_path, schema)
    _, _, schema_hash, _ = _build_validator(
        schema_snapshot_path, provider_transport=True
    )
    package_digest, package_version, package_path = _controller_package_identity(spec)
    capability_path, capability_digest = _capability_manifest_identity(spec)
    now = _utc_now()
    receipt: dict[str, Any] = {
        "protocol": PROCESS_RECEIPT_PROTOCOL,
        "receipt_id": spec["receipt_id"],
        "queue_run_id": spec.get("queue_run_id"),
        "feature_run_id": spec.get("feature_run_id"),
        "phase": spec.get("phase"),
        "phase_detail": spec.get("phase_detail"),
        "role": spec.get("role"),
        "attempt": spec.get("attempt", 1),
        "model": spec["model"],
        "reasoning": spec["reasoning"],
        "sandbox": spec.get("sandbox", "read-only"),
        "network_access": _network_access_enabled(spec),
        "required_capabilities": _required_capabilities(spec),
        "writable_roots": [str(path) for path in _writable_roots(spec)],
        "ephemeral_scratch": str(scratch) if scratch is not None else None,
        "ephemeral_scratch_contents_sha256": None,
        "ephemeral_scratch_removed": scratch is None,
        "capability_manifest_path": capability_path,
        "capability_manifest_sha256": capability_digest,
        "resume_thread_id": spec.get("resume_thread_id"),
        "controller_child": spec.get("controller_child", False),
        "controller_package_digest": package_digest,
        "controller_package_version": package_version,
        "controller_package_path": package_path,
        "cwd": str(Path(spec["cwd"]).resolve()),
        "argv": argv,
        "codex_version": codex_version,
        "codex_executable_sha256": codex_sha256,
        "prompt_path": str(prompt_path),
        "prompt_sha256": prompt_hash,
        "prompt_source_path": str(prompt_source_path),
        "prompt_source_sha256": prompt_source_hash,
        "schema_path": str(schema_path),
        "schema_sha256": schema_hash,
        "schema_transport_sha256": schema_hash,
        "schema_source_path": str(schema_source_path),
        "schema_source_sha256": schema_source_hash,
        "schema_compiler_version": COMPILER_VERSION,
        "expected_sha256": sha256_bytes(canonical_bytes(spec.get("expected", {}))),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "output_path": str(output_path),
        "exit_path": str(exit_path),
        "status": "prepared",
        "terminal_cause": {
            "class": "none",
            "retryable": False,
            "provider_code": None,
            "http_status": None,
            "message": "",
        },
        "diagnostics": [],
        "prepared_at": now,
        "state_revision": 0,
    }
    atomic_write_json(receipt_path, receipt)
    child_spec = _child_spec(spec, argv, scratch)
    atomic_write_json(child_spec_path, child_spec)
    started = time.monotonic()
    with prompt_path.open("rb") as prompt, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("supervised_child.py")), str(child_spec_path), str(release_path), str(exit_path)],
            cwd=spec["cwd"],
            stdin=prompt,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        pid = process.pid
        pgid = os.getpgid(pid)
        fingerprint = _process_fingerprint(pid)
        receipt.update(
            status="spawned_unconfirmed",
            pid=pid,
            process_group_id=pgid,
            process_start_fingerprint=fingerprint,
            spawned_at=_utc_now(),
            state_revision=1,
        )
        atomic_write_json(receipt_path, receipt)
        _write_release(release_path)
        receipt.update(status="released", released_at=_utc_now(), state_revision=2)
        atomic_write_json(receipt_path, receipt)
        saw_thread = False
        timed_out = False
        try:
            while process.poll() is None:
                events = _read_events(stdout_path)
                if not saw_thread and _thread_id(events):
                    saw_thread = True
                    receipt.update(
                        status="running",
                        thread_id=_thread_id(events),
                        running_at=_utc_now(),
                        state_revision=3,
                    )
                    atomic_write_json(receipt_path, receipt)
                if time.monotonic() - started > timeout:
                    timed_out = True
                    _terminate_owned_group(pid, pgid, fingerprint)
                    break
                time.sleep(0.1)
            exit_code = process.wait()
        except BaseException:
            verified = _terminate_owned_group(pid, pgid, fingerprint)
            _finalize_interrupted_receipt(
                receipt_path=receipt_path,
                receipt=receipt,
                process=process,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                output_path=output_path,
                child_spec_path=child_spec_path,
                exit_path=exit_path,
                termination_verified=verified,
            )
            raise
    return _finalize_receipt(
        receipt_path=receipt_path,
        receipt=receipt,
        exit_code=exit_code,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        output_path=output_path,
        child_spec_path=child_spec_path,
        exit_path=exit_path,
        spec=spec,
        validator=validator,
        validation_error=validation_error,
        timed_out=timed_out,
    )


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", type=Path)
    arguments = parser.parse_args()
    try:
        receipt = run(arguments.spec.resolve())
    except StateError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
