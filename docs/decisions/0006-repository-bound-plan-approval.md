# 0006 — Repository-bound PlanGraph approval

Status: accepted
Date: 2026-08-10
Owners: harness controller

## Context

PlanGraph consumes a plan decomposition and begins FeatureRuns without durable
evidence that the exact plan, decomposition, repository revision, scope grants,
commands, and timeouts were approved. Its audit digest detects some resume-time
drift but does not establish approval authority. The flow-editor review exposed
untracked plans, incorrect paths, missing criterion assignments, unusable
clean-checkout commands, and undeclared operator decisions before execution.

`plan-projection-design.md` intentionally excluded approval envelopes and
automatic plan revision until a delivered workflow demonstrated a concrete
need. The observed gap is now narrow and has a production consumer:
`PlanGraph.run()` and its resume path must launch no FeatureRun for an
unapproved or mismatched subject.

## Decision

Adopt the admission-only Slices 0 and 1 in
`docs/development/plan-approval-design.md`:

- define one canonical `plan-graph-plan/1` artifact containing the reviewed
  decomposition, per-run writable scope, verification commands and timeouts,
  path intents, command dependencies, and final functionality commands;
- keep `base_commit` in the approval subject rather than the committed
  decomposition, avoiding a self-referential commit hash;
- define one canonical PlanGraph identity implementation shared by approval and
  PlanGraph audit;
- require an immutable, operator-attested approval receipt through the shipped
  PlanGraph CLI;
- revalidate repository objects and host-executable identities before first
  launch and on resume; and
- fail closed before a node worktree or launcher is created when approval,
  scope, command, policy, or evidence does not match.

This decision is a narrow exception to the approval-envelope exclusion in
`plan-projection-design.md`. It does not authorize automatic review, semantic
plan revision, reviewer plugins, or recovery that changes a plan during an
active graph. Those remain deferred Slices 2 and 3.

The first trust domain is one local machine. Controller issuance provides
policy enforcement and auditable provenance, not protection against a
malicious actor with equivalent local write and process authority.

## Mid-graph supersession

A corrected plan creates a new subject and a new PlanGraph. The initial policy
does not graft completed nodes or checkpoints from the superseded graph; it
reruns the graph from the newly approved base and retains the old candidate
lineage only as evidence. Preserving completed work requires a later explicit
lineage-transfer contract and separate approval against the last good candidate.

## Referenced artifacts

The base commit already binds the complete Git tree. The subject's
`referenced_artifacts` list exists to enumerate reviewer inputs and assemble
minimal context, not to provide additional integrity coverage.

## Alternatives

- Continue treating “approved” as caller-supplied prose. This preserves the
  demonstrated drift and authority gap.
- Hash only the plan and decomposition. This detects byte changes but does not
  bind repository identity, execution scope, environment evidence, policy, or
  approval authority.
- Build automated reviewers and revision in the same release. This exceeds the
  demonstrated minimum and violates complexity admission.
- Reuse completed nodes after plan supersession. Without an explicit semantic
  lineage contract this can apply old work to changed requirements.

## Evidence

- `docs/development/plan-approval-design.md` records the reviewed contract and
  acceptance criteria.
- The existing `PlanGraphAudit` plan/graph digest demonstrates the current
  partial integrity mechanism and production consumer.
- `tests/test_plan_approval.py` proves shared identity, ordered graph-created
  command dependencies, host-executable invalidation, exact scope/timeout
  transport, operator-attested issuance, production CLI launch, and zero
  launcher calls after approval tampering on resume.
- The complete repository test suite passes with Slices 0 and 1 enabled.

## Consequences

Plans and canonical decompositions must be committed before approval. Operators
perform an explicit subject-bound attestation in Slice 1. Final commands become
structured argv and implicit shell execution is removed. PlanGraph owns the
authoritative per-node grants it transmits to FeatureRun adapters.

Approval becomes dependent on recorded host executable identities when commands
use host tools. Changing such a tool invalidates admission. Mid-graph plan
correction is intentionally expensive because correctness takes precedence over
automatic salvage.

## Validation and reversal

Keep this decision while the production CLI fails closed before launcher
invocation for every bound-input mismatch and successful runs record the shared
subject/receipt identity in PlanGraph audit. Revisit it if the operator-attested
workflow does not prevent observed drift, host binding proves impractical, or
production evidence justifies automated reviewers, revision, or lineage
transfer.
