# 0002 — Controller-owned parallel child batches

Status: accepted
Date: 2026-08-02
Owners: harness controller

## Context

Independent child attempts were dispatched synchronously, one tool call at a
time. External scripts could launch them concurrently, but that bypassed the
composition contract, obscured the task tree, and left the parent unaware of a
durable fork/join boundary. A prior 39-worktree survey also exposed a crash
window where valid journal events could be ahead of the last atomic checkpoint.

## Decision

The provider-neutral controller owns batch scheduling. A parent submits one
`ChildBatchRequest` containing ordered, independent requests and an explicit
parallelism cap. The dispatcher validates and reserves the complete batch before
launch, assigns deterministic child attempt IDs, runs distinct authorized
executors in a bounded thread pool, collects all terminal outcomes, and returns
one ordered `ChildBatchResult`.

The model sees one `spawn_children` tool. It does not create threads or choose
executors. Each role maps to one controller-owned authorization and executor;
roles in a parallel batch must be unique so a stateful executor instance is
never used concurrently. The initial failure policy is `collect_all`.

The resident parent remains open while the controller executes and joins the
batch. Valid hash-chained events ahead of a non-final checkpoint are
automatically reconciled on recovery and recorded by a new audit event.

## Alternatives

- Repeated `spawn_child` calls keep the interface small but serialize work and
  make overlap depend on backend-specific concurrent tool-call behavior.
- A separate survey-specific thread-pool script proves concurrency but creates a
  second orchestration system outside the attempt runner.
- Backend-native subagents reduce controller code but make bounds, identities,
  audit evidence, and recovery provider-specific.

## Evidence

- `tests/test_composition.py` proves actual overlap, the concurrency cap,
  stable ordering, all-or-nothing validation, and collect-all failure behavior.
- `tests/test_agent_sessions.py` proves the parent session stays resident across
  the fork/join and receives ordered results.
- `tests/test_audit.py` proves valid journal-ahead state is reconciled without
  accepting a broken hash chain.
- `examples/parallel_worktree_survey.py` exercises the production-shaped path
  with one fresh read-only Codex child per registered worktree.

## Consequences

Batch size, parallelism, subagent count, depth, role authority, and executor selection
remain controller policy. Completion order is observable in events, while result
order is deterministic. A batch can consume resources much faster than serial
dispatch, so callers must set explicit caps. Retained child conversations remain
an individual-dispatch feature until a batch follow-up contract is defined.

## Validation and reversal

Keep this decision while deterministic tests and live runs show bounded overlap,
complete terminal collection, correct audit reconstruction, and no duplicate
launch after recovery. Supersede it if a process-isolated scheduler or async
runtime can preserve the same contracts with materially better cancellation and
resource accounting.
