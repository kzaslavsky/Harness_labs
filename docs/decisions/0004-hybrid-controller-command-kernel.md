# 0004 — Hybrid controller with a deterministic command kernel

Status: accepted
Concerns-paths: harness_labs/core/controller_kernel.py, harness_labs/core/controller_coordinator.py
Date: 2026-08-03
Owners: harness controller

## Context

A non-reasoning controller can enforce an inappropriate universal requirement
when a task-specific evidentiary judgment is encoded as a mechanical invariant.
Conversely, allowing a model to mutate checkpoints, budgets, task identity, or
terminal state directly makes replay, authority, and crash recovery unreliable.

The first composition prototype also mapped each role to one executor instance.
That prevented a coordinator from choosing an arbitrary bounded number of
parallel inspectors sharing the same role profile.

## Decision

The controller is a hybrid subsystem:

- one deterministic kernel owns commands, authoritative state, receipts,
  revisions, idempotency, task bounds, finding dispositions, completion gates,
  events, and checkpoints;
- one resident coordinator model receives a deterministic `RunView`, exercises
  semantic judgment, and uses a fixed provider-neutral command language;
- read-only query tools open structured results, findings, decisions, events, and
  content-addressed artifacts only when needed;
- task results share one semantic envelope and select a task-declared detail
  schema;
- role profiles describe capabilities, while a capability scheduler creates a
  fresh executor/session for each concurrent attempt; and
- authorized workers may request bounded subchildren through the same command
  envelope and global hierarchy limits.

The coordinator may invent objectives, context, roles, decompositions, findings,
decisions, and recovery proposals. It may not invent new kernel verbs or grant
itself authority. A completion request succeeds only when generic contract gates
are satisfied.

## Alternatives

- A deterministic script with an LLM fallback leaves ordinary semantic decisions
  in the wrong layer and invokes judgment only after a mechanical rule fails.
- An LLM-only controller has judgment but cannot provide deterministic authority,
  atomic identity, idempotency, or trustworthy replay by itself.
- Scenario-specific phase handlers would make the initial planning workflow easy
  to implement but would not support multimodal diagnosis or open-ended product
  appraisal without controller changes.
- One executor singleton per role preserves the original dispatcher but prevents
  homogeneous parallel review and inspection.

## Evidence

- `tests/test_controller_kernel.py` covers typed commands, stale revision
  rejection, idempotency, evidence promotion, completion gates, hierarchy, and
  machine-readable contracts.
- `tests/test_controller_scheduler.py` covers capability rejection, repeated-role
  parallelism with fresh executors, and bounded subchild delegation.
- `tests/test_controller_run.py` covers the real fixture entrypoint, terminal
  audit manifest, coordinator replacement, completed-work reuse, and
  journal-ahead dispatch recovery.
- `tests/test_controller_scenarios.py` runs three non-isomorphic task graphs
  through the same kernel and command language: history-to-plan, dynamic
  dark-mode UI diagnosis, and hierarchical product appraisal.
- The repository's complete unittest suite passes with the new controller.

## Consequences

The kernel does not decide whether a plan, diagnosis, or product proposal is
semantically good. It validates typed evidence and leaves those judgments to the
coordinator or independent workers. The coordinator does not receive complete
logs by default, but every authorized artifact remains queryable.

The current slice is an analysis-and-planning controller, not yet the complete
feature lifecycle. It does not yet broker isolated writable worktrees, run Git
integration transactions, or provide live browser executors. Those remain
separate production milestones.

## Validation and reversal

Keep this decision while the same controller passes materially different
scenario suites without scenario-name branches and resume does not duplicate
effects. Supersede it if a smaller authority boundary can preserve semantic
judgment, backend neutrality, audit reconstruction, bounded hierarchy, and
crash-safe idempotency.
