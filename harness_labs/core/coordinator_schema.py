"""Versioned, provider-neutral coordinator segmentation contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from harness_labs.core.development_policy import DevelopmentPolicy

COORDINATOR_SCHEMA_PROTOCOL = "coordinator-dispatch-schema/1"


@dataclass(frozen=True)
class CoordinatorSegment:
    """One contiguous phase range owned by one fresh coordinator session."""

    id: str
    phases: tuple[str, ...]
    instructions: str
    coordinator_profile: str = "default"
    context_artifact_kinds: tuple[str, ...] = ()
    required_artifact_kinds: tuple[str, ...] = ()
    exit_artifact_kinds: tuple[str, ...] = ()
    development_policy: DevelopmentPolicy | None = None
    max_attempts: int | None = None
    max_tool_calls: int | None = None

    def __post_init__(self) -> None:
        for name in ("id", "instructions", "coordinator_profile"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"coordinator segment {name} must be non-empty")
        if not self.phases or not all(
            isinstance(phase, str) and phase.strip() for phase in self.phases
        ):
            raise ValueError("coordinator segment phases must be non-empty strings")
        if len(set(self.phases)) != len(self.phases):
            raise ValueError("coordinator segment phases must be unique")
        for name in (
            "context_artifact_kinds",
            "required_artifact_kinds",
            "exit_artifact_kinds",
        ):
            values = getattr(self, name)
            if not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"coordinator segment {name} must contain names")
            if len(set(values)) != len(values):
                raise ValueError(f"coordinator segment {name} must be unique")
        if not set(self.required_artifact_kinds).issubset(
            self.context_artifact_kinds
        ):
            raise ValueError(
                "required artifact kinds must also be included in segment context"
            )
        for name in ("max_attempts", "max_tool_calls"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ValueError(
                    f"coordinator segment {name} must be positive or unbounded"
                )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CoordinatorSegment:
        context = value.get("context", {})
        if not isinstance(context, Mapping):
            raise ValueError("coordinator segment context must be an object")
        raw_policy = value.get("development_policy")
        if raw_policy is not None and not isinstance(raw_policy, Mapping):
            raise ValueError(
                "coordinator segment development_policy must be an object or null"
            )
        return cls(
            id=_text(value, "id", "coordinator segment"),
            phases=tuple(_text_list(value, "phases", "coordinator segment")),
            instructions=_text(value, "instructions", "coordinator segment"),
            coordinator_profile=(
                _text(value, "coordinator_profile", "coordinator segment")
                if "coordinator_profile" in value
                else "default"
            ),
            context_artifact_kinds=tuple(
                _text_list(
                    context,
                    "artifact_kinds",
                    "coordinator segment context",
                    required=False,
                )
            ),
            required_artifact_kinds=tuple(
                _text_list(
                    context,
                    "required_artifact_kinds",
                    "coordinator segment context",
                    required=False,
                )
            ),
            exit_artifact_kinds=tuple(
                _text_list(
                    value,
                    "exit_artifact_kinds",
                    "coordinator segment",
                    required=False,
                )
            ),
            development_policy=(
                DevelopmentPolicy.from_mapping(raw_policy)
                if isinstance(raw_policy, Mapping)
                else None
            ),
            max_attempts=_optional_positive_int(value, "max_attempts"),
            max_tool_calls=_optional_positive_int(value, "max_tool_calls"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phases": list(self.phases),
            "instructions": self.instructions,
            "coordinator_profile": self.coordinator_profile,
            "context": {
                "artifact_kinds": list(self.context_artifact_kinds),
                "required_artifact_kinds": list(
                    self.required_artifact_kinds
                ),
            },
            "exit_artifact_kinds": list(self.exit_artifact_kinds),
            "development_policy": (
                self.development_policy.as_dict()
                if self.development_policy is not None
                else None
            ),
            "max_attempts": self.max_attempts,
            "max_tool_calls": self.max_tool_calls,
        }


@dataclass(frozen=True)
class CoordinatorDispatchSchema:
    """Ordered coordinator segments covering an arbitrary run phase graph."""

    schema_id: str
    segments: tuple[CoordinatorSegment, ...]
    protocol: str = COORDINATOR_SCHEMA_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != COORDINATOR_SCHEMA_PROTOCOL:
            raise ValueError("coordinator schema protocol is invalid")
        if not isinstance(self.schema_id, str) or not self.schema_id.strip():
            raise ValueError("coordinator schema_id must be non-empty")
        if not self.segments:
            raise ValueError("coordinator schema requires at least one segment")
        ids = [segment.id for segment in self.segments]
        if len(set(ids)) != len(ids):
            raise ValueError("coordinator segment ids must be unique")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CoordinatorDispatchSchema:
        if value.get("protocol") != COORDINATOR_SCHEMA_PROTOCOL:
            raise ValueError("coordinator schema protocol is invalid")
        raw_segments = value.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError("coordinator schema segments must be a non-empty list")
        if not all(isinstance(item, Mapping) for item in raw_segments):
            raise ValueError("coordinator schema segments must be objects")
        return cls(
            schema_id=_text(value, "schema_id", "coordinator schema"),
            segments=tuple(
                CoordinatorSegment.from_mapping(item) for item in raw_segments
            ),
        )

    def validate_phases(self, run_phases: tuple[str, ...]) -> None:
        """Require exact, ordered, one-time coverage of the run phase graph."""

        covered = tuple(
            phase for segment in self.segments for phase in segment.phases
        )
        if covered != run_phases:
            raise ValueError(
                "coordinator schema phases must exactly cover run phases in order"
            )

    def segment_for_phase(self, phase: str) -> CoordinatorSegment:
        for segment in self.segments:
            if phase in segment.phases:
                return segment
        raise ValueError(f"no coordinator segment covers phase: {phase}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "schema_id": self.schema_id,
            "segments": [segment.as_dict() for segment in self.segments],
        }

    def sha256(self) -> str:
        encoded = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _text(value: Mapping[str, Any], name: str, owner: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{owner} {name} must be non-empty")
    return item


def _text_list(
    value: Mapping[str, Any],
    name: str,
    owner: str,
    *,
    required: bool = True,
) -> list[str]:
    item = value.get(name)
    if item is None and not required:
        return []
    if (
        not isinstance(item, list)
        or (required and not item)
        or not all(isinstance(entry, str) and entry.strip() for entry in item)
    ):
        raise ValueError(f"{owner} {name} must be a string list")
    return list(item)


def _optional_positive_int(
    value: Mapping[str, Any],
    name: str,
) -> int | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(
            f"coordinator segment {name} must be positive or null"
        )
    return item


__all__ = [
    "COORDINATOR_SCHEMA_PROTOCOL",
    "CoordinatorDispatchSchema",
    "CoordinatorSegment",
]
