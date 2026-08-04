# Coordinator Dispatch Contract

Status: implemented

## Purpose

The coordinator dispatcher gives a run an arbitrary, versioned segmentation of
its lifecycle. It creates fresh coordinator sessions at declared context
boundaries while the deterministic controller kernel remains the sole authority
for run state.

The dispatcher is provider-neutral. A segment's `coordinator_profile` is an
opaque selection key interpreted by the supplied session factory, so the same
schema can select Codex, Claude, oMLX, or another coordinator implementation.

## Relationship

```text
operator / production entrypoint
  -> deterministic CoordinatorDispatcher
       -> fresh AgentSession for the current segment
       -> ControllerKernel commands and queries
            -> CapabilityScheduler
            -> TaskAttempt executors
            -> evidence, checkpoint, and audit journal
```

The dispatcher owns session lifecycle. The coordinator owns semantic judgment.
The kernel owns phase, task, finding, criterion, and terminal authority.

The dispatcher may register a schema, record coordinator session starts and
ends, and block a run after a deterministic dispatch failure. It cannot satisfy
criteria, adjudicate findings, create tasks, or advance a phase.

## Schema

The machine-readable contract is
[`../../schemas/coordinator-dispatch.schema.json`](../../schemas/coordinator-dispatch.schema.json).
Each schema contains ordered segments. Every run phase must appear exactly once
and in the same order as the run contract.

One segment defines:

- a stable identity;
- one or more contiguous phases;
- coordinator instructions;
- an opaque coordinator profile;
- artifact kinds to expose as handoff references;
- artifact kinds required before the coordinator may start;
- a maximum number of fresh coordinator attempts; and
- a per-attempt tool-call limit.

The dispatcher passes artifact descriptors, not artifact contents. A coordinator
opens only the evidence it needs through the existing bounded artifact query.

The implement-v13-shaped example is
[`../../schemas/examples/implement-v13-coordinators.json`](../../schemas/examples/implement-v13-coordinators.json).
It declares:

```text
orient + plan       -> plan-refute coordinator
implement           -> build coordinator
verify + review     -> verification/review coordinator
integrate + report  -> integration/report coordinator
```

The dispatcher contains no knowledge of those names. A different schema can
group phases such as `research + synthesize -> report` without code changes.

## Runtime

For each nonterminal controller snapshot, the dispatcher:

1. resolves the segment containing the authoritative current phase;
2. verifies required handoff artifact kinds;
3. computes the next attempt, enforcing a limit when the schema declares one;
4. constructs a segment context with schema identity, instructions, artifact
   descriptors, and prior coordinator-session outcomes;
5. asks the session factory for a fresh backend session;
6. records the session start through a typed dispatcher command;
7. runs `CoordinatorLoop` with the segment's exact phase scope;
8. stops and closes the session as soon as the kernel leaves that scope;
9. records boundary, terminal, blocked, or recoverable-failure outcome; and
10. starts the next segment, retries with a fresh coordinator, or blocks.

Phase identity is never inferred from coordinator final prose. A boundary exists
only when the controller snapshot records a phase outside the current segment or
a terminal run status.

## Authoritative controller state

The kernel records:

- registered schema identity and SHA-256;
- segment phase coverage and attempt limits;
- the active coordinator session;
- every completed coordinator session and outcome; and
- the exact starting and ending phase of each session.

These records are controller events and therefore enter the existing hash-linked
audit journal and atomic checkpoint.

## Python API

The primary contracts are:

- `CoordinatorDispatchSchema` and `CoordinatorSegment`;
- `CoordinatorLaunch`;
- `CoordinatorDispatcher`; and
- `run_dispatched_controller(...)`.

`run_dispatched_controller(...)` is the fresh-run entrypoint.
`resume_dispatched_controller(...)` restores the hash-verified checkpoint and
evidence catalog from the same run directory, verifies that the coordinator
schema identity and hash match, and continues that run.

On resume, the dispatcher deterministically:

- closes a checkpointed active coordinator session as `interrupted`;
- refuses to replay a task checkpointed as `running`, because its external
  effects are not known to be idempotent;
- dispatches checkpointed `ready` tasks before asking a coordinator to make new
  decisions; and
- allocates the next coordinator attempt from durable session history.

The terminal manifest records whether the entrypoint was a fresh run or a
resume. A resume test interrupts a real dispatcher between coordinator
sessions, restores the same run, proves that the original task is not repeated,
and verifies the resulting audit chain.
