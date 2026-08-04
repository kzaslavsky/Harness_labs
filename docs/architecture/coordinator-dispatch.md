# Coordinator Dispatch Contract

Status: implemented prototype

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
3. computes the next bounded attempt;
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

`run_dispatched_controller(...)` is the production-shaped fresh-run entrypoint.
It creates the audit journal, evidence catalog, kernel, scheduler, and
dispatcher, then finalizes a manifest containing the coordinator launches and
authoritative final state.

## Current boundary

The prototype supports bounded in-run coordinator replacement. Durable restart
of the dispatcher process itself is not yet exposed as a public resume
entrypoint. The controller records enough schema and session history to support
that next step, but recovery must not be claimed until a killed dispatcher is
resumed through the real production entrypoint.
