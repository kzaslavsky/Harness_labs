"""Run the same bounded treasure attempt through Codex and/or oMLX."""

from __future__ import annotations

import argparse

from harness_labs import CodexAppServerSession, OmlxAgentSession

from .treasure_scenario import run_treasure_scenario


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=("codex", "omlx", "all"),
        default="all",
    )
    args = parser.parse_args()
    sessions = []
    if args.backend in {"codex", "all"}:
        sessions.append(("codex", CodexAppServerSession()))
    if args.backend in {"omlx", "all"}:
        sessions.append(("omlx", OmlxAgentSession()))

    exit_code = 0
    for name, session in sessions:
        result, dispatcher = run_treasure_scenario(
            session,
            attempt_id=f"treasure-parent-{name}",
        )
        print(f"{name}:")
        print(f"  status: {result.status}")
        if result.status == "succeeded":
            print(f"  output: {result.payload['text']}")
            if "usage" in result.payload:
                print(f"  usage: {result.payload['usage']}")
        else:
            print(f"  error: {result.payload.get('error', 'unknown')}")
            exit_code = 1
        print(f"  children: {len(dispatcher.events) // 2}")
        for evidence in result.evidence:
            print(f"  evidence: {evidence}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
