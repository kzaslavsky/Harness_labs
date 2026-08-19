# Adjudication — implement-v13-codex remediation plan adversarial reviews

Status: complete; adjudication only; no remediation implemented

Immutable inputs:

- Plan:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md`
  (`895379e2b5a22ac53e76e94e54317a48700fac212aa92bf5ed238f218e29f59f`)
- Adversarial review 1:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-review-1.md`
  (`89d026a5684cef1f14211ba03d173c7fe3d5f7447e1261a7fb2d2f2b36004e7b`)
- Adversarial review 2:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-review-2.md`
  (`c7f23d7abda27d2d042039673705a0d51193691a8c15c71cef2c812753a843d6`)

Scope: exactly R1-001 through R1-012 adjudicated against R2-R1-001 through
R2-R1-012. The controlling record is limited to the evidence/source paths cited
by each paired claim. Critical means the plan cannot safely or deterministically
be implemented or resumed without correction.

## J-R1-001

- **Ruling:** R2 narrowing adopted
- **Controlling evidence:** SR-3 requires resumed `prepare_dispatch` to return
  `dispatch_action=launch` and then starts a fresh coordinator, but names no
  executable:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:759-778`.
  The serial state machine uses `launch` for both pending and resumed
  in-progress features:
  `<user-home>/.codex/skills/serial-implement-codex/scripts/serial_state.py:720-742`.
  The normal launch protocol invokes `start_planning.py`, which accepts `launch`
  but rejects an existing worktree:
  `<user-home>/.codex/skills/serial-implement-codex/references/protocol.md:47-55`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/start_planning.py:248-279`.
  Direct `run_feature.py` recovery is an existing exception:
  `<user-home>/.codex/skills/serial-implement-codex/SKILL.md:52-55`.
- **Authoritative corrected finding wording:** SR-3 returns the same `launch`
  action used by fresh planning but does not explicitly bind a resumed
  existing-worktree dispatch to the direct `run_feature.py` recovery entrypoint;
  following the normal launch protocol would fail at the existing-worktree
  guard.
- **Final severity:** critical
- **Release-blocking:** yes
- **Exact plan correction:** Define an explicit resumed-run dispatch action, or
  an equivalently unambiguous package-migration recovery flag, whose sole
  consumer is the run-owned `run_feature.py`; specify its CLI, validation, and
  lease semantics, and prohibit routing the migrated run through
  `start_planning.py`. Add an end-to-end test beginning from a blocked
  `REVIEWING/fix` checkpoint and existing worktree that performs authorized
  serial resume, launches the run-owned recovery controller, reopens only that
  checkpoint detail, and reaches the first post-migration deterministic gate.

## J-R1-002

- **Ruling:** R1 upheld
- **Controlling evidence:** SR-2 calls for one apparent multi-document migration
  through normal compare-and-swap APIs while its recovery section describes
  prefix recovery:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:742-757,798-811`.
  Existing CAS updates one locked file:
  `<user-home>/.codex/skills/implement-v13-codex/scripts/state_io.py:95-119`.
  Ledger save increments and replaces without an expected-revision witness:
  `<user-home>/.codex/skills/implement-v13-codex/scripts/review_closure.py:210-219`.
  Serial resume validates checkpoint/transaction evidence and mutates queue
  state only, while checkpoint reopening is a later separate mutation:
  `<user-home>/.codex/skills/serial-implement-codex/scripts/serial_state.py:844-949`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/feature_state.py:184-225`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_feature.py:528-544`.
- **Authoritative corrected finding wording:** SR-2 describes a recoverable
  multi-document migration journal, but neither existing state-authority APIs
  nor the plan's declared ownership specify an executable transaction
  coordinator with CAS for every named authority and launch exclusion until
  committed.
- **Final severity:** critical
- **Release-blocking:** yes
- **Exact plan correction:** Add an explicit controller-owned migration command
  and contract that acquires authorities in a fixed order, uses per-document CAS
  witnesses including a ledger CAS API, and persists a `prepared` journal that
  becomes authoritative only when committed. Keep serial queue mutation inside
  `serial_state.py`, define forward recovery for every durable-write prefix, and
  add crash-injection tests after every durable write proving neither resume nor
  child launch is selectable until every authority validates against one
  committed migration.

## J-R1-003

- **Ruling:** R2 narrowing adopted
- **Controlling evidence:** SR-2 ambiguously includes a “dispatch/package
  reference” in its updated state:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:747-752`.
  Dispatch is immutable metadata, fresh startup persists it as
  `dispatch.v1.json`, and synthetic recovery rejects byte changes:
  `<user-home>/.codex/skills/implement-v13-codex/references/json-phase-flow.md:8-13,31-34`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/start_planning.py:281-285,478-493`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_synthetic_flow.py:496-507,525-536`.
- **Authoritative corrected finding wording:** SR-2 ambiguously includes
  “dispatch/package reference” in its migration updates without explicitly
  preserving the original dispatch bytes, even though the cited contract and
  recovery consumer treat dispatch metadata as immutable.
- **Final severity:** high
- **Release-blocking:** no

## J-R1-004

- **Ruling:** R2 narrowing adopted
- **Controlling evidence:** AC-3 requires a complete run-owned controller
  package but its named inventory is scripts, schemas, prompts, references, and
  a manifest:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:56-68,268-285,322-329`.
  Runtime reads also require package `SKILL.md`, the sibling serial controller,
  and built-ins:
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_feature.py:25-61,159-205`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_phase_flow.py:180-188,349-374`.
- **Authoritative corrected finding wording:** AC-3 requires a complete
  run-owned package, but the concrete manifest inventory and isolation test do
  not explicitly cover all cited runtime package reads, leaving package
  completeness under-specified.
- **Final severity:** high
- **Release-blocking:** no

## J-R1-005

- **Ruling:** R1 upheld
- **Controlling evidence:** AC-7 and certification mandate a production vertical
  slice without requiring merge, production result, or dispatcher
  acknowledgment:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:114-126,663-688`.
  The currently named production vertical-slice test intentionally ends with a
  blocked checkpoint and queue:
  `<user-home>/.codex/skills/implement-v13-codex/tests/test_production_vertical_slice.py:74-219`.
  The repository contract requires the shipped entrypoints and an uninterrupted
  lifecycle through production result and queue acknowledgment:
  `<user-home>/Documents/harness_labs/docs/architecture/harness-contract.md:59-64,198-220`.
- **Authoritative corrected finding wording:** The certification gate names a
  production vertical slice but does not require that the changed test reach
  integration, immutable feature result, and queue acknowledgment; the
  currently cited test terminates blocked.
- **Final severity:** high
- **Release-blocking:** no

## J-R1-006

- **Ruling:** R1 upheld
- **Controlling evidence:** The plan promises exact transitive test selection
  and rejection of an intentionally omitted dependency edge:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:584-590,625-644`.
  The current ledger accepts caller-supplied dependency fields and schedules
  from those same fields:
  `<user-home>/.codex/skills/implement-v13-codex/scripts/review_closure.py:120-193,210-218`.
  Repository policy requires verification tied to acceptance criteria and
  independently evidenced worker claims:
  `<user-home>/Documents/harness_labs/AGENTS.md:49-63`.
- **Authoritative corrected finding wording:** The planned graph can validate
  declared dependencies but has no specified independent source from which
  omission of a required surface-to-test edge can be detected.
- **Final severity:** high
- **Release-blocking:** no

## J-R1-007

- **Ruling:** R2 narrowing adopted
- **Controlling evidence:** Repository policy prohibits generalized snapshots
  and requires bounded work:
  `<user-home>/Documents/harness_labs/AGENTS.md:47-67`.
  The architecture contract requires demonstrated failure, production consumer,
  and end-to-end assertion for each new mechanism:
  `<user-home>/Documents/harness_labs/docs/architecture/harness-contract.md:145-156`.
  The plan declares sequential groups, exact ownership, and bounded batch size,
  but combines multiple cross-cutting mechanisms and extends AC-6 to routine
  phase transitions:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:7-14,101-112,216-329,391-460,528-603,820-859`.
- **Authoritative corrected finding wording:** The plan is bounded in ownership
  and declared files, but it does not demonstrate per-mechanism complexity
  admission and independently executable production staging for all of the
  cross-cutting machinery it combines; its AC-6 scope is also broader than the
  stated review-repair sub-engine.
- **Final severity:** medium
- **Release-blocking:** no

## J-R1-008

- **Ruling:** R1 upheld
- **Controlling evidence:** The plan requires hard limits while directing
  certification data to select their values:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:101-112,591-602,906-908`.
  The installed skill forbids deriving hard limits from observations, and the
  protocol requires an exact operator-declared value or named safety contract:
  `<user-home>/.codex/skills/implement-v13-codex/SKILL.md:116-119`;
  `<user-home>/.codex/skills/implement-v13-codex/references/protocol.md:42-50`.
- **Authoritative corrected finding wording:** The plan assigns state-changing
  hard-limit authority to certification observations without an exact
  operator-declared limit or named safety contract.
- **Final severity:** high
- **Release-blocking:** no

## J-R1-009

- **Ruling:** R1 upheld
- **Controlling evidence:** The plan describes legacy missing-field behavior and
  “receipt v2” without a protocol discriminator:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:331-343`.
  The current schema and both writer paths use protocol `/1`, and the synthetic
  consumer accepts only `/1`:
  `<user-home>/.codex/skills/implement-v13-codex/schemas/process-receipt.schema.json:1-9`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_exec.py:482-530,795-840`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_synthetic_flow.py:22-24,300-309`.
- **Authoritative corrected finding wording:** Receipt-v2 compatibility lacks an
  exact version discriminator and a defined reader strategy for immutable `/1`
  receipts and new required fields.
- **Final severity:** medium
- **Release-blocking:** no

## J-R1-010

- **Ruling:** R2 narrowing adopted
- **Controlling evidence:** AC-1.3 and the positive test require universal
  production-schema coverage, but the concrete package inventory uses
  `schemas/*-result.schema.json`:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:40-44,312-316,345-360`.
  Planner startup generates a task-bound schema from `plan.schema.json`, while
  plan review uses `plan-review.schema.json` and a special production byte guard:
  `<user-home>/.codex/skills/implement-v13-codex/scripts/start_planning.py:159-168,347-395`;
  `<user-home>/.codex/skills/implement-v13-codex/references/protocol.md:244-248`;
  `<user-home>/.codex/skills/implement-v13-codex/scripts/run_exec.py:300-320`.
- **Authoritative corrected finding wording:** The plan requires all production
  schema producers to be certified but inconsistently defines the concrete
  package inventory as `*-result.schema.json`, which does not enumerate the
  cited generated planner and plan-review producers.
- **Final severity:** medium
- **Release-blocking:** no

## J-R1-011

- **Ruling:** R1 upheld
- **Controlling evidence:** The plan lists the new event classes, claims use of
  repository schemas, leaves schema alignment open, and omits a common writer
  from the group write sets:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:251-304,399-433,536-570,820-859,912-915`.
  The repository event schema has a closed top-level enum containing none of the
  listed names:
  `<user-home>/Documents/harness_labs/schemas/run-event.schema.json:1-37`.
- **Authoritative corrected finding wording:** The required event classes lack a
  specified mapping into the closed repository event envelope and lack an
  identified scoped append-only writer/sequence authority.
- **Final severity:** medium
- **Release-blocking:** no

## J-R1-012

- **Ruling:** R2 narrowing adopted
- **Controlling evidence:** SR-1 says to preserve “all 124 revisions' current
  durable content” while also prohibiting inference of missing facts:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:728-735`.
  The ledger implementation stores one document, increments
  `state_revision`, and replaces that path:
  `<user-home>/.codex/skills/implement-v13-codex/scripts/review_closure.py:184-192,210-219`.
  The audit establishes revision 124 and histories embedded in the current
  ledger, not 124 retained snapshots:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-failure-analysis.md:482-490,512-520`.
- **Authoritative corrected finding wording:** SR-1's phrase “all 124 revisions'
  current durable content” is ambiguous and overstates the cited evidence unless
  read as the current revision-124 ledger bytes plus histories embedded in that
  single document; prior revision snapshots are not established.
- **Final severity:** low
- **Release-blocking:** no

## Exact totals

- Rulings — R1 upheld: 6; R2 narrowing adopted: 6; R1 rejected: 0; total: 12.
- Final severity — critical: 2; high: 5; medium: 4; low: 1; total: 12.
- Release-blocking — yes: 2; no: 10; total: 12.
- Critical claim IDs: J-R1-001, J-R1-002.

## Machine-readable 12-row index

| claim_id | paired_claim_id | ruling | final_severity | release_blocking |
|---|---|---|---|---|
| J-R1-001 | R2-R1-001 | R2 narrowing adopted | critical | true |
| J-R1-002 | R2-R1-002 | R1 upheld | critical | true |
| J-R1-003 | R2-R1-003 | R2 narrowing adopted | high | false |
| J-R1-004 | R2-R1-004 | R2 narrowing adopted | high | false |
| J-R1-005 | R2-R1-005 | R1 upheld | high | false |
| J-R1-006 | R2-R1-006 | R1 upheld | high | false |
| J-R1-007 | R2-R1-007 | R2 narrowing adopted | medium | false |
| J-R1-008 | R2-R1-008 | R1 upheld | high | false |
| J-R1-009 | R2-R1-009 | R1 upheld | medium | false |
| J-R1-010 | R2-R1-010 | R2 narrowing adopted | medium | false |
| J-R1-011 | R2-R1-011 | R1 upheld | medium | false |
| J-R1-012 | R2-R1-012 | R2 narrowing adopted | low | false |
