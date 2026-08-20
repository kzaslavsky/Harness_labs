# 0006 — Parallel PlanGraph admission and recovery contract

Status: accepted
Concerns-paths: harness_labs/plangraph/plan_graph.py, harness_labs/plangraph/plan_graph_join.py, harness_labs/plangraph/plan_graph_authority.py, schemas/plan-graph-parallel-decomposition.schema.json
Date: 2026-08-10
Owners: PlanGraph controller

## Context

`harness_labs/plan_graph.py` is a serial, audited queue at this decision's
base (`86b34234c97df31d5338fe2adf91de2586751c81`). The accepted replay plan
requires a bounded fork/join scheduler without weakening custody, recovery, or
FeatureRun evidence rules. In particular, a child process being live (or
printing JSON) is not proof that a candidate was sealed.

## Decision

Future parallel PlanGraph implementations MUST admit only
`harness-plan-graph-parallel-decomposition/1` decompositions and MUST preserve
the three companion contracts in `schemas/`:

- `plan-graph-parallel-child-request/1` freezes one reservation, immutable
  parent candidate, lane identity, writable paths, and the complete ordered
  dependency-candidate set for a join.
- `plan-graph-parallel-checkpoint/1` is the durable scheduling authority. It
  records frontier sets, reservations, allocations, the current staging head,
  and at most one graph-owned integration lease. Liveness is intentionally not
  stored there.
- `plan-graph-parallel-seal-receipt/1` is the only success-adoption input. A
  seal requires matching descriptor, allocation, candidate, verification, and
  terminal-journal evidence references. Child stdout may be retained as an
  artifact but can never establish success by itself.

The remaining versioned boundary records are mandatory: `logical-attempt` is
the controller-owned, monotonically numbered graph attempt; `allocation`
records a single CAS allocation from that attempt; `event` records append-only
state transitions; `child-evidence-notification` carries untrusted child
evidence; `liveness` records a PID plus its immutable start token; `resume` and
`force-reconcile` record recovery authority; `protected-ref` names the one
controller-owned staging ref; and `integration-receipt` proves the final
compare-and-swap integration. A receipt or notification from another attempt,
allocation, parent candidate, or candidate manifest MUST be rejected.

The state machine is `queued -> reserved -> running -> sealed`; `queued` or
`reserved` may become `blocked` after a failed dependency, and `running` may
become `blocked` if recovery cannot prove a sealed outcome. A live child stays
`running`; a dead child with a complete matching receipt becomes `sealed`; a
dead or unavailable child without that receipt is ambiguous and becomes
`blocked`, never retried implicitly.

Reservation is compare-and-swap: it names the checkpoint revision and expected
staging head. The controller atomically records every allocation in a batch
before launch. A completion may seal only its exact allocation and immutable
parent; a stale revision or changed staging head blocks the operation. Sibling
lanes may not advance the graph staging branch. A join alone holds the
integration lease and CAS-advances that head after its complete dependency set
has verified seal receipts.

Dependency-derived bases and order are not implementation choices. A root uses
the decomposition base commit. A non-join lane uses its sole sealed dependency
candidate. A join constructs its base from its declared dependency candidates
in stable decomposition sibling order, verifies that construction before it is
eligible for dispatch, and consumes one execution slot while doing so. The
controller continues every already-reserved sibling after a lane failure, then
drains and records every terminal result; it blocks dependent nodes and never
launches a new dependent after the failure.

Only the controller owns admission, allocation, durable checkpoint mutation,
protected-ref mutation, recovery decisions, and final integration. Children may
write only their allocated worktree paths and may only emit evidence
notifications. On interruption, the controller checkpoints the outstanding
allocation identities, does not reuse them, and resumes only from the last
verified checkpoint. A stale heartbeat whose PID and start token still match
remains running and is not redispatched; a missing or mismatched identity is
ambiguous and blocks until an explicit force-reconcile record supplies durable
evidence. Serial decompositions retain their existing one-ready-node order and
their serial base/ref semantics; this contract adds no alternate serial path.

## Alternatives

- Silently serialize a decomposition that declares parallel fields. Rejected:
  it changes the approved topology and hides unsupported authority.
- Treat child exit status or stdout as terminal authority. Rejected: either can
  exist without a durable candidate, verified test evidence, or a matching
  reservation.
- Infer a dead child outcome from the current branch head. Rejected: concurrent
  lanes and interrupted joins make the head ambiguous.

## Evidence

- `docs/development/parallel-plangraph-execution-replay-plan.md` specifies the
  ready frontier, lane custody, join, and recovery requirements.
- `docs/decisions/0002-controller-owned-parallel-child-batches.md` establishes
  complete pre-launch reservation, deterministic allocation, and collect-all.
- `tests/test_plan_graph_parallel_contracts.py` validates the closed schemas,
  representative fork/join fixtures, state/liveness truth table, receipt rule,
  and CAS predicates without wall-clock assertions.

## Consequences

Production scheduling is deliberately deferred. When added, it must reject
unsupported parallel input, use these versioned contracts, and retain the
baseline fixture byte-for-byte. Recovery stops on ambiguity; it may not relaunch
or integrate based on stdout, liveness, or an observed branch head.

## Validation and reversal

The contract remains until an implementation passes its admission, recovery,
lane-isolation, join, and final-integration gates. A successor may change a
protocol only through a new versioned schema and migration decision; it must not
reinterpret existing checkpoints.
