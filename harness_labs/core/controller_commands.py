"""Provider-neutral commands submitted to the deterministic controller kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


COMMAND_PROTOCOL = "harness-command/1"
RECEIPT_PROTOCOL = "harness-command-receipt/1"


@dataclass(frozen=True)
class CommandActor:
    id: str
    role: str
    parent_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.role.strip():
            raise ValueError("command actor identity and role must be non-empty")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, "parent_id": self.parent_id}


@dataclass(frozen=True)
class CommandProvenance:
    trigger_event: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.trigger_event is not None and not self.trigger_event.strip():
            raise ValueError("trigger_event must be non-empty when supplied")
        if not all(isinstance(ref, str) and ref.strip() for ref in self.evidence_refs):
            raise ValueError("provenance evidence_refs must be non-empty strings")

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger_event": self.trigger_event,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str
    run_id: str
    type: str
    actor: CommandActor
    expected_revision: int
    idempotency_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    provenance: CommandProvenance = field(default_factory=CommandProvenance)
    protocol: str = COMMAND_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != COMMAND_PROTOCOL:
            raise ValueError("command protocol is invalid")
        for name in ("command_id", "run_id", "type", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"command {name} must be non-empty")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if not isinstance(self.payload, Mapping):
            raise ValueError("command payload must be an object")

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "type": self.type,
            "actor": self.actor.as_dict(),
            "expected_revision": self.expected_revision,
            "idempotency_key": self.idempotency_key,
            "provenance": self.provenance.as_dict(),
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    run_id: str
    status: Literal["accepted", "rejected", "duplicate"]
    revision: int
    event_ids: tuple[str, ...] = ()
    effect_refs: tuple[str, ...] = ()
    error_code: str | None = None
    message: str = ""
    protocol: str = RECEIPT_PROTOCOL

    @property
    def accepted(self) -> bool:
        return self.status in {"accepted", "duplicate"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "status": self.status,
            "revision": self.revision,
            "event_ids": list(self.event_ids),
            "effect_refs": list(self.effect_refs),
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class KernelEvent:
    event_id: str
    run_id: str
    revision: int
    event_type: str
    actor: CommandActor
    command_id: str
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "revision": self.revision,
            "event_type": self.event_type,
            "actor": self.actor.as_dict(),
            "command_id": self.command_id,
            "payload": dict(self.payload),
        }
