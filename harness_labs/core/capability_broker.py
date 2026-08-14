"""Policy-controlled browser, network, and external-effect execution brokers."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from time import monotonic_ns
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from harness_labs.core.attempts import TaskAttempt, TaskResult
from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.controller_results import semantic_payload


CAPABILITY_REQUEST_PROTOCOL = "capability-request/1"
CAPABILITY_RECEIPT_PROTOCOL = "capability-receipt/1"
CAPABILITY_KINDS = frozenset({"browser", "network", "external_effect"})
CapabilityHandler = Callable[["CapabilityRequest"], Mapping[str, Any]]


class CapabilityDenied(RuntimeError):
    """A request failed deterministic authorization before reaching a handler."""


@dataclass(frozen=True)
class CapabilityRequest:
    request_id: str
    kind: str
    operation: str
    target: str
    payload: Mapping[str, Any]
    idempotency_key: str
    authorization_ref: str | None = None
    protocol: str = CAPABILITY_REQUEST_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != CAPABILITY_REQUEST_PROTOCOL:
            raise ValueError("capability request protocol is invalid")
        if self.kind not in CAPABILITY_KINDS:
            raise ValueError("capability request kind is invalid")
        for name in ("request_id", "operation", "target", "idempotency_key"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"capability request {name} must be non-empty")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityRequest":
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("capability request payload must be an object")
        authorization = value.get("authorization_ref")
        if authorization is not None and not isinstance(authorization, str):
            raise ValueError(
                "capability request authorization_ref must be a string or null"
            )
        return cls(
            request_id=str(value.get("request_id", "")),
            kind=str(value.get("kind", "")),
            operation=str(value.get("operation", "")),
            target=str(value.get("target", "")),
            payload=dict(payload),
            idempotency_key=str(value.get("idempotency_key", "")),
            authorization_ref=authorization,
            protocol=str(value.get("protocol", "")),
        )


@dataclass(frozen=True)
class CapabilityPolicy:
    """Closed authorization surface for one run."""

    browser_operations: frozenset[str] = frozenset()
    browser_origins: frozenset[str] = frozenset()
    network_operations: frozenset[str] = frozenset()
    network_hosts: frozenset[str] = frozenset()
    external_effect_operations: frozenset[str] = frozenset()
    external_effect_targets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CapabilityReceipt:
    request_id: str
    kind: str
    operation: str
    target: str
    status: str
    duration_ms: int
    result: Mapping[str, Any]
    authorization_ref: str | None
    replayed: bool = False
    protocol: str = CAPABILITY_RECEIPT_PROTOCOL

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "kind": self.kind,
            "operation": self.operation,
            "target": self.target,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "authorization_ref": self.authorization_ref,
            "replayed": self.replayed,
            "result": copy.deepcopy(dict(self.result)),
        }


class CapabilityBroker:
    """Validate and route effects through controller-owned injected handlers."""

    def __init__(
        self,
        policy: CapabilityPolicy,
        handlers: Mapping[str, CapabilityHandler],
        *,
        audit: AuditJournal | None = None,
    ) -> None:
        unknown = set(handlers) - CAPABILITY_KINDS
        if unknown:
            raise ValueError(f"unknown capability handlers: {sorted(unknown)}")
        self.policy = policy
        self.handlers = dict(handlers)
        self.audit = audit
        self._receipts: dict[str, CapabilityReceipt] = {}

    def execute(self, request: CapabilityRequest) -> CapabilityReceipt:
        self._authorize(request)
        previous = self._receipts.get(request.idempotency_key)
        if previous is not None:
            if (
                previous.kind,
                previous.operation,
                previous.target,
            ) != (request.kind, request.operation, request.target):
                raise CapabilityDenied("idempotency key was reused for another effect")
            return CapabilityReceipt(
                **{
                    **previous.__dict__,
                    "replayed": True,
                }
            )
        handler = self.handlers.get(request.kind)
        if handler is None:
            raise CapabilityDenied(
                f"no controller handler is registered for {request.kind}"
            )
        started = monotonic_ns()
        try:
            result = handler(request)
            if not isinstance(result, Mapping):
                raise TypeError("capability handler result must be an object")
            status = "succeeded"
        except Exception as exc:
            result = {"error": str(exc), "error_type": type(exc).__name__}
            status = "failed"
        receipt = CapabilityReceipt(
            request_id=request.request_id,
            kind=request.kind,
            operation=request.operation,
            target=request.target,
            status=status,
            duration_ms=(monotonic_ns() - started) // 1_000_000,
            result=dict(result),
            authorization_ref=request.authorization_ref,
        )
        self._receipts[request.idempotency_key] = receipt
        self._audit(request, receipt)
        return receipt

    def _authorize(self, request: CapabilityRequest) -> None:
        if request.kind == "browser":
            _require_allowed(request.operation, self.policy.browser_operations)
            origin = _origin(request.target)
            _require_allowed(origin, self.policy.browser_origins)
            return
        if request.kind == "network":
            _require_allowed(request.operation.upper(), self.policy.network_operations)
            host = _host(request.target)
            _require_allowed(host, self.policy.network_hosts)
            return
        _require_allowed(
            request.operation, self.policy.external_effect_operations
        )
        _require_allowed(request.target, self.policy.external_effect_targets)
        if not request.authorization_ref:
            raise CapabilityDenied(
                "external effects require an explicit authorization reference"
            )

    def _audit(
        self, request: CapabilityRequest, receipt: CapabilityReceipt
    ) -> None:
        if self.audit is None:
            return
        artifact = self.audit.write_artifact(
            "capability-receipt", receipt.as_dict()
        )
        self.audit.append(
            "capability_broker_completed",
            status=receipt.status,
            payload={
                "kind": request.kind,
                "operation": request.operation,
                "target": request.target,
                "authorization_ref": request.authorization_ref,
                "tool_calls": 1,
            },
            actor=AuditActor("capability-broker", "capability_broker"),
            backend_id=f"{request.kind}-broker",
            duration_ms=receipt.duration_ms,
            artifacts=(artifact,),
        )


@dataclass(frozen=True)
class BrokeredCapabilityExecutor:
    """Expose a broker as an ordinary capability-scheduled task executor."""

    task: Mapping[str, Any]
    broker: CapabilityBroker
    evidence: EvidenceCatalog

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        try:
            context = self.task.get("context")
            if isinstance(context, str):
                context = json.loads(context)
            if not isinstance(context, Mapping):
                raise ValueError("capability task context must be an object")
            raw_request = context.get("capability_request")
            if not isinstance(raw_request, Mapping):
                raise ValueError(
                    "capability task requires context.capability_request"
                )
            request = CapabilityRequest.from_mapping(raw_request)
            if request.kind not in set(
                self.task.get("required_capabilities", ())
            ):
                raise CapabilityDenied(
                    "capability request kind is absent from the task grant"
                )
            receipt = self.broker.execute(request)
            artifact = self.evidence.add(
                kind="capability-receipt",
                content=receipt.as_dict(),
                media_type="application/json",
                producer_task_id=str(self.task["id"]),
            )
            if receipt.status != "succeeded":
                return TaskResult(
                    attempt.attempt_id,
                    "failed",
                    {"receipt": receipt.as_dict()},
                    (artifact.ref,),
                )
            payload = semantic_payload(
                summary=(
                    f"{receipt.kind} capability {receipt.operation} completed"
                ),
                details_schema=str(self.task["details_schema"]),
                details={"capability_receipt": receipt.as_dict()},
                claims=(
                    {
                        "id": f"capability-{request.request_id}",
                        "statement": (
                            f"Controller broker completed {request.operation} "
                            f"for {request.target}"
                        ),
                        "kind": "observed",
                        "evidence_refs": [artifact.ref],
                    },
                ),
                artifacts=(artifact.as_dict(),),
            )
            return TaskResult(
                attempt.attempt_id,
                "succeeded",
                payload,
                (artifact.ref, f"capability-broker:{request.kind}"),
            )
        except (CapabilityDenied, ValueError, TypeError) as exc:
            return TaskResult(
                attempt.attempt_id,
                "failed",
                {"error": str(exc), "error_type": type(exc).__name__},
            )


def _require_allowed(value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise CapabilityDenied(f"capability value is not allowed: {value}")


def _host(target: str) -> str:
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CapabilityDenied("network target must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise CapabilityDenied("network target cannot contain credentials")
    return parsed.hostname.lower()


def _origin(target: str) -> str:
    parsed = urlsplit(target)
    host = _host(target)
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


__all__ = [
    "CAPABILITY_KINDS",
    "CAPABILITY_RECEIPT_PROTOCOL",
    "CAPABILITY_REQUEST_PROTOCOL",
    "CapabilityBroker",
    "BrokeredCapabilityExecutor",
    "CapabilityDenied",
    "CapabilityPolicy",
    "CapabilityReceipt",
    "CapabilityRequest",
]
