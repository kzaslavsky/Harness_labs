"""Run with `python3 -m examples.run_poem_attempt` from the repository root."""

from __future__ import annotations

from harness_labs import (
    AttemptRunner,
    InMemoryReferenceStore,
    PoemBackend,
    TaskAttempt,
    TextExecutor,
)


def main() -> int:
    store = InMemoryReferenceStore(
        {
            "task:poem": "write a poem about the operator",
            "context:operator": {"subject": "the operator"},
            "grant:text": {"capabilities": ["generate_text"]},
        }
    )
    attempt = TaskAttempt(
        attempt_id="poem-1",
        task_ref="task:poem",
        context_ref="context:operator",
        grant_ref="grant:text",
    )
    result = AttemptRunner().run(
        attempt,
        TextExecutor(store=store, backend=PoemBackend()),
    )
    if result.status != "succeeded":
        raise SystemExit(result.payload.get("error", result.status))
    print(result.payload["text"])
    print(result.evidence[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
