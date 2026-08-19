# Adversarial review 2 — review-1 claim refutation

Status: complete; review only; no remediation implemented

Immutable inputs:

- Plan:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md`
  (`895379e2b5a22ac53e76e94e54317a48700fac212aa92bf5ed238f218e29f59f`)
- Adversarial review 1:
  `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-review-1.md`
  (`89d026a5684cef1f14211ba03d173c7fe3d5f7447e1261a7fb2d2f2b36004e7b`)

Method: this review attempts only to refute or materially narrow R1-001 through
R1-012. Each adjudication is limited to the plan passages and evidence/source
paths cited by that R1 claim.

## R2-R1-001

- **Verdict:** materially_narrowed
- **Strongest attempted refutation:** `dispatch_action=launch` is a serial queue
  action, not itself the name of an executable. The plan separately says to
  start a fresh coordinator from the rollover summary, and the installed serial
  skill already recognizes direct `run_feature.py` as the recovery entrypoint
  for an existing successful-planner checkpoint. Therefore the plan does not
  literally select `start_planning.py`.
- **Allowed evidence with absolute source binding:**
  - The plan requires `launch` and then a fresh bounded coordinator but names no
    executable:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:759-778`.
  - The serial state machine uses `launch` for both a pending feature and a
    resumed in-progress feature:
    `<user-home>/.codex/skills/serial-implement-codex/scripts/serial_state.py:720-742`.
  - The normal fresh-dispatch protocol sends the payload to
    `start_planning.py`:
    `<user-home>/.codex/skills/serial-implement-codex/references/protocol.md:47-55`.
  - `start_planning.py` accepts only `launch` and rejects an existing worktree:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/start_planning.py:248-279`.
  - Direct recovery through `run_feature.py` is an existing, narrowly stated
    exception:
    `<user-home>/.codex/skills/serial-implement-codex/SKILL.md:52-55`.
- **Reasoning:** The categorical word “selects” is too strong because the plan
  never says to invoke `start_planning.py`, and existing policy supplies a
  direct recovery route. The attempted refutation does not remove the defect:
  the same `launch` value denotes both fresh and resumed dispatch while the
  normal launch protocol and recovery exception require different consumers.
  The plan does not bind SR-3 to the recovery consumer or define how the
  run-owned package launches it. The issue is an unresolved executable routing
  ambiguity, not proof that SR-3 affirmatively chooses the fresh planner.
- **Corrected claim wording:** SR-3 returns the same `launch` action used by
  fresh planning but does not explicitly bind a resumed existing-worktree
  dispatch to the direct `run_feature.py` recovery entrypoint; following the
  normal launch protocol would fail at the existing-worktree guard.
- **Severity/release-blocking:** Critical severity and `release_blocking=true`
  remain justified because the only live safe-resume sequence is not
  deterministically executable as written.

## R2-R1-002

- **Verdict:** upheld
- **Strongest attempted refutation:** The plan does contemplate new migration
  machinery: `controller_package.py` is assigned migration validation,
  `serial_state.py` is in Group 1's write set, and rollback text specifies a
  prepared/per-document/validated/committed journal. It could therefore be read
  as an implementation specification for extending, rather than merely using,
  current APIs.
- **Allowed evidence with absolute source binding:**
  - SR-2 calls for one apparent multi-document migration through “normal”
    compare-and-swap APIs, while rollback describes prefix recovery:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:742-757,798-811`.
  - Existing CAS updates exactly one locked file:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/state_io.py:95-119`.
  - The ledger save increments and replaces without an expected-revision
    witness:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/review_closure.py:210-219`.
  - Serial resume verifies checkpoint/transaction evidence and mutates queue
    state only:
    `<user-home>/.codex/skills/serial-implement-codex/scripts/serial_state.py:844-949`.
  - Checkpoint reopening remains a later, separate CAS:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/feature_state.py:184-225`;
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_feature.py:528-544`.
- **Reasoning:** The planned journal makes intent clearer but does not identify
  the authority that coordinates its writes or add a ledger CAS contract. More
  importantly, “atomically write ... first, then update” describes sequential
  durable mutations, and the existing APIs shown by the allowed evidence cannot
  enforce one authoritative commit boundary. The strongest refutation narrows
  how incomplete the design is, but it does not refute the missing executable
  transaction authority.
- **Corrected claim wording:** SR-2 describes a recoverable multi-document
  migration journal, but neither existing state-authority APIs nor the plan's
  declared ownership specify an executable transaction coordinator with CAS for
  every named authority and launch exclusion until committed.
- **Severity/release-blocking:** Critical severity and
  `release_blocking=true` remain justified.

## R2-R1-003

- **Verdict:** materially_narrowed
- **Strongest attempted refutation:** “dispatch/package reference” can be parsed
  as a package reference associated with dispatch rather than the persisted
  dispatch file itself. AC-3 also allows a distinct migration record, so SR-2
  need not necessarily rewrite `dispatch.v1.json`.
- **Allowed evidence with absolute source binding:**
  - The ambiguous write-set phrase and requirement to bind the digest in all
    updated state documents appear at
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:747-752`.
  - Dispatch payload is immutable run metadata:
    `<user-home>/.codex/skills/implement-v13-codex/references/json-phase-flow.md:8-13,31-34`.
  - Fresh startup persists the received payload as `dispatch.v1.json`:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/start_planning.py:281-285,478-493`.
  - Synthetic resume and verification reject a changed dispatch hash:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_synthetic_flow.py:496-507,525-536`.
- **Reasoning:** Review 1 cannot prove from the phrase alone that the plan
  mandates rewriting dispatch bytes. It can prove that the wording leaves that
  prohibited interpretation open and fails to state that original dispatch
  bytes remain immutable while a separate migration binding supplies effective
  package identity. That ambiguity is material in an “atomic migration” write
  set.
- **Corrected claim wording:** SR-2 ambiguously includes
  “dispatch/package reference” in its migration updates without explicitly
  preserving the original dispatch bytes, even though the cited contract and
  recovery consumer treat dispatch metadata as immutable.
- **Severity/release-blocking:** High severity remains justified because the
  migration write set governs durable provenance. Review 1's
  `release_blocking=false` label remains justified.

## R2-R1-004

- **Verdict:** materially_narrowed
- **Strongest attempted refutation:** AC-3 says “complete” and requires every
  controller/child path to resolve from the run-owned package. “Scripts,
  schemas, prompts, references, and package manifest” can be illustrative rather
  than exhaustive, and a manifest could include `SKILL.md`. Thus omission from
  the prose list is not proof that implementation will exclude the files.
- **Allowed evidence with absolute source binding:**
  - AC-3's completeness and run-owned resolution requirements, alongside its
    named categories, are at
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:56-68`;
    the package implementation language is at
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:268-285,322-329`.
  - The coordinator prompt reads package `SKILL.md`, and the controller imports
    the sibling serial package by relative path:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_feature.py:25-61,159-205`.
  - The phase-flow runner requires package-relative built-ins and support files:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_phase_flow.py:180-188,349-374`.
- **Reasoning:** The acceptance criterion semantically includes the runtime
  dependency closure, so the categorical claim that the proposed snapshot
  excludes these members is not established. However, the concrete manifest
  description and test do not enumerate `SKILL.md`, built-ins, or the sibling
  serial package, despite demonstrated runtime reads. “Complete” is not an
  independently checkable inventory.
- **Corrected claim wording:** AC-3 requires a complete run-owned package, but
  the concrete manifest inventory and isolation test do not explicitly cover
  all cited runtime package reads, leaving package completeness
  under-specified.
- **Severity/release-blocking:** High severity remains justified because missing
  runtime closure would make the certified recovery package non-self-contained.
  Review 1's `release_blocking=false` label remains justified.

## R2-R1-005

- **Verdict:** upheld
- **Strongest attempted refutation:** The plan makes the “production vertical
  slice” mandatory and cites the repository contract as governing context; that
  phrase could be understood to inherit the contract's complete-lifecycle
  meaning even without restating every terminal artifact.
- **Allowed evidence with absolute source binding:**
  - AC-7 and certification mandate a production vertical slice but do not name
    merge, production result, or dispatcher acknowledgment:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:114-126,663-688`.
  - The currently named vertical-slice success test intentionally ends with
    blocked checkpoint and queue:
    `<user-home>/.codex/skills/implement-v13-codex/tests/test_production_vertical_slice.py:74-219`.
  - The repository contract requires shipped subprocess entrypoints and an
    uninterrupted lifecycle through production result and queue acknowledgment:
    `<user-home>/Documents/harness_labs/docs/architecture/harness-contract.md:59-64,198-220`.
- **Reasoning:** Inheritance from the normative contract is the strongest
  defense, but the plan's concrete certification command points to an existing
  test whose asserted terminal condition is blocked. Nothing in the cited plan
  passage explicitly requires replacing or extending it to exercise integration,
  result creation, and acknowledgment. A mandatory test name with insufficient
  behavior does not satisfy the normative lifecycle gate.
- **Corrected claim wording:** The certification gate names a production
  vertical slice but does not require that the changed test reach integration,
  immutable feature result, and queue acknowledgment; the currently cited test
  terminates blocked.
- **Severity/release-blocking:** High severity remains justified. Review 1's
  `release_blocking=false` label remains justified.

## R2-R1-006

- **Verdict:** upheld
- **Strongest attempted refutation:** The planned graph is “source-bound,”
  declares immutable test nodes and edge reasons, and includes an adversarial
  omitted-edge mutation. Those requirements could support an implementation
  that validates declared edges against source evidence.
- **Allowed evidence with absolute source binding:**
  - The plan promises exact transitive selection and rejection of an
    intentionally omitted edge:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:584-590,625-644`.
  - The current ledger accepts caller-supplied dependency fields and derives
    scheduling from them:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/review_closure.py:120-193,210-218`.
  - Repository policy requires gates tied to acceptance criteria and independent
    evidence:
    `<user-home>/Documents/harness_labs/AGENTS.md:49-63`.
- **Reasoning:** “Source-bound” describes provenance but does not define an
  independent completeness oracle. The mutation test cannot deterministically
  know an edge was omitted if its only input is the candidate graph from which
  the edge is absent. No allowed evidence identifies another registry, analysis,
  or conservative fallback.
- **Corrected claim wording:** The planned graph can validate declared
  dependencies but has no specified independent source from which omission of a
  required surface-to-test edge can be detected.
- **Severity/release-blocking:** High severity remains justified. Review 1's
  `release_blocking=false` label remains justified.

## R2-R1-007

- **Verdict:** materially_narrowed
- **Strongest attempted refutation:** The plan defines three sequential groups,
  exact write sets, single-writer ownership, claim-to-group traceability,
  bounded batch size, and a review-repair sub-engine explicitly scoped away
  from full-lifecycle redesign. The cited policy does not define a numeric
  maximum feature size, so breadth alone cannot prove a violation.
- **Allowed evidence with absolute source binding:**
  - Repository policy prohibits generalized snapshots and requires bounded work:
    `<user-home>/Documents/harness_labs/AGENTS.md:47-67`.
  - Complexity admission requires a demonstrated failure, production consumer,
    and end-to-end assertion for each new mechanism:
    `<user-home>/Documents/harness_labs/docs/architecture/harness-contract.md:145-156`.
  - The plan's scope assertion, partition, mechanisms, and AC-6 breadth are at
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:7-14,101-112,216-329,391-460,528-603,820-859`.
- **Reasoning:** Review 1 overreaches by declaring the whole three-group plan a
  proven policy violation merely from mechanism count. The plan contains real
  bounds and maps its mechanisms to adjudicated failures. The narrower concern
  survives: AC-6 says “routine phase transitions,” while Group 3 is justified as
  a review-repair sub-engine, and the cited record does not give each listed
  cross-cutting mechanism its own independently executable production path
  before later machinery is admitted. This supports staged complexity
  admission, not a categorical finding that every planned mechanism is
  generalized or unrelated.
- **Corrected claim wording:** The plan is bounded in ownership and declared
  files, but it does not demonstrate per-mechanism complexity admission and
  independently executable production staging for all of the cross-cutting
  machinery it combines; its AC-6 scope is also broader than the stated
  review-repair sub-engine.
- **Severity/release-blocking:** Review 1's high severity is not justified after
  narrowing; the allowed record supports a material planning-scope concern but
  not a categorical rule violation. Its `release_blocking=false` label remains
  justified.

## R2-R1-008

- **Verdict:** upheld
- **Strongest attempted refutation:** AC-6 calls the limits hard configuration
  bounds, and certification could theoretically include an operator decision
  that supplies the values before the candidate is finally accepted.
- **Allowed evidence with absolute source binding:**
  - The plan requires hard limits yet says certification data will select their
    values:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:101-112,591-602,906-908`.
  - The installed skill forbids deriving hard limits from performance
    observations:
    `<user-home>/.codex/skills/implement-v13-codex/SKILL.md:116-119`.
  - The protocol requires an exact limit explicitly declared by an operator or
    named safety contract:
    `<user-home>/.codex/skills/implement-v13-codex/references/protocol.md:42-50`.
- **Reasoning:** The plan does not identify that operator decision or safety
  contract. It expressly makes certification data the selector, which is the
  prohibited observational source. A hypothetical later authorization is not
  present in the allowed record and cannot refute the claim.
- **Corrected claim wording:** The plan assigns state-changing hard-limit
  authority to certification observations without an exact operator-declared
  limit or named safety contract.
- **Severity/release-blocking:** High severity remains justified. Review 1's
  `release_blocking=false` label remains justified.

## R2-R1-009

- **Verdict:** upheld
- **Strongest attempted refutation:** “Readers treat missing new fields as
  legacy receipt v1; writers emit receipt v2” supplies an implicit structural
  discriminator: presence of the new required fields. A protocol bump is not
  logically mandatory if the existing protocol is intentionally extended.
- **Allowed evidence with absolute source binding:**
  - The plan specifies legacy missing-field behavior and “receipt v2” but no
    protocol string:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:331-343`.
  - The current schema fixes protocol `/1`:
    `<user-home>/.codex/skills/implement-v13-codex/schemas/process-receipt.schema.json:1-9`.
  - Both current writer paths emit `/1`:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_exec.py:482-530,795-840`.
  - The synthetic consumer accepts only `/1`:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_synthetic_flow.py:22-24,300-309`.
- **Reasoning:** A structural discriminator could work, but the plan does not
  choose it as the protocol strategy or reconcile it with the phrase “receipt
  v2.” Nor does it state how the closed new-writer requirements coexist with
  immutable `/1` evidence and the strict synthetic consumer. The strongest
  refutation offers a possible design, not one specified in the allowed record.
- **Corrected claim wording:** Receipt-v2 compatibility lacks an exact version
  discriminator and a defined reader strategy for immutable `/1` receipts and
  new required fields.
- **Severity/release-blocking:** Medium severity and
  `release_blocking=false` remain justified.

## R2-R1-010

- **Verdict:** materially_narrowed
- **Strongest attempted refutation:** Although the contract-change paragraph
  names `schemas/*-result.schema.json`, the positive test immediately requires
  enumeration of “every production response schema,” and AC-1.3 universally
  covers every canonical role-output schema. The plan therefore does not
  unambiguously limit certification to the filename glob.
- **Allowed evidence with absolute source binding:**
  - The universal AC, narrower glob, and broader positive test are at
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:40-44,312-316,345-360`.
  - Planner startup creates a task-bound schema from `plan.schema.json`:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/start_planning.py:159-168,347-395`.
  - Plan-review uses checked-in `plan-review.schema.json`:
    `<user-home>/.codex/skills/implement-v13-codex/references/protocol.md:244-248`.
  - `run_exec.py` has a special byte guard for that non-result schema:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/run_exec.py:300-320`.
- **Reasoning:** Review 1's title says the inventory “does not cover” all
  producers, but the broad AC and positive test do require universal coverage.
  The surviving defect is an internal specification conflict: the concrete glob
  cannot produce the promised complete inventory and no dispatch-derived
  inventory mechanism resolves the conflict. That is under-specification, not
  proof of intended exclusion.
- **Corrected claim wording:** The plan requires all production schema producers
  to be certified but inconsistently defines the concrete package inventory as
  `*-result.schema.json`, which does not enumerate the cited generated planner
  and plan-review producers.
- **Severity/release-blocking:** Medium severity and
  `release_blocking=false` remain justified.

## R2-R1-011

- **Verdict:** upheld
- **Strongest attempted refutation:** The event names could be payload
  classifications under existing generic top-level types such as
  `verification`, `retry`, or `phase_transition`; therefore the closed
  `event_type` enum does not by itself require a schema change.
- **Allowed evidence with absolute source binding:**
  - The plan lists the new classes, claims use of repository schemas, leaves
    alignment open, and omits a common writer from the group write sets:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:251-304,399-433,536-570,820-859,912-915`.
  - The repository event schema has a closed top-level enum containing none of
    the listed class names:
    `<user-home>/Documents/harness_labs/schemas/run-event.schema.json:1-37`.
- **Reasoning:** Payload subtypes are a plausible resolution, which is already
  acknowledged by review 1, but the plan does not provide that mapping. It also
  does not identify the writer or sequence authority within the declared scope.
  The possible compatibility strategy therefore does not make the observability
  requirement executable.
- **Corrected claim wording:** The required event classes lack a specified
  mapping into the closed repository event envelope and lack an identified
  scoped append-only writer/sequence authority.
- **Severity/release-blocking:** Medium severity and
  `release_blocking=false` remain justified.

## R2-R1-012

- **Verdict:** materially_narrowed
- **Strongest attempted refutation:** “preserve all 124 revisions' current
  durable content” can be charitably read as preserving all content currently
  durable in the ledger observed at revision 124, not as reconstructing 124
  historical files. The same plan says not to infer missing facts, supporting
  that non-fabricating interpretation.
- **Allowed evidence with absolute source binding:**
  - The disputed language is at
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:728-735`.
  - The ledger stores one document, increments `state_revision`, and replaces
    that path:
    `<user-home>/.codex/skills/implement-v13-codex/scripts/review_closure.py:184-192,210-219`.
  - The adjudication establishes revision 124 and embedded histories, not 124
    snapshots:
    `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-failure-analysis.md:482-490,512-520`.
- **Reasoning:** The charitable reading prevents review 1 from proving that the
  plan affirmatively promises fabrication of overwritten states. Nevertheless,
  the plural possessive “124 revisions'” naturally claims preservation of
  multiple revisions, while the allowed evidence establishes only one current
  revision-124 document and its embedded histories. The wording must be narrowed
  to the evidence actually available.
- **Corrected claim wording:** SR-1's phrase “all 124 revisions' current durable
  content” is ambiguous and overstates the cited evidence unless it is read as
  the current revision-124 ledger bytes plus histories embedded in that single
  document; prior revision snapshots are not established.
- **Severity/release-blocking:** Review 1's medium severity is not justified
  after narrowing because the plan also says not to infer missing facts; this is
  a provenance-wording ambiguity rather than demonstrated migration behavior.
  Its `release_blocking=false` label remains justified.

## Verdict totals

- fully_refuted: 0
- materially_narrowed: 6
- upheld: 6
- total: 12

## Exact 12-row index

| claim_id | verdict | review_1_severity_justified | review_1_release_blocking_justified |
|---|---|---:|---:|
| R2-R1-001 | materially_narrowed | true | true |
| R2-R1-002 | upheld | true | true |
| R2-R1-003 | materially_narrowed | true | true |
| R2-R1-004 | materially_narrowed | true | true |
| R2-R1-005 | upheld | true | true |
| R2-R1-006 | upheld | true | true |
| R2-R1-007 | materially_narrowed | false | true |
| R2-R1-008 | upheld | true | true |
| R2-R1-009 | upheld | true | true |
| R2-R1-010 | materially_narrowed | true | true |
| R2-R1-011 | upheld | true | true |
| R2-R1-012 | materially_narrowed | false | true |
