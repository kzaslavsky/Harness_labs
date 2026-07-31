"""Shared construction for the cross-backend treasure scenario."""

from __future__ import annotations

from pathlib import Path

from harness_labs import (
    TOOL_UNAVAILABLE_REFUSAL,
    AgentSession,
    AttemptRunner,
    AuditJournal,
    ChildAuthorization,
    ChildDispatcher,
    CodexFileReaderExecutor,
    InMemoryReferenceStore,
    ModelCapabilityExecutor,
    OmlxBackend,
    SessionToolExecutor,
    TaskAttempt,
    TaskResult,
)


def run_treasure_scenario(
    session: AgentSession,
    *,
    attempt_id: str,
    child_backend: str,
    audit: AuditJournal | None = None,
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
            "task:report-treasure": (
                "Output what is in treasure_chest.txt. Keep the child session alive, "
                "then ask that same child exactly: what enabled you to answer me "
                "this way? After receiving its explanation, preserve the original "
                "file-answer or refusal as your final answer."
            ),
            "context:parent": {},
            "grant:spawn-reader": {
                "capabilities": ["spawn_child", "send_child_message"],
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
    if child_backend == "codex":
        child_capabilities = frozenset({"read_file"})
        reader = CodexFileReaderExecutor(store, keep_alive=True, audit=audit)
    elif child_backend == "omlx":
        child_capabilities = frozenset()
        reader = ModelCapabilityExecutor(
            store=store,
            backend=OmlxBackend(max_tokens=32, temperature=0.0, audit=audit),
            backend_id="omlx",
            capabilities=child_capabilities,
            unavailable_response=TOOL_UNAVAILABLE_REFUSAL,
            keep_alive=True,
            audit=audit,
        )
    else:
        raise ValueError(f"unsupported child backend: {child_backend}")
    dispatcher = ChildDispatcher(
        parent,
        {
            "file_reader": ChildAuthorization(
                role="file_reader",
                task_ref="task:read-treasure",
                context_ref="context:treasure",
                grant_ref="grant:read-treasure",
                backend_id=child_backend,
                capabilities=child_capabilities,
                executor=reader,
            )
        },
        audit=audit,
    )
    result = AttemptRunner().run(
        parent,
        SessionToolExecutor(
            store=store,
            session=session,
            dispatcher=dispatcher,
            max_tool_calls=2,
            keep_child_alive=True,
            audit=audit,
        ),
    )
    return result, dispatcher
