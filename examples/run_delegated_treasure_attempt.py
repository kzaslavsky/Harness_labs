"""Run the same bounded treasure attempt through Codex and/or oMLX."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from harness_labs import (
    AuditActor,
    AuditJournal,
    CodexAppServerSession,
    OmlxAgentSession,
    OmlxBackend,
)

from .treasure_scenario import run_treasure_scenario


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--parent",
        "--backend",
        dest="parent",
        choices=("codex", "omlx", "all"),
        default="all",
    )
    parser.add_argument(
        "--child",
        choices=("matched", "codex", "omlx", "all"),
        default="matched",
    )
    args = parser.parse_args()
    parent_names = ("codex", "omlx") if args.parent == "all" else (args.parent,)

    exit_code = 0
    for parent_name in parent_names:
        child_names = (
            ("codex", "omlx")
            if args.child == "all"
            else ((parent_name,) if args.child == "matched" else (args.child,))
        )
        for child_name in child_names:
            run_id = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                + f"-{parent_name}-to-{child_name}-{uuid4().hex[:8]}"
            )
            run_dir = (
                Path(__file__).resolve().parent.parent
                / "logs"
                / "runs"
                / run_id
            )
            audit = AuditJournal(
                run_dir,
                run_id,
                actor=AuditActor(run_id, "controller"),
            )
            session = (
                CodexAppServerSession(audit=audit)
                if parent_name == "codex"
                else OmlxAgentSession(
                    backend=OmlxBackend(
                        max_tokens=128,
                        temperature=0.0,
                        audit=audit,
                    )
                )
            )
            result, dispatcher = run_treasure_scenario(
                session,
                attempt_id=f"treasure-{parent_name}-to-{child_name}",
                child_backend=child_name,
                audit=audit,
            )
            audit.finalize(
                result.status,
                result={
                    "attempt_id": result.attempt_id,
                    "status": result.status,
                    "payload": dict(result.payload),
                    "evidence": list(result.evidence),
                },
            )
            verification = AuditJournal.verify(run_dir)
            print(f"parent={parent_name} child={child_name}:")
            print(f"  status: {result.status}")
            if result.status == "succeeded":
                print(f"  output: {result.payload['text']}")
                child_turns = result.payload.get("child_turns", ())
                if len(child_turns) > 1:
                    print(
                        "  child follow-up: "
                        f"{child_turns[1]['payload']['text']}"
                    )
                if "usage" in result.payload:
                    print(f"  usage: {result.payload['usage']}")
            else:
                print(f"  error: {result.payload.get('error', 'unknown')}")
                exit_code = 1
            print(
                "  children: "
                f"{sum(event.event_type == 'child_dispatched' for event in dispatcher.events)}"
            )
            print(
                "  child responses: "
                f"{sum(event.event_type == 'child_responded' for event in dispatcher.events)}"
            )
            print(
                "  child terminated: "
                f"{any(event.event_type == 'child_terminated' for event in dispatcher.events)}"
            )
            print(f"  audit: {run_dir}")
            print(f"  audit head: {verification['head_hash']}")
            for evidence in result.evidence:
                print(f"  evidence: {evidence}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
