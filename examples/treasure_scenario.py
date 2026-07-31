"""Shared construction for the cross-backend treasure scenario."""

from __future__ import annotations

from pathlib import Path

from harness_labs import (
    TOOL_UNAVAILABLE_REFUSAL,
    AgentSession,
    AttemptRunner,
    ChildAuthorization,
    ChildDispatcher,
    CodexFileReaderExecutor,
    InMemoryReferenceStore,
    SessionToolExecutor,
    TaskAttempt,
    TaskResult,
)


class UnavailableFileReaderExecutor:
    """Return a safe child result when its backend cannot read files."""

    def execute(self, attempt: TaskAttempt) -> TaskResult:
        return TaskResult(
            attempt_id=attempt.attempt_id,
            status="succeeded",
            payload={"text": TOOL_UNAVAILABLE_REFUSAL},
            evidence=("capability:read_file:unavailable",),
        )


def run_treasure_scenario(
    session: AgentSession,
    *,
    attempt_id: str,
) -> tuple[TaskResult, ChildDispatcher]:
    """Run the same parent attempt and authorization policy on any session."""

    treasure_path = Path(__file__).resolve().parent.parent / "treasure_chest.txt"
    parent = TaskAttempt(
        attempt_id=attempt_id,
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
    if "read_file" in session.capabilities.child_capabilities:
        reader = CodexFileReaderExecutor(store)
    else:
        reader = UnavailableFileReaderExecutor()
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
    result = AttemptRunner().run(
        parent,
        SessionToolExecutor(
            store=store,
            session=session,
            dispatcher=dispatcher,
        ),
    )
    return result, dispatcher
