# `solo-phased-reviewfix` and FeatureRun

Status: analysis
Date: 2026-08-04

## Scope

This document compares the current Retinology Claude `implement-v13` engine in
use, `solo-phased-reviewfix`, with the Harness Labs FeatureRun prototype. It
focuses on planning, implementation, review, context lifecycle, authority, and
auditability.

The Claude sources examined were:

- `/Users/kirillzaslavsky/claudeprojects/Retinology/.claude/commands/implement-v13.md`
- `/Users/kirillzaslavsky/claudeprojects/Retinology/.claude/engine/phases/plan-refute.md`
- `/Users/kirillzaslavsky/claudeprojects/Retinology/.claude/engine/phases/phase-scoped.md`
- `/Users/kirillzaslavsky/claudeprojects/Retinology/.claude/engine/engines/solo-phased-reviewfix.md`
- the planner, source-binding reviewer, plan-reviser, and review-fixer prompts
  under `.claude/engine/prompts/`

The tracked Claude workflow was at commit
`cbf9479de0511c9afb65e31745c06cce59cd1e03`. The comparison does not treat
`solo` or `solo-codexbuild` as the active engine.

## Executive conclusion

Claude `solo-phased-reviewfix` has the stronger feature-development policy.
FeatureRun has the stronger provider-neutral execution and authority substrate.

The target architecture should combine them:

1. retain FeatureRun's deterministic kernel, typed commands, bounded task tree,
   capability scheduler, content-addressed evidence, and hash-linked audit;
2. port the Claude workflow's specialized planning and review policies as
   versioned policy modules;
3. add a thin deterministic dispatcher that replaces coordinator sessions at
   deliberate context boundaries; and
4. keep one controller kernel authoritative beneath both dispatcher and
   coordinators.

The dispatcher is necessary, but it must not become a second controller.

## Current phase structures

### Claude `solo-phased-reviewfix`

```text
thin dispatcher
  |
  +-- G1 Segment A: fresh plan/refute coordinator
  |     orient
  |     write plan
  |     run FRAME / NECESSITY / MECHANISM refutation lenses
  |     run up to two Codex plan-review rounds
  |     revise and classify findings
  |     write build briefing and durable handoff
  |
  +-- G1 Segment B: fresh builder
  |     read checkpoint
  |     read revised plan
  |     read build briefing
  |     read refutation reports
  |     implement and test
  |     classify review tier as mechanical or sensitive
  |
  +-- G2: fresh verification/review coordinator
  |     run Runtime Smoke A
  |     construct risk-shaped review panels
  |     fix, test, and re-review for up to 3 or 5 cycles
  |
  +-- G3: fresh commit coordinator
        run Smoke B when required
        run clean-environment certification
        commit
        run post-commit gate
        write run manifest
        optionally merge
```

`solo-phased-reviewfix` always enables phase scoping. Its G1 group has an
additional segment cut between plan refutation and implementation. The builder
never receives the planning agent's transcript. It receives a deliberate
handoff consisting of the checkpoint, revised plan, `build_briefing.md`, and
refutation reports.

### FeatureRun prototype

```text
one resident coordinator session
  |
  +-- plan: read-only planner and optional plan reviewers
  +-- implement: one workspace-write implementer
  +-- verify: fixed controller-owned command plus read-only interpretation
  +-- review: read-only reviewer
  |     optional remediator -> verifier -> reviewer loop
  +-- integrate: deterministic Git executor
  +-- report: read-only reporter
```

Workers are fresh processes, but the coordinator remains resident across the
entire lifecycle. Its conversation accumulates every query, command result, task
outcome, rejection, and recovery attempt.

## Planning

### What Claude does

The plan is a repository-resident engineering contract. Its planner must verify
concrete symbols, flags, paths, gates, and environment variables against the
working tree. The plan must include files, dependency-ordered steps,
parallelization, runtime contracts, testing, risks, and verified learnings.

Before implementation, three specialized lenses attempt to falsify it:

- `FRAME` attacks scope, ownership, architecture, contracts, and ADR alignment.
- `NECESSITY` attempts to prove that proposed new substrates are unnecessary.
- `MECHANISM` attacks transactions, crash replay, idempotency, destructive
  operations, integrity handling, PHI containment, and refusal behavior.

Up to two Codex rounds provide an independent model perspective. Findings are
triaged and the plan is revised in a batched edit. Unresolved critical findings
are classified:

- `CONTRACT` findings park or enter a bounded overnight adjudication ladder.
- `DESIGN` findings become mandatory implementation and review residuals.

The planner/refuter then writes a build briefing containing explored locations,
must-not-touch surfaces, rejected alternatives, tacit knowledge, lower-scored
risks, and gate hazards.

### What FeatureRun does

FeatureRun dispatches a read-only planner with coordinator-selected orientation
evidence. The worker returns a Markdown plan, typed claims and findings,
criterion coverage, recommendations, and unresolved questions.

Only the history scenario currently requires two plan-review artifacts and a
revised plan. Other scenarios require a plan artifact and satisfied criterion
but leave review topology to coordinator judgment.

The kernel validates the worker's authority, result schema, evidence references,
artifact count, and criterion coverage. It does not yet validate the plan's
source bindings, dependency completeness, runtime contracts, necessity, or
unhappy-path coverage.

### Assessment

Claude is substantially better at plan quality and falsification. FeatureRun is
better at making plan attempts attributable, typed, bounded, and auditable.

The right port is not a generic instruction saying "review the plan." It is a
versioned planning policy that constructs the source-binding and refutation
tasks, defines their output schemas, and gives the kernel deterministic exit
requirements.

## Implementation

### What Claude does

The active engine uses one fresh Claude builder after the plan/refute context
cut. It is seeded from durable artifacts rather than the prior transcript. It
can inspect additional repository context, implement, iterate with targeted
tests, record consequential decisions, and run the full suite at build exit.

Unlike `solo-codexbuild`, the active engine does not decompose implementation
into Codex dependency groups and does not use Codex predecessor reports.

### What FeatureRun does

FeatureRun dispatches one fresh workspace-write implementer. The executor:

- captures Git status and diff before execution;
- permits worktree edits but not commits or delegation;
- captures Git status and diff afterward;
- fails if no repository change occurred; and
- records an implementation summary and workspace-change receipt.

### Assessment

The two active designs are closer than a comparison with `solo-codexbuild`
suggests: each presently has one primary builder.

Claude is better at carrying a deliberate planning handoff and at allowing the
builder to gather whatever additional context implementation requires.
FeatureRun is better at capability assignment, executor identity, structured
results, backend replacement, and kernel-owned task state.

Neither currently enforces a narrow OS-level writable-path grant. Claude
declares and later checks intended file surfaces; FeatureRun grants the whole
worktree to its writable executor.

## Review

### What Claude does

At build exit, a deterministic classifier selects `mechanical` or `sensitive`.
Uncertainty resolves to `sensitive`.

| Review lever | Mechanical | Sensitive |
|---|---:|---:|
| Cycle cap | 3 | 5 |
| Per-cycle tests | affected paths | affected paths |
| Full browser walk | final cycle | final cycle |
| Mutation testing | skipped | final sensitive surface |
| Disclosure attack | not required | every cycle |
| Design residuals | every cycle | every cycle |

Review panel composition follows the diff's risk surface. Larger diffs require
multiple reviewers and an adversarial reviewer. PHI, security, schema,
migration, contract, ADR, and UI surfaces add specialized review obligations.

Each finding carries a score, file and line, proposed fix, fix-cost class, and
the normative clause it protects. The review loop adds deduplication, a re-raise
ledger, citation checks, scope-expansion screening, a separate fixer, targeted
tests, fix-regression review, bounded cycles, marginal-yield stopping, and a
technical-debt sink.

### What FeatureRun does

FeatureRun provides one generic independent reviewer, typed findings, explicit
finding dispositions, a writable remediator, controller-owned verification, and
the ability to re-review. The kernel will not leave review while a
disposition-required finding remains open.

It does not yet provide a risk classifier, reviewer-construction policy,
finding deduplication, fix-cost accounting, re-raise ledger, mutation policy,
targeted-test constructor, cycle budget, or marginal-yield rule.

### Assessment

Claude has a mature review strategy. FeatureRun has stronger authoritative
review state.

One Claude behavior needs reconsideration during the port: score-80+ findings
remaining at the cycle cap may be folded into technical debt unless protected
by stronger contract or residual rules. FeatureRun's safer primitive is that a
finding marked `requires_disposition` cannot disappear merely because a cycle
budget expired.

## Context lifecycle and cost

Claude phase scoping implements the correct economic model:

```text
cost of retained context = retained tokens x remaining model requests
```

The plan/refute transcript dies before implementation. Build context dies before
review. Review context dies before commit. Only curated artifacts cross those
boundaries.

FeatureRun keeps one coordinator resident. The recorded live runs showed no
coordinator compactions. Individual requests remained below the model context
window, but repeated cached reads accumulated millions of tokens. This is not a
compaction problem; it is a session-lifetime and context-selection problem.

## Authority and audit

Claude uses a mutable checkpoint, Markdown plan, append-only decision journal,
temporary receipt files, review reports, and committed run manifest. This is
valuable human-readable evidence, but many invariants depend on agents following
prose conventions and directly editing shared state.

FeatureRun centralizes authority in a deterministic kernel:

- typed commands and receipts;
- expected revisions and idempotency keys;
- bounded task depth, subagent count, total task count, and concurrency;
- capability-matched scheduling;
- typed semantic results;
- content-addressed artifacts;
- append-only hash-linked events; and
- deterministic phase and integration gates.

The controller substrate is the stronger basis. Claude's phase policies should
be ported onto it rather than its mutable authority model being copied.

## Required dispatcher architecture

Yes, FeatureRun needs a dispatcher above coordinator sessions, with one
controller kernel authoritative beneath both. The relationship is not a simple
three-layer authority stack, however.

```text
operator / production CLI
          |
          v
deterministic run dispatcher
  - owns coordinator-session lifecycle
  - acquires segment lease
  - starts, stops, and replaces coordinators
  - reacts to durable controller state
  - never decides semantic acceptance
          |
          +-------------------------------+
          |                               |
          v                               v
phase-scoped coordinator             controller kernel
  - makes semantic judgments          - owns authoritative run state
  - requests tasks and transitions    - validates every command
  - opens selected evidence           - enforces phases, gates, bounds
  - cannot spawn its successor         - records checkpoints and audit
          |                               |
          +----------- typed API ----------+
                                          |
                                          v
                              scheduler -> executors -> evidence
```

The dispatcher and coordinator are different clients of the controller:

- The dispatcher receives only lifecycle and administrative authority.
- The coordinator receives semantic task, decision, finding, and transition
  authority within its current segment.
- The controller validates both and remains the sole authoritative writer.

The dispatcher is logically above coordinator sessions because it creates and
replaces them. It is not above the controller's authority and must not maintain
a competing phase state.

### Why a dispatcher is necessary

A coordinator cannot reliably end itself and also create its fresh successor.
Putting replacement logic in the outgoing coordinator:

- retains the very context the cut is supposed to discard;
- makes crash recovery depend on the dying process;
- mixes semantic judgment with process supervision; and
- leaves no durable owner between sessions.

The kernel should also not directly contain backend process-management logic.
Keeping dispatch separate preserves a deterministic state machine while letting
different runtimes construct Codex, Claude, oMLX, or other coordinator sessions.

### Minimum dispatcher responsibilities

The first dispatcher should remain deliberately small:

1. Read the authoritative controller snapshot.
2. Return if the run is terminal.
3. Map the current phase to a versioned coordinator segment.
4. Acquire a controller-recorded segment lease.
5. Ask a deterministic context-handoff assembler for the segment's starting
   packet of artifact references.
6. Start one coordinator session through a coordinator factory.
7. Record the session identity and liveness with the controller.
8. Wait for coordinator termination or a controller-recorded segment boundary.
9. Re-read controller state rather than trusting final model prose.
10. Release the lease and either start the next segment, perform one bounded
    recovery start, or report the controller's terminal state.

It must never:

- mark acceptance criteria satisfied;
- adjudicate review findings;
- infer phase completion from coordinator text;
- edit checkpoints or audit logs directly;
- inspect the whole repository to make semantic decisions;
- launch two writable coordinators for the same segment; or
- wait passively without verified live ownership.

### Controller additions needed

The kernel needs only the state required by the production dispatcher:

- `segment_id` and the phase range it owns;
- coordinator session identity and backend;
- lease owner, acquisition, heartbeat, and expiry evidence;
- segment attempt number and predecessor;
- handoff artifact reference;
- session-started, session-ended, and session-lost events; and
- a deterministic indication of whether the current state requires another
  coordinator, recovery, operator input, or terminal reporting.

These should extend the existing checkpoint and event model. They should not
become a second scheduler or a generalized distributed-systems framework.

### Coordinator segmentation

An initial FeatureRun policy can mirror the active Claude engine:

| Segment | Phases | Coordinator context |
|---|---|---|
| Plan/refute | `orient`, `plan` | objective, repository identity, orientation policy |
| Build | `implement` | accepted plan, build handoff, plan findings, constraints |
| Verify/review | `verify`, `review` | accepted plan, actual diff, verification policy |
| Integrate/report | `integrate`, `report` | gate matrix, review state, candidate state |

The exact cut points are policy. The dispatcher mechanism should work for other
segment tables without containing scenario-specific branches.

## Implemented next step

The minimal deterministic dispatcher now exists as `CoordinatorDispatcher`.
It consumes the versioned coordinator-dispatch schema described in
`docs/architecture/coordinator-dispatch.md`.

Its first end-to-end acceptance test should run the production entrypoint with
deterministic coordinator sessions and prove:

1. the plan coordinator reaches a validated handoff;
2. its session is closed;
3. a fresh build coordinator starts from only the declared handoff;
4. a fresh verify/review coordinator starts after implementation;
5. a fresh integration/report coordinator finishes the run;
6. every nonterminal checkpoint names a verified live owner;
7. no phase is inferred from coordinator final text;
8. a killed coordinator is replaced once from the last verified checkpoint; and
9. the event chain reconstructs every segment transition and session identity.

Once that production path passes, port plan refutation and risk-tiered review as
policy modules over the same dispatcher and controller contracts.
