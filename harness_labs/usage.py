"""Authoritative run usage normalization, pricing, and summary construction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping


USAGE_PROTOCOL = "harness-usage/1"


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    source: str

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.source.strip():
            raise ValueError("model price identity and source must be non-empty")
        if any(
            value < 0
            for value in (
                self.input_usd_per_million,
                self.cached_input_usd_per_million,
                self.output_usd_per_million,
            )
        ):
            raise ValueError("model prices cannot be negative")

    def calculate(
        self, input_tokens: int, cached_input_tokens: int, output_tokens: int
    ) -> Decimal:
        uncached = max(0, input_tokens - cached_input_tokens)
        total = (
            Decimal(uncached) * self.input_usd_per_million
            + Decimal(cached_input_tokens) * self.cached_input_usd_per_million
            + Decimal(output_tokens) * self.output_usd_per_million
        ) / Decimal(1_000_000)
        return total.quantize(Decimal("0.000001"))


def usage_payload(
    *,
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    pricing: ModelPrice | None = None,
) -> dict[str, Any]:
    values = (input_tokens, cached_input_tokens, output_tokens)
    if any(not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("token counts must be non-negative integers")
    if cached_input_tokens > input_tokens:
        raise ValueError("cached input cannot exceed total input")
    if pricing is not None and pricing.model != model:
        raise ValueError("pricing model does not match usage model")
    return {
        "protocol": USAGE_PROTOCOL,
        "model": model,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": (
            str(pricing.calculate(*values)) if pricing is not None else None
        ),
        "pricing_source": pricing.source if pricing is not None else None,
    }


def parse_codex_jsonl_usage(stdout: str) -> Mapping[str, int] | None:
    """Return the last authoritative turn.completed usage object."""

    found = None
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("type") != "turn.completed":
            continue
        raw = value.get("usage")
        if not isinstance(raw, Mapping):
            continue
        try:
            found = {
                "input_tokens": int(raw["input_tokens"]),
                "cached_input_tokens": int(raw.get("cached_input_tokens", 0)),
                "output_tokens": int(raw["output_tokens"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return found


def parse_claude_result_usage(payload: Mapping[str, Any]) -> Mapping[str, int] | None:
    """Normalize a `claude -p --output-format json` result usage object.

    Claude reports uncached, cache-read, and cache-creation input separately;
    the harness usage protocol counts total input with cache reads as the
    cached subset.
    """

    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    try:
        uncached = int(raw["input_tokens"])
        cache_read = int(raw.get("cache_read_input_tokens", 0))
        cache_creation = int(raw.get("cache_creation_input_tokens", 0))
        output = int(raw["output_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(uncached, cache_read, cache_creation, output) < 0:
        return None
    return {
        "input_tokens": uncached + cache_read + cache_creation,
        "cached_input_tokens": cache_read,
        "output_tokens": output,
    }


def build_run_summary(
    events_path: Path,
    *,
    status: str,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "duration_ms": 0,
        "tool_calls": 0,
    }
    costs = Decimal("0")
    priced = 0
    unpriced = 0
    records = 0
    missing_usage = 0
    backends: dict[str, dict[str, int]] = {}
    for event in _events(events_path):
        duration = event.get("duration_ms")
        if isinstance(duration, int) and duration >= 0:
            totals["duration_ms"] += duration
        if event.get("event_type") in {
            "backend_transport",
            "backend_usage",
            "capability_broker_completed",
        }:
            records += 1
        payload = event.get("payload", {})
        if not isinstance(payload, Mapping):
            continue
        tool_calls = payload.get("tool_calls")
        if isinstance(tool_calls, int) and tool_calls >= 0:
            totals["tool_calls"] += tool_calls
        usage = payload.get("usage")
        if not isinstance(usage, Mapping) or usage.get("protocol") != USAGE_PROTOCOL:
            if event.get("event_type") in {"backend_transport", "backend_usage"}:
                missing_usage += 1
            continue
        backend = str(event.get("backend_id") or "unknown")
        bucket = backends.setdefault(
            backend,
            {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        )
        for name in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and value >= 0:
                totals[name] += value
                bucket[name] += value
        value = usage.get("cost_usd")
        if value is None:
            unpriced += 1
        else:
            costs += Decimal(str(value))
            priced += 1
    return {
        "protocol": "harness-run-summary/1",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "usage": {
            "input_tokens": totals["input_tokens"],
            "cached_input_tokens": totals["cached_input_tokens"],
            "output_tokens": totals["output_tokens"],
            "wall_clock_ms": _elapsed_ms(started_at, finished_at),
            "summed_event_duration_ms": totals["duration_ms"],
            "tool_calls": totals["tool_calls"],
            "cost_usd": str(costs.quantize(Decimal("0.000001"))),
            "priced_usage_records": priced,
            "unpriced_usage_records": unpriced,
            "missing_usage_records": missing_usage,
            "cost_complete": unpriced == 0 and missing_usage == 0,
            "records": records,
            "by_backend": backends,
            "collection_method": "authoritative backend and broker events",
        },
    }


def _elapsed_ms(started_at: str, finished_at: str) -> int:
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    return max(0, int((finish - start).total_seconds() * 1000))


def _events(path: Path) -> Iterable[Mapping[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if isinstance(value, Mapping):
            yield value


__all__ = [
    "ModelPrice",
    "USAGE_PROTOCOL",
    "build_run_summary",
    "parse_codex_jsonl_usage",
    "usage_payload",
]
