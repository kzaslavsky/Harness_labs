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

The harness MUST bound hierarchy depth, fan-out, retries, runtime, and resource
consumption. Workers with overlapping writable paths MUST be serialized or have
an explicit conflict-resolution owner.

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

## Acceptance

A harness implementation conforms only when automated tests demonstrate:

- schema-valid run, task, context, result, checkpoint, event, and decision data;
- enforced hierarchy, scope, budget, and retry bounds;
- crash-safe checkpoint and resume behavior;
- isolated worktree and branch execution;
- failed-gate prevention of integration;
- post-merge verification against the intended base;
- complete structured telemetry without secrets;
- repeatable metrics on a versioned evaluation suite.
