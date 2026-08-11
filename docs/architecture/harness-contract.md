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

The current engine defaults `max_depth` and `max_subagents` to `5`.
`max_subagents` limits the number of direct children under one parent over the
task tree; it is not a concurrency limit. `max_parallelism`, total task count,
coordinator attempts, and coordinator tool calls default to unbounded (`null`)
until empirical runs establish useful limits. Any of those limits may still be
set explicitly by a run or coordinator schema. A production profile MUST
eventually bound retries, runtime, and resource consumption before unattended
use. Workers with overlapping writable paths MUST be serialized or have an
explicit conflict-resolution owner.

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

A PlanGraph-associated FeatureRun is the normal FeatureRun with its approved
PlanGraph packet bound as the planning handoff. It omits only `orient` and
`plan`; it MUST use the same implementation, verification, ledger-backed
review/fix, Git transaction, recovery, integration, and reporting machinery.
A PlanGraph-bound coordinator dispatches the normal `implement` segment; the
existing FeatureRun controller continues to own every subsequent gate.
A custom child lifecycle or replacement review loop is not a conforming
PlanGraph-associated FeatureRun.

## Git isolation and integration

Each feature run MUST operate in a dedicated worktree on a dedicated feature
branch. It MUST record the base branch and base commit before edits. A harness
MUST NOT rely on the user's primary checkout as disposable execution space.

`run_feature_worktree(...)` owns this transaction. It rejects a dirty or
misidentified base, creates the feature branch and worktree at the recorded base
commit, requires the run contract to bind those exact identities, stages only
declared allowed paths, and records content-addressed receipts for creation,
candidate commit, and integration. Merge is optional and defaults off.

A workspace-write executor requires an explicit non-empty writable-path grant.
It normally starts from a clean candidate worktree. A review fixer MAY opt into
a dirty baseline because it follows the builder in the same candidate tree. In
that mode the controller compares before/after file states and applies the grant
to the fixer's delta, not to unchanged builder edits. It always rejects HEAD or
branch changes. Its `workspace-change-receipt/2` records the baseline state,
final state, and worker-only changed paths. This is controller verification
after execution, not an OS-level filesystem sandbox.

### Deterministic verification and recovery

Before review or commit, FeatureRun MAY execute one declared verification
command directly in the candidate worktree. When configured, this command is
the authority for pass or fail: a model report cannot override its exit code.
The controller records the command, candidate snapshot, exit code, stdout, and
stderr as durable evidence.

If the review/fix gate changes the candidate, FeatureRun MUST execute that same
declared command again after review and before commit. The post-review execution
uses the same bounded recovery path and its receipt identifies the
`post_review_repair` stage. A review that leaves the candidate byte-identical
does not cause a duplicate execution.

A failed command MUST enter the bounded same-worktree repair path rather than
immediately ending the run. The repair attempt receives the exact failed
command receipt and may change only the declared paths. The controller then
reruns the same command. Only a failed repair or a still-failing command after
the repair limit blocks the run; the uncommitted candidate worktree is retained
for inspection or recovery.

### Review/fix gate

FeatureRun MAY enable its controller-owned review/fix gate after the coordinator
has completed and before the integration owner commits. The gate consumes normal
Task Attempts for `review`, `fix`, and `verify`; model/backend selection remains
an executor concern.

When enabled, the gate MUST persist a `review-ledger/1` artifact after each
material transition. Finding identity is `(file, subject)`. The ledger records
every occurrence, cycle seen, score, normative protection, fix cost, disposition,
fix attempt, verification state, and reopening. A required or contract-violating
finding MUST NOT disappear at a cycle or yield limit. It blocks unless an
operator explicitly enabled required-finding conversion to technical debt.

In a PlanGraph run, an open `scope_expanding` finding MAY transfer instead of
blocking only when it declares the exact paths required for repair and the
frozen graph resolves every required path to the same unique
downstream owner. The source ledger preserves the finding as `transferred`; the
graph checkpoint carries the complete record to that owner's bound request, and
the destination review ledger reopens the same stable key as a required
obligation. Missing, ambiguous, current, or already-completed owners fail closed.
Graph completion requires the destination FeatureRun to close the obligation.

Targeted verification MAY verify only a subset of the findings addressed in one
repair attempt. Verified keys advance to regression review; unverified keys stay
open and consume the remaining bounded cycle budget. A successor MAY inherit
those open stable keys with discovery frozen, so recovery repairs the existing
ledger without reopening review or repeating already verified work.

The policy independently switches ledgering, duplicate collapse, re-raise
suppression, normative-citation checks, scope-expansion screening, targeted
verification, regression re-review, risk-tiered cycle limits, no-progress and
marginal-yield exits, and the technical-debt sink. Disabling one mechanism MUST
not implicitly disable another. The exact resolved policy is part of every
ledger artifact.

The default enabled policy uses deterministic risk routing: uncertain or
security/schema/storage/web/UI changes are `sensitive` (five review cycles);
other changes are `mechanical` (three). The final allowed cycle is review-only:
the controller does not apply a fix it cannot independently re-review.

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

The dispatcher treats a durable `running` task as uncertain external state and
blocks instead of replaying it. A checkpointed active coordinator session can
be closed as interrupted because session lifecycle itself is controller-owned;
checkpointed `ready` tasks may then be dispatched without repeating completed
tasks.

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
