"""Operator-attested, repository-bound admission for PlanGraph."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
from typing import Mapping, Sequence

from harness_labs.plan_graph import ApprovalEvidence, plan_from_mapping, validate_plan_graph_plan
from harness_labs.plan_graph_contract import (
    PlanGraphContractError,
    canonical_json,
    canonical_plan_graph_payload,
    load_repository_id,
    plan_graph_identity,
    sha256_bytes,
    sha256_json,
)


SUBJECT_PROTOCOL = "plan-approval-subject/1"
GATE_PROTOCOL = "plan-approval-gates/1"
OPERATOR_APPROVAL_PROTOCOL = "plan-operator-approval/1"
RECEIPT_PROTOCOL = "plan-approval-receipt/1"
POLICY_ID = "operator-attested-plan-approval/1"
REPOSITORY_IDENTITY_PATH = ".harness/repository.json"
MAX_TIMEOUT_SECONDS = 7200.0


class PlanApprovalError(ValueError):
    """Raised when approval evidence is absent, invalid, or stale."""


@dataclass(frozen=True)
class PreparedApproval:
    subject_path: Path
    gate_evidence_path: Path
    subject_sha256: str
    plan_graph_digest: str


@dataclass(frozen=True)
class ValidatedApproval:
    decomposition: Mapping[str, object]
    decomposition_path: str
    base_commit: str
    repository_id: str
    plan_sha256: str
    subject_sha256: str
    receipt_sha256: str
    plan_graph_digest: str
    audit_record: Mapping[str, object]

    @property
    def evidence(self) -> ApprovalEvidence:
        return ApprovalEvidence(
            self.subject_sha256,
            self.receipt_sha256,
            self.plan_graph_digest,
            self.audit_record,
        )


def prepare_approval(
    *,
    repository: Path,
    decomposition_path: Path,
    output_directory: Path,
) -> PreparedApproval:
    """Freeze Git inputs and run deterministic admission gates."""

    repository = repository.resolve()
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    base_commit = _git(repository, "rev-parse", "HEAD")
    decomposition_relative = _relative_repository_path(
        repository, decomposition_path.resolve(), "decomposition"
    )
    decomposition_record, decomposition_raw = _git_artifact(
        repository, base_commit, decomposition_relative
    )
    try:
        working_raw = decomposition_path.resolve().read_bytes()
    except OSError as exc:
        raise PlanApprovalError(f"could not read decomposition: {exc}") from exc
    if working_raw != decomposition_raw:
        raise PlanApprovalError(
            "working decomposition does not match the blob at base_commit"
        )
    decomposition = _load_json_bytes(decomposition_raw, "decomposition")
    try:
        canonical = canonical_plan_graph_payload(decomposition)
    except PlanGraphContractError as exc:
        raise PlanApprovalError(str(exc)) from exc

    identity_record, identity_raw = _git_artifact(
        repository, base_commit, REPOSITORY_IDENTITY_PATH
    )
    identity_payload = _load_json_bytes(identity_raw, "repository identity")
    try:
        repository_id = load_repository_id(identity_payload)
    except PlanGraphContractError as exc:
        raise PlanApprovalError(str(exc)) from exc

    plan_record, _ = _git_artifact(repository, base_commit, str(canonical["plan"]))
    referenced = []
    for path in canonical["referenced_artifacts"]:
        record, _ = _git_artifact(repository, base_commit, str(path))
        referenced.append(record)

    policy = {
        "policy_id": POLICY_ID,
        "reviewer_profile_digests": [],
        "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
    }
    policy["policy_sha256"] = sha256_json(policy)
    graph_digest = plan_graph_identity(
        repository_id=repository_id,
        base_commit=base_commit,
        plan_sha256=plan_record["sha256"],
        decomposition=canonical,
    )
    subject = {
        "protocol": SUBJECT_PROTOCOL,
        "repository": {
            "identity": {
                "id": repository_id,
                **identity_record,
            },
            "base_commit": base_commit,
        },
        "plan": plan_record,
        "decomposition": {
            "protocol": canonical["protocol"],
            **decomposition_record,
            "canonical_sha256": sha256_json(canonical),
        },
        "referenced_artifacts": referenced,
        "review_policy": policy,
        "plan_graph_digest": graph_digest,
    }
    subject_sha = sha256_json(subject)
    gates = _run_static_gates(
        repository=repository,
        base_commit=base_commit,
        repository_id=repository_id,
        plan_sha256=plan_record["sha256"],
        decomposition=canonical,
        subject_sha256=subject_sha,
    )
    subject_path = output_directory / "subject.json"
    gate_path = output_directory / "gate-evidence.json"
    _write_json(subject_path, subject)
    _write_json(gate_path, gates)
    return PreparedApproval(subject_path, gate_path, subject_sha, graph_digest)


def issue_receipt(
    *,
    repository: Path,
    subject_path: Path,
    gate_evidence_path: Path,
    operator_approval_path: Path,
    receipt_path: Path,
) -> Path:
    """Issue an immutable receipt for one explicit operator attestation."""

    subject = _load_json_file(subject_path, "approval subject")
    _validate_subject_shape(subject)
    subject_sha = sha256_json(subject)
    (
        decomposition,
        base_commit,
        repository_id,
        plan_sha256,
        _,
    ) = _validate_subject_against_repository(repository.resolve(), subject)
    gates = _load_json_file(gate_evidence_path, "gate evidence")
    _validate_gate_evidence(gates)
    if gates.get("subject_sha256") != subject_sha:
        raise PlanApprovalError("gate evidence does not match the approval subject")
    if gates.get("plan_graph_digest") != subject["plan_graph_digest"]:
        raise PlanApprovalError("gate evidence PlanGraph identity mismatch")
    _revalidate_host_executables(gates.get("host_executables"))
    fresh_gates = _run_static_gates(
        repository=repository.resolve(),
        base_commit=base_commit,
        repository_id=repository_id,
        plan_sha256=plan_sha256,
        decomposition=decomposition,
        subject_sha256=subject_sha,
    )
    for field in (
        "status",
        "subject_sha256",
        "plan_graph_digest",
        "host_path",
        "host_executables",
    ):
        if gates.get(field) != fresh_gates[field]:
            raise PlanApprovalError(
                f"gate evidence {field} does not match fresh controller checks"
            )
    operator = _load_json_file(operator_approval_path, "operator approval")
    _validate_operator_approval(operator)
    if operator.get("subject_sha256") != subject_sha:
        raise PlanApprovalError("operator approval does not match the subject")
    receipt_path = receipt_path.resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "protocol": RECEIPT_PROTOCOL,
        "status": "approved",
        "subject": _artifact_reference(subject_path.resolve(), receipt_path.parent),
        "gate_evidence": _artifact_reference(
            gate_evidence_path.resolve(), receipt_path.parent
        ),
        "operator_approval": _artifact_reference(
            operator_approval_path.resolve(), receipt_path.parent
        ),
        "policy_id": POLICY_ID,
        "plan_graph_digest": subject["plan_graph_digest"],
        "controller": {"id": "plan-approval", "role": "admission-controller"},
        "created_at": _timestamp(),
    }
    _write_json(receipt_path, receipt)
    return receipt_path


class PlanApprovalAdmission:
    """Load and revalidate one approval receipt against Git and host state."""

    def __init__(self, *, repository: Path, receipt_path: Path) -> None:
        self.repository = repository.resolve()
        self.receipt_path = receipt_path.resolve()

    def validate(self) -> ValidatedApproval:
        receipt = _load_json_file(self.receipt_path, "approval receipt")
        _validate_receipt_shape(receipt)
        if receipt["status"] != "approved" or receipt["policy_id"] != POLICY_ID:
            raise PlanApprovalError("receipt is not approved under the supported policy")
        subject = _load_referenced_json(
            receipt["subject"], self.receipt_path.parent, "approval subject"
        )
        _validate_subject_shape(subject)
        subject_sha = sha256_json(subject)
        if receipt["plan_graph_digest"] != subject["plan_graph_digest"]:
            raise PlanApprovalError("receipt PlanGraph identity mismatch")
        gates = _load_referenced_json(
            receipt["gate_evidence"], self.receipt_path.parent, "gate evidence"
        )
        _validate_gate_evidence(gates)
        if (
            gates.get("subject_sha256") != subject_sha
            or gates.get("plan_graph_digest") != subject["plan_graph_digest"]
        ):
            raise PlanApprovalError("gate evidence is invalid or belongs to another subject")
        operator = _load_referenced_json(
            receipt["operator_approval"],
            self.receipt_path.parent,
            "operator approval",
        )
        _validate_operator_approval(operator)
        if (
            operator.get("subject_sha256") != subject_sha
        ):
            raise PlanApprovalError("operator approval does not match the subject")

        (
            decomposition,
            base_commit,
            repository_id,
            plan_sha256,
            graph_digest,
        ) = _validate_subject_against_repository(self.repository, subject)
        _revalidate_host_executables(gates.get("host_executables"))
        return ValidatedApproval(
            decomposition=decomposition,
            decomposition_path=str(subject["decomposition"]["path"]),
            base_commit=base_commit,
            repository_id=repository_id,
            plan_sha256=plan_sha256,
            subject_sha256=subject_sha,
            receipt_sha256=sha256_bytes(self.receipt_path.read_bytes()),
            plan_graph_digest=graph_digest,
            audit_record={
                "protocol": "plan-approval-audit-record/1",
                "subject_sha256": subject_sha,
                "receipt_sha256": sha256_bytes(self.receipt_path.read_bytes()),
                "receipt_path": str(self.receipt_path),
                "policy_id": receipt["policy_id"],
                "subject": dict(subject),
                "operator_approval": dict(operator),
                "gate_evidence": dict(gates),
            },
        )

    def approval_validator(self):
        return lambda: self.validate().evidence


def _validate_subject_against_repository(
    repository_root: Path, subject: Mapping[str, object]
) -> tuple[Mapping[str, object], str, str, str, str]:
    repository = subject["repository"]
    base_commit = repository["base_commit"]
    identity = repository["identity"]
    identity_raw = _verify_git_artifact(
        repository_root, base_commit, identity, "repository identity"
    )
    try:
        repository_id = load_repository_id(
            _load_json_bytes(identity_raw, "repository identity")
        )
    except PlanGraphContractError as exc:
        raise PlanApprovalError(str(exc)) from exc
    if repository_id != identity["id"]:
        raise PlanApprovalError("repository identity content mismatch")
    plan_raw = _verify_git_artifact(
        repository_root, base_commit, subject["plan"], "plan"
    )
    decomposition_raw = _verify_git_artifact(
        repository_root,
        base_commit,
        subject["decomposition"],
        "decomposition",
    )
    for artifact in subject["referenced_artifacts"]:
        _verify_git_artifact(
            repository_root, base_commit, artifact, "referenced artifact"
        )
    try:
        decomposition = canonical_plan_graph_payload(
            _load_json_bytes(decomposition_raw, "decomposition")
        )
    except PlanGraphContractError as exc:
        raise PlanApprovalError(str(exc)) from exc
    if sha256_json(decomposition) != subject["decomposition"]["canonical_sha256"]:
        raise PlanApprovalError("canonical decomposition digest mismatch")
    plan_sha256 = sha256_bytes(plan_raw)
    graph_digest = plan_graph_identity(
        repository_id=repository_id,
        base_commit=base_commit,
        plan_sha256=plan_sha256,
        decomposition=decomposition,
    )
    if graph_digest != subject["plan_graph_digest"]:
        raise PlanApprovalError("approval subject PlanGraph identity mismatch")
    return decomposition, base_commit, repository_id, plan_sha256, graph_digest


def _run_static_gates(
    *,
    repository: Path,
    base_commit: str,
    repository_id: str,
    plan_sha256: str,
    decomposition: Mapping[str, object],
    subject_sha256: str,
) -> dict[str, object]:
    plan = plan_from_mapping(
        decomposition,
        base_commit=base_commit,
        repository_id=repository_id,
        plan_sha256=plan_sha256,
    )
    identity = plan_graph_identity(
        repository_id=repository_id,
        base_commit=base_commit,
        plan_sha256=plan_sha256,
        decomposition=decomposition,
    )
    validate_plan_graph_plan(plan)
    host_executables: list[dict[str, object]] = []
    for run in plan.runs:
        if run.verification_timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise PlanApprovalError(
                f"run {run.id!r} verification timeout exceeds policy"
            )
        _validate_intents(repository, base_commit, run.id, run.path_intents)
        _validate_base_required_paths(
            repository, base_commit, run.verification_required_paths
        )
        if run.verification_argv:
            evidence = _executable_evidence(
                repository, base_commit, run.verification_argv,
                run.verification_required_paths,
            )
            if evidence is not None:
                host_executables.append({"consumer": run.id, **evidence})
        for gate in run.verification_gates:
            if gate.timeout_seconds > MAX_TIMEOUT_SECONDS:
                raise PlanApprovalError(
                    f"run {run.id!r} gate {gate.name!r} timeout exceeds policy"
                )
            gate_evidence = _executable_evidence(
                repository, base_commit, gate.argv,
                run.verification_required_paths,
            )
            if gate_evidence is not None:
                host_executables.append(
                    {"consumer": f"{run.id}:{gate.name}", **gate_evidence}
                )
    for index, command in enumerate(plan.functionality_tests):
        if command.timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise PlanApprovalError(
                f"functionality test {index} timeout exceeds policy"
            )
        _validate_base_required_paths(repository, base_commit, command.required_paths)
        evidence = _executable_evidence(
            repository, base_commit, command.argv, command.required_paths
        )
        if evidence is not None:
            host_executables.append({"consumer": f"functionality:{index}", **evidence})
    return {
        "protocol": GATE_PROTOCOL,
        "status": "passed",
        "subject_sha256": subject_sha256,
        "plan_graph_digest": identity,
        "host_path": os.environ.get("PATH", ""),
        "host_executables": host_executables,
        "checked_at": _timestamp(),
    }


def _validate_intents(
    repository: Path,
    base_commit: str,
    run_id: str,
    intents: Sequence[object],
) -> None:
    for intent in intents:
        path = intent.path
        exists = _git_path_exists(repository, base_commit, path)
        if intent.action == "modify" and not exists:
            raise PlanApprovalError(
                f"run {run_id!r} modify path {path!r} is absent at base_commit"
            )
        if intent.action == "create" and exists:
            raise PlanApprovalError(
                f"run {run_id!r} create path {path!r} already exists at base_commit"
            )


def _validate_base_required_paths(
    repository: Path, base_commit: str, required_paths: Sequence[object]
) -> None:
    for required in required_paths:
        if required.availability == "base" and not _git_path_exists(
            repository, base_commit, required.path
        ):
            raise PlanApprovalError(
                f"required base path {required.path!r} is absent at base_commit"
            )


def _executable_evidence(
    repository: Path,
    base_commit: str,
    argv: Sequence[str],
    required_paths: Sequence[object],
) -> dict[str, object] | None:
    executable = argv[0]
    if os.path.isabs(executable):
        return {
            "requested": executable,
            **_host_executable_identity(Path(executable)),
        }
    if "/" not in executable:
        resolved = shutil.which(executable)
        if resolved is None:
            raise PlanApprovalError(f"executable {executable!r} is not on PATH")
        return {"requested": executable, **_host_executable_identity(Path(resolved))}
    path = PurePosixPath(executable)
    declared = next(
        (required for required in required_paths if required.path == str(path)), None
    )
    if declared is None:
        raise PlanApprovalError(
            f"repository executable {executable!r} is not in required_paths"
        )
    if declared.availability == "base":
        mode = _git(repository, "ls-tree", base_commit, "--", executable)
        if not mode or not mode.split()[0].endswith("755"):
            raise PlanApprovalError(
                f"repository executable {executable!r} is absent or not executable"
            )
    return None


def _host_executable_identity(path: Path) -> dict[str, object]:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        raw = resolved.read_bytes()
    except OSError as exc:
        raise PlanApprovalError(f"host executable {path} is unavailable: {exc}") from exc
    if not metadata.st_mode & stat.S_IXUSR:
        raise PlanApprovalError(f"host executable {resolved} is not executable")
    return {
        "path": str(resolved),
        "sha256": sha256_bytes(raw),
        "size_bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def _revalidate_host_executables(value: object) -> None:
    if not isinstance(value, list):
        raise PlanApprovalError("gate evidence host_executables must be an array")
    for index, expected in enumerate(value):
        if not isinstance(expected, Mapping) or not isinstance(expected.get("path"), str):
            raise PlanApprovalError(f"host executable evidence {index} is invalid")
        requested = expected.get("requested")
        if isinstance(requested, str):
            try:
                if os.path.isabs(requested):
                    resolved_now = str(Path(requested).resolve(strict=True))
                else:
                    resolved = shutil.which(requested)
                    if resolved is None:
                        raise PlanApprovalError(
                            f"host executable is no longer on PATH: {requested}"
                        )
                    resolved_now = str(Path(resolved).resolve(strict=True))
            except OSError as exc:
                raise PlanApprovalError(
                    f"host executable is unavailable: {requested}: {exc}"
                ) from exc
            if resolved_now != expected["path"]:
                raise PlanApprovalError(
                    f"host executable resolution changed: {requested}"
                )
        actual = _host_executable_identity(Path(expected["path"]))
        for field in ("path", "sha256", "size_bytes", "mtime_ns"):
            if actual[field] != expected.get(field):
                raise PlanApprovalError(
                    f"host executable identity changed: {expected['path']}"
                )


def _git_artifact(
    repository: Path, commit: str, path: str
) -> tuple[dict[str, object], bytes]:
    try:
        blob = _git(repository, "rev-parse", f"{commit}:{path}")
        completed = subprocess.run(
            ["git", "-C", str(repository), "show", f"{commit}:{path}"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise PlanApprovalError(f"could not resolve {path!r}: {exc}") from exc
    if completed.returncode:
        raise PlanApprovalError(f"path {path!r} is absent from {commit}")
    raw = completed.stdout
    return {"path": path, "git_blob": blob, "sha256": sha256_bytes(raw)}, raw


def _verify_git_artifact(
    repository: Path,
    commit: str,
    expected: Mapping[str, object],
    label: str,
) -> bytes:
    path = expected.get("path")
    if not isinstance(path, str):
        raise PlanApprovalError(f"{label} path is invalid")
    actual, raw = _git_artifact(repository, commit, path)
    for field in ("path", "git_blob", "sha256"):
        if actual[field] != expected.get(field):
            raise PlanApprovalError(f"{label} {field} does not match approval")
    return raw


def _git_path_exists(repository: Path, commit: str, path: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stdout + completed.stderr).strip()
        raise PlanApprovalError(detail or f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _relative_repository_path(repository: Path, path: Path, label: str) -> str:
    try:
        relative = path.relative_to(repository).as_posix()
    except ValueError as exc:
        raise PlanApprovalError(f"{label} must be inside the repository") from exc
    if not relative or relative == ".":
        raise PlanApprovalError(f"{label} must name a repository file")
    return relative


def _artifact_reference(path: Path, relative_to: Path) -> dict[str, object]:
    return {
        "path": os.path.relpath(path, relative_to),
        "sha256": sha256_bytes(path.read_bytes()),
    }


def _load_referenced_json(
    reference: object, relative_to: Path, label: str
) -> Mapping[str, object]:
    if not isinstance(reference, Mapping):
        raise PlanApprovalError(f"{label} reference must be an object")
    _require_exact_keys(reference, {"path", "sha256"}, f"{label} reference")
    path_value = reference.get("path")
    digest = reference.get("sha256")
    if not isinstance(path_value, str) or not isinstance(digest, str):
        raise PlanApprovalError(f"{label} reference is invalid")
    path = (relative_to / path_value).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanApprovalError(f"could not read {label}: {exc}") from exc
    if sha256_bytes(raw) != digest:
        raise PlanApprovalError(f"{label} artifact digest mismatch")
    return _load_json_bytes(raw, label)


def _validate_subject_shape(subject: Mapping[str, object]) -> None:
    _require_exact_keys(
        subject,
        {
            "protocol",
            "repository",
            "plan",
            "decomposition",
            "referenced_artifacts",
            "review_policy",
            "plan_graph_digest",
        },
        "approval subject",
    )
    if subject.get("protocol") != SUBJECT_PROTOCOL:
        raise PlanApprovalError("unsupported approval subject protocol")
    repository = subject.get("repository")
    if not isinstance(repository, Mapping):
        raise PlanApprovalError("approval subject repository is invalid")
    _require_exact_keys(repository, {"identity", "base_commit"}, "subject repository")
    _require_hex(repository.get("base_commit"), 40, "subject base_commit")
    identity = repository.get("identity")
    if not isinstance(identity, Mapping):
        raise PlanApprovalError("subject repository identity is invalid")
    _require_exact_keys(identity, {"id", "path", "git_blob", "sha256"}, "subject identity")
    if not isinstance(identity.get("id"), str) or not identity["id"]:
        raise PlanApprovalError("subject repository id is invalid")
    _validate_git_artifact(identity, "subject identity", extra={"id"})
    plan = subject.get("plan")
    decomposition = subject.get("decomposition")
    if not isinstance(plan, Mapping) or not isinstance(decomposition, Mapping):
        raise PlanApprovalError("subject plan or decomposition is invalid")
    _validate_git_artifact(plan, "subject plan")
    _validate_git_artifact(
        decomposition,
        "subject decomposition",
        extra={"protocol", "canonical_sha256"},
    )
    if decomposition.get("protocol") != "plan-graph-plan/1":
        raise PlanApprovalError("subject decomposition protocol is invalid")
    _require_hex(
        decomposition.get("canonical_sha256"), 64, "canonical decomposition digest"
    )
    artifacts = subject.get("referenced_artifacts")
    if not isinstance(artifacts, list):
        raise PlanApprovalError("subject referenced_artifacts must be an array")
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise PlanApprovalError(f"referenced artifact {index} is invalid")
        _validate_git_artifact(artifact, f"referenced artifact {index}")
    policy = subject.get("review_policy")
    if not isinstance(policy, Mapping):
        raise PlanApprovalError("subject review policy is invalid")
    _require_exact_keys(
        policy,
        {
            "policy_id",
            "reviewer_profile_digests",
            "max_timeout_seconds",
            "policy_sha256",
        },
        "subject review policy",
    )
    if policy.get("policy_id") != POLICY_ID:
        raise PlanApprovalError("subject review policy is unsupported")
    profiles = policy.get("reviewer_profile_digests")
    if not isinstance(profiles, list):
        raise PlanApprovalError("subject reviewer_profile_digests must be an array")
    for index, digest in enumerate(profiles):
        _require_hex(digest, 64, f"reviewer profile digest {index}")
    if policy.get("max_timeout_seconds") != MAX_TIMEOUT_SECONDS:
        raise PlanApprovalError("subject timeout policy is unsupported")
    expected_policy_digest = sha256_json(
        {key: value for key, value in policy.items() if key != "policy_sha256"}
    )
    if policy.get("policy_sha256") != expected_policy_digest:
        raise PlanApprovalError("subject review policy digest mismatch")
    _require_hex(subject.get("plan_graph_digest"), 64, "subject PlanGraph digest")


def _validate_receipt_shape(receipt: Mapping[str, object]) -> None:
    _require_exact_keys(
        receipt,
        {
            "protocol",
            "status",
            "subject",
            "gate_evidence",
            "operator_approval",
            "policy_id",
            "plan_graph_digest",
            "controller",
            "created_at",
        },
        "approval receipt",
    )
    if receipt.get("protocol") != RECEIPT_PROTOCOL:
        raise PlanApprovalError("unsupported approval receipt protocol")
    for field in ("subject", "gate_evidence", "operator_approval"):
        reference = receipt.get(field)
        if not isinstance(reference, Mapping):
            raise PlanApprovalError(f"receipt {field} reference is invalid")
        _require_exact_keys(reference, {"path", "sha256"}, f"receipt {field}")
        if not isinstance(reference.get("path"), str) or not reference["path"]:
            raise PlanApprovalError(f"receipt {field} path is invalid")
        _require_hex(reference.get("sha256"), 64, f"receipt {field} digest")
    _require_hex(receipt.get("plan_graph_digest"), 64, "receipt PlanGraph digest")
    controller = receipt.get("controller")
    if not isinstance(controller, Mapping):
        raise PlanApprovalError("receipt controller is invalid")
    _require_exact_keys(controller, {"id", "role"}, "receipt controller")
    for field in ("id", "role"):
        if not isinstance(controller.get(field), str) or not controller[field]:
            raise PlanApprovalError(f"receipt controller {field} is invalid")
    if not isinstance(receipt.get("created_at"), str) or not receipt["created_at"]:
        raise PlanApprovalError("receipt created_at is invalid")
    _validate_timestamp(receipt["created_at"], "receipt created_at")


def _validate_gate_evidence(gates: Mapping[str, object]) -> None:
    _require_exact_keys(
        gates,
        {
            "protocol",
            "status",
            "subject_sha256",
            "plan_graph_digest",
            "host_path",
            "host_executables",
            "checked_at",
        },
        "gate evidence",
    )
    if gates.get("protocol") != GATE_PROTOCOL or gates.get("status") != "passed":
        raise PlanApprovalError("gate evidence is not a passed approval gate result")
    _require_hex(gates.get("subject_sha256"), 64, "gate subject digest")
    _require_hex(gates.get("plan_graph_digest"), 64, "gate PlanGraph digest")
    if not isinstance(gates.get("host_path"), str):
        raise PlanApprovalError("gate host_path is invalid")
    if not isinstance(gates.get("checked_at"), str) or not gates["checked_at"]:
        raise PlanApprovalError("gate checked_at is invalid")
    _validate_timestamp(gates["checked_at"], "gate checked_at")
    if not isinstance(gates.get("host_executables"), list):
        raise PlanApprovalError("gate host_executables must be an array")
    for index, executable in enumerate(gates["host_executables"]):
        if not isinstance(executable, Mapping):
            raise PlanApprovalError(f"host executable evidence {index} is invalid")
        required = {"consumer", "path", "sha256", "size_bytes", "mtime_ns"}
        allowed = required | {"requested"}
        if not required.issubset(executable) or not set(executable).issubset(allowed):
            raise PlanApprovalError(f"host executable evidence {index} has invalid fields")
        for field in ("consumer", "path"):
            if not isinstance(executable.get(field), str) or not executable[field]:
                raise PlanApprovalError(
                    f"host executable evidence {index} {field} is invalid"
                )
        if "requested" in executable and (
            not isinstance(executable["requested"], str) or not executable["requested"]
        ):
            raise PlanApprovalError(
                f"host executable evidence {index} requested is invalid"
            )
        _require_hex(executable.get("sha256"), 64, f"host executable {index} digest")
        for field in ("size_bytes", "mtime_ns"):
            if (
                isinstance(executable.get(field), bool)
                or not isinstance(executable.get(field), int)
                or executable[field] < 0
            ):
                raise PlanApprovalError(
                    f"host executable evidence {index} {field} is invalid"
                )


def _validate_operator_approval(operator: Mapping[str, object]) -> None:
    _require_exact_keys(
        operator,
        {"protocol", "subject_sha256", "actor", "approved_at", "statement"},
        "operator approval",
    )
    if operator.get("protocol") != OPERATOR_APPROVAL_PROTOCOL:
        raise PlanApprovalError("unsupported operator approval protocol")
    _require_hex(operator.get("subject_sha256"), 64, "operator subject digest")
    for field in ("actor", "approved_at", "statement"):
        if not isinstance(operator.get(field), str) or not operator[field].strip():
            raise PlanApprovalError(f"operator approval {field} is required")
    _validate_timestamp(operator["approved_at"], "operator approval approved_at")


def _validate_git_artifact(
    artifact: Mapping[str, object], label: str, *, extra: set[str] | None = None
) -> None:
    expected = {"path", "git_blob", "sha256"} | (extra or set())
    _require_exact_keys(artifact, expected, label)
    if not isinstance(artifact.get("path"), str) or not artifact["path"]:
        raise PlanApprovalError(f"{label} path is invalid")
    _require_hex(artifact.get("git_blob"), 40, f"{label} git blob")
    _require_hex(artifact.get("sha256"), 64, f"{label} sha256")


def _require_hex(value: object, length: int, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PlanApprovalError(f"{label} must be {length} lowercase hex characters")


def _validate_timestamp(value: object, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanApprovalError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise PlanApprovalError(f"{label} must include a timezone")


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise PlanApprovalError(f"{label} has invalid fields")


def _load_json_file(path: Path, label: str) -> Mapping[str, object]:
    try:
        return _load_json_bytes(path.read_bytes(), label)
    except OSError as exc:
        raise PlanApprovalError(f"could not read {label}: {exc}") from exc


def _load_json_bytes(raw: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanApprovalError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise PlanApprovalError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    raw = (canonical_json(value) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "GATE_PROTOCOL",
    "OPERATOR_APPROVAL_PROTOCOL",
    "POLICY_ID",
    "PlanApprovalAdmission",
    "PlanApprovalError",
    "PreparedApproval",
    "RECEIPT_PROTOCOL",
    "SUBJECT_PROTOCOL",
    "ValidatedApproval",
    "issue_receipt",
    "prepare_approval",
]
