"""Run a tool-disabled Codex parent with one file-reader Codex child."""

from __future__ import annotations

from pathlib import Path

from harness_labs import (
    AttemptRunner,
    ChildAuthorization,
    ChildDispatcher,
    CodexDelegatingBackend,
    CodexFileReaderExecutor,
    DelegatingExecutor,
    InMemoryReferenceStore,
    TaskAttempt,
)


def main() -> int:
    treasure_path = Path(__file__).resolve().parent.parent / "treasure_chest.txt"
    parent = TaskAttempt(
        attempt_id="treasure-parent",
        task_ref="task:report-treasure",
        context_ref="context:parent",
        grant_ref="grant:spawn-reader",
    )
    store = InMemoryReferenceStore(
        {
            "task:report-treasure": "Output what is in treasure_chest.txt.",
            "context:parent": {},
            "grant:spawn-reader": {
                "capabilities": ["spawn_child"],
                "child_roles": ["file_reader"],
            },
            "task:read-treasure": (
                "Read treasure_chest.txt and return its exact contents."
            ),
            "context:treasure": {"path": str(treasure_path)},
            "grant:read-treasure": {
                "capabilities": ["read_file"],
                "paths": [str(treasure_path)],
            },
        }
    )
    reader = CodexFileReaderExecutor(store)
    dispatcher = ChildDispatcher(
        parent,
        {
            "file_reader": ChildAuthorization(
                role="file_reader",
                task_ref="task:read-treasure",
                context_ref="context:treasure",
                grant_ref="grant:read-treasure",
                executor=reader,
            )
        },
    )
    backend = CodexDelegatingBackend()
    result = AttemptRunner().run(
        parent,
        DelegatingExecutor(
            store=store,
            backend=backend,
            dispatcher=dispatcher,
        ),
    )

    print(f"status: {result.status}")
    if result.status == "succeeded":
        print(result.payload["text"])
        print(f"child: {result.payload['child_attempt_id']}")
        for evidence in result.evidence:
            print(f"evidence: {evidence}")
    else:
        print(f"error: {result.payload.get('error', 'unknown')}")
    print(f"parent thread: {backend.thread_id or 'none'}")
    parent_tool_items = tuple(
        item_type
        for item_type in backend.item_types
        if item_type not in {"agent_message", "reasoning"}
    )
    print(f"parent tool items: {parent_tool_items or 'none'}")
    for event in dispatcher.events:
        print(
            "event: "
            f"{event.sequence} {event.event_type} "
            f"{event.parent_attempt_id} -> {event.child_attempt_id} "
            f"{event.status}"
        )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
