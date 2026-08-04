# 0001 — Execution-first production lifecycle

Status: accepted
Date: 2026-07-21
Owners: Harness Labs maintainers
Run: not applicable

## Context

A harness can validate schemas, transitions, receipts, recovery primitives, and
synthetic phase catalogs while still lacking a production controller that advances
a real feature. Component and marker-flow success can therefore conceal an
ownerless lifecycle boundary and produce a false completion claim.

## Decision

Harness development is sequenced as:

1. smallest uninterrupted production vertical slice;
2. correctness and role-boundary hardening;
3. recovery and replay;
4. observability and optimization beyond what the working path requires.

The production slice begins at the real dispatch entrypoint and reaches the
production result and queue acknowledgment without another operator message. It
uses the real controller, worktree, prompts, checkpoints, receipts, and queue,
with deterministic stub model outputs permitted for testing. Every nonterminal
checkpoint has a verified live owner.

Synthetic flows, direct transition tests, and fabricated terminal artifacts are
supporting evidence only. A new control-plane mechanism is admitted only when it
names a demonstrated production failure, its production consumer, and the
end-to-end assertion it supports.

## Alternatives

- Complete schemas, recovery, and synthetic certification before integrating the
  production path. Rejected because individually correct components do not prove
  lifecycle liveness.
- Rely on a natural-language run owner to interpret phase documents indefinitely.
  Rejected because task lifetime and ownership transfer require executable proof.
- Treat complete synthetic phase traversal as production conformance. Rejected
  because marker work does not exercise feature implementation or integration.

## Evidence

- [`../architecture/harness-contract.md`](../architecture/harness-contract.md)
  defines the production lifecycle and ownership requirements.
- [`../development/NEXT_STEPS.md`](../development/NEXT_STEPS.md) sequences the
  minimal conforming harness around the production vertical slice.
- [`../observability/logging-and-metrics.md`](../observability/logging-and-metrics.md)
  separates production, component, synthetic, and fabricated evidence.

## Consequences

Milestones cannot claim conformance from component or synthetic coverage alone.
The first integration test may use deterministic model stubs, but it must use the
real production control path. Recovery and optimization work is deferred until
that uninterrupted path passes. Each nonterminal handoff must carry verifiable
ownership rather than an arbitrary coordinator label.

## Validation and reversal

Validate the decision with an automated run from production dispatch through
queue acknowledgment, requiring no additional operator input and detecting any
ownerless nonterminal checkpoint. Revise or supersede this decision only if an
alternative test provides equivalent proof of production lifecycle execution and
ownership continuity.
