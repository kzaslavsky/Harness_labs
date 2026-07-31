"""Run with `python3 -m examples.run_poem_attempt` from the repository root."""

from __future__ import annotations

from typing import Any, Mapping

from harness_labs import (
    AttemptRunner,
    InMemoryReferenceStore,
    TaskAttempt,
    TextExecutor,
)


class PoemBackend:
    """A deterministic program backend used to prove the executor boundary."""

    def generate(self, task: str, context: Mapping[str, Any]) -> str:
        subject = context.get("subject", "the operator")
        return (
            f"For {subject}, who keeps the systems bright,\n"
            "And turns uncertain signals into light,\n"
            "May every careful command find its way,\n"
            "And quiet, well-run engines mark the day."
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
