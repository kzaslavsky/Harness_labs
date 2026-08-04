# Harness Architecture Contract

Status: normative

## Purpose

This contract defines the minimum architecture for a Harness Labs autonomous
feature-development harness. Implementations may vary in model, scheduler, and
storage, but may not omit these behavioral boundaries.

The keywords **MUST**, **SHOULD**, and **MAY** express requirement strength.

## Two-plane architecture

### Static control plane

The repository MUST version:

- role definitions and authority boundaries;
- task, context, result, event, decision, and checkpoint schemas;
- lifecycle transitions and stopping conditions;
- quality gates and acceptance-check mappings;
- Git worktree, branch, commit, and merge policy;
- retry, escalation, recovery, and budget limits;
- logging redaction and retention rules.

Static policy is the reproducible source of truth. A runtime prompt MAY explain
it but MUST NOT silently replace it.

### Dynamic execution plane

At runtime the harness MAY decompose work, select roles, allocate budgets,
assemble context, schedule independent tasks, retry recoverable failures, and
request review. Each action MUST remain within static policy, be attributable to
an actor and parent task, and emit sufficient state to resume or audit the run.

## Execution-first invariant

A harness MUST implement one executable production path through the complete
lifecycle before adding generalized recovery, replay, optimization, or synthetic
certification. Starting from the production entrypoint, one accountable run owner
MUST advance the run without another operator message until completion or a
genuine blocker.

Every nonterminal checkpoint MUST identify the component responsible for its next
transition. A coordinator identifier MUST resolve to a live process, task, or
resumable durable controller; an arbitrary label is not ownership proof. A
dispatcher MUST NOT enter passive waiting unless another owner has been verified
alive. Returning from the parent while the run is nonterminal and no successor is
alive is a conformance failure.

The durable checkpoint is the sole authority for phase identity. A foreground
shell or controller process proves liveness only and MUST NOT be reported as
evidence that its original phase is still active. A chained controller SHOULD
emit a flushed structured phase event after each observed checkpoint transition;
after one unexplained observation interval, its parent MUST take a zero-timeout
durable-state snapshot before naming the phase.

Synthetic, debug, and component flows MAY supplement production testing, but
MUST NOT substitute for the production lifecycle path or be reported as feature
completion.
The production lifecycle test MUST invoke the shipped dispatch and startup CLI
entrypoints as subprocesses. A test that calls planning and feature-driving
functions separately is nonconforming because it can supply a missing handoff.

### Failure-class repair guardrail

A request to diagnose or investigate a failed production harness run authorizes
the run owner to implement and test the smallest in-scope harness repair for the
demonstrated failure class in the same task. Stop after diagnosis only when the
operator explicitly requests diagnosis-only work or the repair needs new
authority outside the harness scope.

A corrective change MUST remove the demonstrated failure class, not only the
observed instance. If the current architecture cannot satisfy the stated
acceptance criterion, replacing that architecture is in scope; a smaller patch
MUST NOT be substituted merely because it is easier to unit test. Before claiming
the correction, identify every lifecycle decision that still depends on prompt
interpretation or an agent remembering to act. Required progress and terminal
settlement MUST be controller-owned and executable. Do not claim success until a
real dispatch reaches terminal queue state without operator intervention; mocks,
static contract tests, synthetic flows, and direct state fabrication are
insufficient.

## Hierarchy

Every run has exactly one run owner. The minimum logical roles are:

| Role | Accountability | May delegate? |
|---|---|---|
| Run owner | Outcome, task tree, budgets, stopping, final report | Yes |
| Planner | Acceptance criteria, decomposition, dependencies, risks | Bounded |
| Worker | One scoped implementation or investigation contract | Bounded only when authorized |
| Verifier | Executes acceptance checks and preserves evidence | No by default |
| Reviewer | Independently evaluates diff, contracts, and evidence | No by default |
| Integrator | Owns final branch validation, commit, and merge | No |

One process MAY hold multiple roles for low-risk work, but the run record MUST
state the role combination. Material changes SHOULD keep implementation and
review logically independent. A child result is advisory until its parent
validates it.

The harness MUST bound hierarchy depth, subagent count, retries, runtime, and
resource consumption. `max_subagents` limits the number of direct children
under one parent over the task tree; `max_parallelism` separately limits how
many tasks may execute concurrently. Workers with overlapping writable paths
MUST be serialized or have an explicit conflict-resolution owner.

## Required contracts

### Run contract

Defines `run_id`, repository identity, worktree, feature branch, base branch,
base commit, objective, acceptance criteria, budgets, permissions, lifecycle,
and schema versions.

### Task contract

Defines task identity and parent, role, objective, allowed scope, writable paths,
dependencies, context references, acceptance checks, budget, output schema,
and escalation conditions.

### Context contract

Defines the exact sources supplied to an agent, why each source is relevant,
its revision or content hash, precedence, exclusions, and token budget. See
[`context-engineering.md`](context-engineering.md).

### Coordinator dispatch contract

Defines ordered coordinator segments, their exact phase coverage, coordinator
profile, instructions, handoff artifact selection and requirements, attempt
limit, and tool-call budget. The dispatcher MUST create a fresh session at every
segment boundary and the controller MUST record schema identity and session
outcomes. See [`coordinator-dispatch.md`](coordinator-dispatch.md).

### Result contract

Defines status, claims, files changed, artifacts, commands executed, verification
results, decisions, unresolved risks, and recommended next transition. Claims
without evidence MUST be marked as inferences.

### Checkpoint contract

Defines the current phase, task states, verified artifacts, branch and commits,
budgets consumed, pending decisions, retry counters, and a monotonic sequence.
Writing a checkpoint MUST be atomic. Resumption MUST reject a checkpoint whose
repository identity or referenced commit no longer matches reality.

### Integration contract

Defines the candidate commit, target base branch and observed head, required
checks, review status, merge strategy, conflict policy, and post-merge proof.

## Complexity admission

A new schema, receipt type, recovery mechanism, abstraction, or telemetry stream
MUST identify:

1. the demonstrated production-lifecycle failure it prevents;
2. the production component that consumes it; and
3. the end-to-end assertion it supports.

If no such failure, consumer, and assertion exist, the mechanism MUST be deferred.
Supporting machinery MUST NOT mature ahead of the executable production path it
supports.

## Lifecycle

```text
orient -> plan -> implement -> verify -> review -> integrate -> report
   |        |         |          |         |          |
   +--------+---------+----------+---------+----------+--> blocked/failed
                                  blocked/failed --> recovering --> prior phase
```

Every transition MUST record the prior state, next state, actor, timestamp,
reason, and evidence. A phase completes only when its exit criteria are true.

## Git isolation and integration

Each feature run MUST operate in a dedicated worktree on a dedicated feature
branch. It MUST record the base branch and base commit before edits. A harness
MUST NOT rely on the user's primary checkout as disposable execution space.

Repository policy authorizes the integration owner to commit scoped work and
merge it into the recorded base branch after all gates pass. Before merging, the
integrator MUST read the current base head, reconcile advancement, inspect the
candidate range, and verify a clean worktree. After merging, it MUST read back
the resulting base commit and record the feature-to-base ancestry proof.

Force pushes, history rewrites, silent semantic conflict resolution, and bypassed
required checks are outside standing authorization.

## Failure and recovery

Failures MUST be classified as recoverable, contract violation, exhausted
budget, external blocker, or terminal quality failure. Retries require a changed
hypothesis, input, or method; repeating the same failed action does not count as
progress. Recovery resumes from durable verified state and emits a new attempt
identity linked to the prior failure.

Repository documentation gates MUST require an archived implementation plan to
link its recorded decision file. Historical broken links that predate the
harness MAY be repaired only after the normal link gate fails and a task-keyed
archive search identifies exactly one target; ambiguity remains a blocker.

## Acceptance

A harness implementation conforms only when automated tests demonstrate:

- one uninterrupted run from the real production dispatch entrypoint through
  `orient -> plan -> implement -> verify -> review -> integrate -> report`, using
  deterministic stub model responses but the real controller, worktree, prompts,
  checkpoints, receipts, and queue;
- no additional operator message is required after dispatch, every nonterminal
  checkpoint has a verified live owner, and the run reaches its production result
  and queue acknowledgment;
- schema-valid run, task, context, result, checkpoint, event, and decision data;
- enforced hierarchy, scope, budget, and retry bounds;
- crash-safe checkpoint and resume behavior;
- isolated worktree and branch execution;
- failed-gate prevention of integration;
- post-merge verification against the intended base;
- complete structured telemetry without secrets;
- repeatable metrics on a versioned evaluation suite.

Synthetic marker traversal, direct state-transition calls, and fabricated feature
results, transactions, merge receipts, or acknowledgments do not satisfy the
production lifecycle requirement. Recovery certification begins only after the
uninterrupted production path passes.
