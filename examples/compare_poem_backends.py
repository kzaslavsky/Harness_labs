"""Compare one poem task across deterministic and Codex backends."""

from __future__ import annotations

from harness_labs import (
    AttemptRunner,
    CodexExecBackend,
    InMemoryReferenceStore,
    PoemBackend,
    TaskAttempt,
    TextBackend,
    TextExecutor,
)


TASK_REF = "task:poem"
CONTEXT_REF = "context:operator"
GRANT_REF = "grant:text"


def execute(
    store: InMemoryReferenceStore,
    backend: TextBackend,
    attempt_id: str,
):
    attempt = TaskAttempt(
        attempt_id=attempt_id,
        task_ref=TASK_REF,
        context_ref=CONTEXT_REF,
        grant_ref=GRANT_REF,
    )
    return AttemptRunner().run(
        attempt,
        TextExecutor(store=store, backend=backend),
    )


def main() -> int:
    store = InMemoryReferenceStore(
        {
            TASK_REF: "write a poem about the operator",
            CONTEXT_REF: {"subject": "the operator"},
            GRANT_REF: {"capabilities": ["generate_text"]},
        }
    )
    comparisons = (
        ("PoemBackend", execute(store, PoemBackend(), "poem-deterministic")),
        ("CodexExecBackend", execute(store, CodexExecBackend(), "poem-codex")),
    )

    for name, result in comparisons:
        print(f"=== {name} ===")
        print(f"status: {result.status}")
        if result.status == "succeeded":
            print(result.payload["text"])
            print(f"evidence: {result.evidence[0]}")
        else:
            print(f"error: {result.payload.get('error', 'unknown')}")
        print()

    return 0 if all(result.status == "succeeded" for _, result in comparisons) else 1


if __name__ == "__main__":
    raise SystemExit(main())
