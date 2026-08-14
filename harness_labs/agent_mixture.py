"""Declarative per-role agent mixtures for FeatureRun worker scheduling.

A FeatureRun already chooses its agents seat-by-seat: the coordinator through
``session_factory`` and each worker role through a
:class:`~harness_labs.controller_scheduler.RoleProfile` executor factory. This
module adds the declarative layer on top: a run (or a PlanGraph node packet)
names its mixture as ``{role: "provider:model@effort"}`` and gets back the
``RoleProfile`` tuple, so which agent runs which role becomes recorded
configuration instead of ad-hoc script wiring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .agent_sessions import AgentSession
from .audit import AuditJournal
from .claude_agent_session import ClaudeAgentSession
from .claude_task_executor import ClaudeSemanticTaskExecutor
from .codex_agent_session import CodexAppServerSession
from .controller_evidence import EvidenceCatalog
from .controller_live import (
    CodexSemanticTaskExecutor,
    select_dirty_baseline_receipt,
)
from .controller_scheduler import RoleProfile
from .git_transaction import workspace_snapshot
from .usage import ModelPrice


_PROVIDER_BACKEND_IDS = {
    "claude": "claude-print",
    "codex": "codex-exec",
}
_PROVIDER_EXECUTABLES = {
    "claude": "claude",
    "codex": "codex",
}
_DEFAULT_MIXTURE_KEY = "*"


@dataclass(frozen=True)
class BackendSpec:
    """One worker backend choice: which provider, model, and effort."""

    provider: str
    model: str
    effort: str = "medium"

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDER_BACKEND_IDS:
            raise ValueError(
                "backend provider must be one of: "
                + ", ".join(sorted(_PROVIDER_BACKEND_IDS))
            )
        if not self.model.strip():
            raise ValueError("backend model must be non-empty")
        if not self.effort.strip():
            raise ValueError("backend effort must be non-empty")

    @property
    def backend_id(self) -> str:
        return _PROVIDER_BACKEND_IDS[self.provider]

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "backend_id": self.backend_id,
        }


def parse_backend_spec(value: str | BackendSpec) -> BackendSpec:
    """Parse ``provider:model[@effort]``, e.g. ``claude:claude-opus-5@high``."""

    if isinstance(value, BackendSpec):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("backend spec must be a non-empty string")
    provider, separator, remainder = value.partition(":")
    if not separator or not remainder.strip():
        raise ValueError(
            f"backend spec must look like provider:model[@effort]: {value!r}"
        )
    model, _, effort = remainder.partition("@")
    if not model.strip():
        raise ValueError(f"backend spec names no model: {value!r}")
    if effort.strip():
        return BackendSpec(provider.strip(), model.strip(), effort.strip())
    return BackendSpec(provider.strip(), model.strip())


@dataclass(frozen=True)
class WorkerRole:
    """A provider-independent worker role definition.

    ``allow_dirty_baseline`` is role-level eligibility only: whether this
    role's contract permits a dispatch to carry a dirty-baseline adoption
    grant at all. The grant itself is computed per dispatch by the controller
    (see ``_controller_dirty_baseline_grant``) from the repository's actual
    dirty paths and this run's own evidence catalog, naming whichever
    workspace-change receipt truthfully covers them; the coordinator has no
    channel to name or influence which receipt is offered, and an eligible
    role with no covering receipt still faces the executor's normal
    clean-baseline refusal.
    """

    profile_id: str
    role: str
    capabilities: frozenset[str]
    details_schemas: frozenset[str]
    instructions: str
    artifact_kind: str
    sandbox: str = "read-only"
    writable_paths: tuple[str, ...] = ()
    require_repository_change: bool = False
    forbid_repository_change: bool = False
    allow_dirty_baseline: bool = False
    preflight_argv: tuple[str, ...] = ()
    require_preflight_success: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.role.strip():
            raise ValueError("worker role identity and role must be non-empty")
        if not self.instructions.strip():
            raise ValueError("worker role instructions must be non-empty")
        if not self.artifact_kind.strip():
            raise ValueError("worker role artifact_kind must be non-empty")
        # Mirror the executor sandbox contract so a bad role fails at
        # profile-build time, not mid-run inside a lazy executor factory.
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox must be read-only or workspace-write")
        if self.sandbox == "workspace-write" and not self.writable_paths:
            raise ValueError("workspace-write requires explicit writable_paths")
        if self.sandbox == "read-only" and self.writable_paths:
            raise ValueError("writable_paths require the workspace-write sandbox")
        if self.require_repository_change and self.sandbox != "workspace-write":
            raise ValueError(
                "require_repository_change requires the workspace-write sandbox"
            )
        if self.require_repository_change and self.forbid_repository_change:
            raise ValueError("repository changes cannot be both required and forbidden")
        if self.allow_dirty_baseline and self.sandbox != "workspace-write":
            raise ValueError("allow_dirty_baseline requires the workspace-write sandbox")
        if self.require_preflight_success and not self.preflight_argv:
            raise ValueError("require_preflight_success requires a preflight command")


def task_with_artifact_kind(
    task: Mapping[str, Any], kind: str
) -> dict[str, Any]:
    """Return the task with ``artifact_kind`` folded into its JSON context."""

    raw = task.get("context", "")
    try:
        context = json.loads(str(raw)) if raw else {}
    except json.JSONDecodeError:
        context = {"supplied_context": str(raw)}
    if not isinstance(context, dict):
        context = {"supplied_context": context}
    context["artifact_kind"] = kind
    return {**task, "context": json.dumps(context, sort_keys=True)}


def resolve_backend_spec(
    mixture: Mapping[str, str | BackendSpec],
    role: WorkerRole,
) -> BackendSpec:
    """Resolve one role's backend by role name, profile id, then ``*``."""

    for key in (role.role, role.profile_id, _DEFAULT_MIXTURE_KEY):
        if key in mixture:
            return parse_backend_spec(mixture[key])
    raise ValueError(
        f"agent mixture names no backend for role {role.role!r} "
        f"(profile {role.profile_id!r}) and has no '*' default"
    )


def build_role_profiles(
    *,
    mixture: Mapping[str, str | BackendSpec],
    roles: tuple[WorkerRole, ...],
    repository: Path,
    evidence: EvidenceCatalog,
    audit: AuditJournal | None = None,
    executables: Mapping[str, str] | None = None,
    pricing: Mapping[str, ModelPrice] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[RoleProfile, ...]:
    """Bind an agent mixture to worker roles as scheduler RoleProfiles.

    ``mixture`` maps a role name, profile id, or ``"*"`` to a backend spec
    (``"claude:claude-opus-5@high"``, ``"codex:gpt-5.6-terra@medium"``, or a
    :class:`BackendSpec`). ``executables`` optionally overrides the CLI binary
    per provider, and ``pricing`` optionally supplies a
    :class:`~harness_labs.usage.ModelPrice` per provider.
    """

    if not roles:
        raise ValueError("agent mixture requires at least one worker role")
    unknown_providers = set(executables or ()) - set(_PROVIDER_EXECUTABLES)
    if unknown_providers:
        raise ValueError(
            "executables override unknown providers: "
            + ", ".join(sorted(unknown_providers))
        )
    profiles = []
    for role in roles:
        spec = resolve_backend_spec(mixture, role)
        profiles.append(
            RoleProfile(
                profile_id=role.profile_id,
                role=role.role,
                capabilities=role.capabilities,
                details_schemas=role.details_schemas,
                backend_id=spec.backend_id,
                allow_dirty_baseline=role.allow_dirty_baseline,
                executor_factory=_executor_factory(
                    spec=spec,
                    role=role,
                    repository=repository,
                    evidence=evidence,
                    audit=audit,
                    executable=(executables or {}).get(
                        spec.provider, _PROVIDER_EXECUTABLES[spec.provider]
                    ),
                    pricing=(pricing or {}).get(spec.provider),
                    timeout_seconds=timeout_seconds,
                ),
            )
        )
    return tuple(profiles)


def _executor_factory(
    *,
    spec: BackendSpec,
    role: WorkerRole,
    repository: Path,
    evidence: EvidenceCatalog,
    audit: AuditJournal | None,
    executable: str,
    pricing: ModelPrice | None,
    timeout_seconds: float | None,
):
    shared = dict(
        repository=repository,
        evidence=evidence,
        role_instructions=role.instructions,
        model=spec.model,
        executable=executable,
        timeout_seconds=timeout_seconds,
        preflight_argv=role.preflight_argv,
        require_preflight_success=role.require_preflight_success,
        sandbox=role.sandbox,
        require_repository_change=role.require_repository_change,
        forbid_repository_change=role.forbid_repository_change,
        writable_paths=role.writable_paths,
        audit=audit,
        pricing=pricing,
    )
    if spec.provider == "claude":
        def factory(task: Mapping[str, Any]) -> ClaudeSemanticTaskExecutor:
            return ClaudeSemanticTaskExecutor(
                task=task_with_artifact_kind(task, role.artifact_kind),
                effort=spec.effort,
                dirty_baseline_grant=_controller_dirty_baseline_grant(
                    role, repository, evidence, audit=audit, attempt_id=task["id"]
                ),
                **shared,
            )
    else:
        def factory(task: Mapping[str, Any]) -> CodexSemanticTaskExecutor:
            return CodexSemanticTaskExecutor(
                task=task_with_artifact_kind(task, role.artifact_kind),
                reasoning=spec.effort,
                dirty_baseline_grant=_controller_dirty_baseline_grant(
                    role, repository, evidence, audit=audit, attempt_id=task["id"]
                ),
                **shared,
            )
    return factory


def _controller_dirty_baseline_grant(
    role: WorkerRole,
    repository: Path,
    evidence: EvidenceCatalog,
    *,
    audit: AuditJournal | None = None,
    attempt_id: str | None = None,
) -> Mapping[str, Any] | None:
    """Compute a controller-owned adoption grant for an eligible writable role.

    The role only declares eligibility (``allow_dirty_baseline``); the grant
    naming a prior attempt's workspace-change receipt is derived here from the
    repository's actual dirty paths and this run's own evidence catalog, never
    from coordinator- or worker-authored task content, so a role frozen ahead
    of time can never itself bypass the executor's clean-baseline preflight,
    and no dispatched task can choose or influence which receipt is offered.
    The candidate is checked with the same shared
    :func:`~harness_labs.controller_live.select_dirty_baseline_receipt` (which
    itself runs :func:`~harness_labs.controller_live.verify_dirty_baseline_grant`,
    the same check the executor runs at preflight: path coverage *and*
    content state), so a grant issued here is never journaled as granted
    against a workspace state that would fail preflight. When no candidate
    qualifies, the decline is journaled too (status ``"refused"``, naming the
    uncovered and content-mismatched paths) so drift is diagnosable from the
    journal.
    """

    if not role.allow_dirty_baseline:
        return None
    snapshot = workspace_snapshot(repository)
    dirty_paths = snapshot["changed_paths"]
    if not dirty_paths:
        return None
    receipt_ref, failure = select_dirty_baseline_receipt(
        evidence=evidence, dirty_paths=dirty_paths, dirty_files=snapshot["files"]
    )
    if receipt_ref is None:
        if audit is not None and failure is not None:
            audit.append(
                "dirty_baseline_adoption_grant_supplied",
                status="refused",
                payload={
                    "dirty_paths": sorted(dirty_paths),
                    "uncovered_paths": list(failure.uncovered_paths),
                    "mismatched_paths": list(failure.mismatched_paths),
                },
                attempt_id=attempt_id,
            )
        return None
    return {"receipt_ref": receipt_ref}


def build_coordinator_session(
    spec: str | BackendSpec,
    *,
    base_instructions: str | None = None,
    audit: AuditJournal | None = None,
    executable: str | None = None,
    pricing: ModelPrice | None = None,
    timeout_seconds: float | None = None,
) -> AgentSession:
    """Bind one backend spec to the coordinator seat as an AgentSession.

    The worker seats resolve through :func:`build_role_profiles`; this covers
    the remaining seat so a FeatureRun's ``session_factory`` can be declared
    with the same ``provider:model[@effort]`` vocabulary.
    """

    resolved = parse_backend_spec(spec)
    options: dict[str, Any] = {
        "model": resolved.model,
        "executable": executable
        or _PROVIDER_EXECUTABLES[resolved.provider],
        "base_instructions": base_instructions,
        "audit": audit,
        "pricing": pricing,
    }
    if timeout_seconds is not None:
        options["timeout_seconds"] = timeout_seconds
    if resolved.provider == "claude":
        return ClaudeAgentSession(effort=resolved.effort, **options)
    return CodexAppServerSession(reasoning=resolved.effort, **options)


__all__ = [
    "BackendSpec",
    "WorkerRole",
    "build_coordinator_session",
    "build_role_profiles",
    "parse_backend_spec",
    "resolve_backend_spec",
    "task_with_artifact_kind",
]
