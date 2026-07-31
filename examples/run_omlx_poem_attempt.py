"""Run the poem task through local Qwen3.5-4B served by oMLX."""

from __future__ import annotations

from harness_labs import (
    AttemptRunner,
    InMemoryReferenceStore,
    OmlxBackend,
    TaskAttempt,
    TextExecutor,
)


def main() -> int:
    attempt = TaskAttempt(
        attempt_id="poem-omlx-qwen35-4b",
        task_ref="task:poem",
        context_ref="context:operator",
        grant_ref="grant:text",
    )
    store = InMemoryReferenceStore(
        {
            "task:poem": "write a poem about the operator",
            "context:operator": {"subject": "the operator"},
            "grant:text": {"capabilities": ["generate_text"]},
        }
    )
    result = AttemptRunner().run(
        attempt,
        TextExecutor(store=store, backend=OmlxBackend()),
    )

    print(f"status: {result.status}")
    if result.status == "succeeded":
        print(result.payload["text"])
        print(f"evidence: {result.evidence[0]}")
        return 0
    print(f"error: {result.payload.get('error', 'unknown')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
