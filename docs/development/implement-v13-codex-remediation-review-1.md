# Adversarial review 1 — implement-v13-codex remediation plan

Status: complete; review only; no remediation implemented

Reviewed artifact:
`/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md`

Verified SHA-256:
`895379e2b5a22ac53e76e94e54317a48700fac212aa92bf5ed238f218e29f59f`

Verification command:

```text
shasum -a 256 docs/development/implement-v13-codex-remediation-plan.md
```

Observed output:

```text
895379e2b5a22ac53e76e94e54317a48700fac212aa92bf5ed238f218e29f59f  docs/development/implement-v13-codex-remediation-plan.md
```

Review basis: the immutable plan above, the adjudicated audit at
`/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-failure-analysis.md`,
the installed `implement-v13-codex` and `serial-implement-codex` packages, and
the repository contracts cited below. Findings challenge the plan, not the
adjudicated existence of C1-C14.

## Release-blocking findings

### R1-001 — The safe-resume launch sequence selects the fresh-planning entrypoint for an existing worktree

- **Severity:** critical
- **Challenged plan section:** `SR-3 — Token-gated serial resume and first bounded action`,
  lines 759-778, especially lines 766-771.
- **Evidence:**
  - The plan requires `resume_blocked_feature`, then requires
    `prepare_dispatch` to return `dispatch_action=launch`:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:766-771`.
  - `prepare_dispatch` consumes `launch_authorized` and does return `launch` for
    a resumed in-progress feature:
    `/Users/kirillzaslavsky/.codex/skills/serial-implement-codex/scripts/serial_state.py:720-742`.
  - The normal meaning of a launch dispatch is to invoke `start_planning.py`:
    `/Users/kirillzaslavsky/.codex/skills/serial-implement-codex/references/protocol.md:47-55`.
  - `start_planning.py` rejects an existing feature worktree before it can enter
    `run_feature.py`:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/start_planning.py:248-279`.
  - The currently authorized recovery route for an existing successful-planner
    checkpoint is direct `run_feature.py`, not `start_planning.py`:
    `/Users/kirillzaslavsky/.codex/skills/serial-implement-codex/SKILL.md:52-55`.
- **Reasoning:** The live subject is already at `REVIEWING/fix` with an existing
  worktree. The proposed sequence creates a launch-shaped dispatch but never
  names the recovery executable. Following the normal launch contract fails at
  the worktree-exists guard; following the recovery exception requires a
  different command that the plan does not specify. A fresh coordinator context
  does not make this a fresh planner run.
- **Concrete correction:** Define one explicit resumed-run dispatch action (or a
  package-migration recovery flag) whose sole consumer is the run-owned
  `run_feature.py`. Specify its CLI, validation, lease semantics, and test. Do
  not route the migrated run through `start_planning.py`. Add an end-to-end test
  that starts from a blocked `REVIEWING/fix` checkpoint and existing worktree,
  performs authorized serial resume, launches the run-owned recovery controller,
  reopens only that checkpoint detail, and reaches the first post-migration
  deterministic gate.
- **Falsification condition:** This claim is falsified if an existing production
  API and test can be cited that consumes the proposed post-resume `launch`
  payload, preserves the existing worktree/checkpoint, starts a fresh coordinator
  thread, and does not call the fresh-worktree path in `start_planning.py`.

### R1-002 — “Atomic migration” cannot be executed through the existing state-authority APIs

- **Severity:** critical
- **Challenged plan section:** `SR-2 — Independent approval and atomic migration`,
  lines 742-757, and `Rollback and recovery`, lines 798-811.
- **Evidence:**
  - The plan proposes one operation that writes a migration receipt and then
    updates the ledger, checkpoint, transaction, dispatch, and queue:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:747-757`.
  - The generic compare-and-swap helper locks and updates exactly one file:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/state_io.py:95-119`.
  - The closure-ledger save path increments a revision and replaces the file
    without a compare-and-swap witness or advisory lock:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:210-219`.
  - Serial resume validates surviving checkpoint/transaction hashes and mutates
    only queue state; it has no migration transaction over the other documents:
    `/Users/kirillzaslavsky/.codex/skills/serial-implement-codex/scripts/serial_state.py:844-949`.
  - Checkpoint resume is a later, separate one-file mutation performed by
    `run_feature.py` after serial authorization:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/feature_state.py:184-225`;
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:528-544`.
- **Reasoning:** No existing authority API can perform the proposed multi-document
  write, and the plan's exact write sets do not name a new migration coordinator
  command or state API. The rollback text describes a recoverable multi-file
  transaction, not an atomic transaction, and byte-restoring queue/checkpoint/
  ledger files would bypass their current ownership and revision APIs. A crash
  after a prefix of writes can therefore expose a package identity that is
  inconsistent across authorities.
- **Concrete correction:** Add an explicit controller-owned migration command
  and contract. It must acquire authorities in a fixed order, use per-document
  compare-and-swap witnesses (including a new ledger CAS API), persist a
  `prepared` journal that is not authoritative until committed, define forward
  recovery for every prefix, and keep serial queue mutation inside
  `serial_state.py`. Include crash-injection tests after every durable write and
  prove that neither resume nor child launch is selectable until all documents
  validate against one committed migration.
- **Falsification condition:** This claim is falsified if the current packages
  already expose a cited command/API that transactionally migrates all named
  documents with their proper locks, CAS revisions, crash-prefix recovery, and
  launch exclusion, and the plan binds SR-2 to that API.

## Other material findings

### R1-003 — SR-2 rewrites immutable dispatch metadata

- **Severity:** high
- **Challenged plan section:** `SR-2`, lines 747-752, especially “update the
  ... dispatch/package reference.”
- **Evidence:**
  - The proposed migration includes the persisted dispatch among updated state:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:747-752`.
  - The phase-flow contract classifies dispatch metadata as immutable:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/references/json-phase-flow.md:8-13,31-34`.
  - Fresh startup persists the exact dispatch payload and later hands that file
    to `run_feature.py`:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/start_planning.py:281-285,478-493`.
  - The synthetic compatibility path explicitly rejects dispatch-byte changes:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_synthetic_flow.py:496-507,525-536`.
- **Reasoning:** Rewriting `dispatch.v1.json` destroys the original dispatch
  witness and changes an input that existing recovery treats as persisted
  metadata. The migration receipt can supersede an effective package selection
  without altering the original dispatch.
- **Concrete correction:** Keep the original dispatch byte-immutable. Put the
  effective package digest and migration receipt in a separate versioned
  run-control binding referenced by the queue/checkpoint/transaction. Make
  `run_feature.py` resolve `original dispatch + committed migration binding` and
  hash both. Test that the original dispatch hash remains unchanged through
  migration and recovery.
- **Falsification condition:** This claim is falsified if the plan removes the
  dispatch from the write set or cites a governing contract that makes the
  dispatch mutable with preserved original bytes and updates every consumer
  accordingly.

### R1-004 — The proposed “complete” package snapshot omits runtime-required package members

- **Severity:** high
- **Challenged plan section:** AC-3 lines 56-68; Group 1 lines 268-285 and
  322-329.
- **Evidence:**
  - AC-3 enumerates scripts, schemas, prompts, references, and a manifest, but
    does not name `SKILL.md`:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:56-68`.
  - A fresh coordinator prompt explicitly directs the child to read the package's
    `SKILL.md` and required references:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:159-205`.
  - `run_feature.py` dynamically loads the sibling serial controller from a path
    relative to its own package:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:25-61`.
  - The installed package also contains runtime `builtins`, while the plan's
    named package categories do not include them; the debug runner reads a
    built-in prompt by package-relative path:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_phase_flow.py:180-188,349-374`.
- **Reasoning:** A manifest that excludes `SKILL.md` cannot bootstrap the fresh
  coordinator required by the migration. A snapshot that resolves the serial
  sibling or built-ins from the global installation is not one explicit
  controller identity. “Complete bounded package” needs a runtime dependency
  closure, not a hand-listed category description.
- **Concrete correction:** Define the package manifest from an explicit runtime
  dependency allowlist that includes `SKILL.md`, all required references,
  scripts, schemas, required built-ins, and the sibling serial controller files.
  State which non-production examples/tests are excluded. Add a test that
  temporarily makes both global installed skill directories unavailable and
  completes dispatch/recovery using only the run-owned package.
- **Falsification condition:** This claim is falsified if the package schema and
  tests explicitly include every package-relative runtime read above and prove
  that no controller/child path resolves from the global installation.

### R1-005 — Certification does not require a successful production lifecycle through dispatcher acknowledgment

- **Severity:** high
- **Challenged plan section:** AC-7 lines 114-126 and `Full certification gate`,
  lines 663-688.
- **Evidence:**
  - The plan asks for “the production vertical slice” but does not require the
    candidate to reach merge, feature result, and `dispatcher_ack`:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:114-126,663-688`.
  - The current named vertical-slice success test terminates intentionally in a
    blocked checkpoint and blocked queue:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/tests/test_production_vertical_slice.py:74-219`.
  - The normative repository contract requires the shipped dispatch and startup
    CLI path and a complete production lifecycle/queue acknowledgment; direct or
    synthetic state fabrication is insufficient:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/architecture/harness-contract.md:59-64,198-220`.
- **Reasoning:** All three groups change production dispatch, execution,
  recovery, review routing, and package identity. A blocked vertical slice can
  prove settlement but cannot prove that the new control plane integrates,
  commits, merges, cleans up, writes the immutable result, and acknowledges the
  queue.
- **Concrete correction:** Make a clean subprocess test from the shipped
  `serial_state.py dispatch` CLI through `start_planning.py` mandatory. With
  deterministic stub role outputs but real controller/worktree/state files, it
  must traverse every phase, commit/merge/cleanup, write
  `feature-result.v1.json`, and reach transaction `dispatcher_ack` and queue
  `done`. Add a second production recovery slice for the migrated blocked run.
- **Falsification condition:** This claim is falsified if the certification
  section names an existing mandatory test that demonstrably reaches
  `dispatcher_ack` through the shipped entrypoints after exercising the changed
  package, migration, scheduler, and gate paths.

### R1-006 — The dependency-graph completeness gate has no independent source of truth

- **Severity:** high
- **Challenged plan section:** AC-5 lines 87-99; Group 3 lines 572-590 and
  618-647.
- **Evidence:**
  - The plan says changed surfaces select exactly the transitive immutable tests
    and that an intentionally omitted dependency must be rejected:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:584-590,625-644`.
  - The current ledger accepts caller-supplied dependency fields, cycle-checks
    them, then schedules from those same fields:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:120-193,210-218`.
  - The repository requires verification gates to be tied to acceptance criteria
    and worker claims to be independently evidenced:
    `/Users/kirillzaslavsky/Documents/harness_labs/AGENTS.md:49-63`.
- **Reasoning:** A validator cannot discover an omitted edge by validating only
  the graph that omitted it. The plan names no independent coverage registry,
  instrumentation trace, immutable assertion map, or conservative fallback from
  which completeness is checked. The “missing dependency edge” mutation test is
  therefore circular and AC-5.3/AC-5.4 are not presently testable.
- **Concrete correction:** Define an independent source of truth for
  surface-to-test coverage (for example, a versioned test-impact registry plus
  runtime trace evidence), its producer, its conservative behavior for unknown
  surfaces, and its hash binding. Graph validation must compare against that
  source and fall back to the full required regression suite when completeness
  cannot be proved.
- **Falsification condition:** This claim is falsified if the plan cites an
  independent, production-consumed artifact or deterministic analysis that can
  identify a deliberately omitted edge without consulting the candidate graph.

### R1-007 — The three-group plan violates the bounded-work/no-generalized-framework rule as written

- **Severity:** high
- **Challenged plan section:** scope assertion lines 7-14; implementation
  partition lines 216-242; Groups 1-3 lines 244-661.
- **Evidence:**
  - The repository explicitly forbids a generalized snapshot framework or
    unrelated refactor and requires bounded work:
    `/Users/kirillzaslavsky/Documents/harness_labs/AGENTS.md:47-67`.
  - The architecture contract requires each new schema, receipt, recovery
    mechanism, abstraction, or telemetry stream to identify its production
    failure, consumer, and end-to-end assertion, and forbids supporting
    machinery from maturing ahead of the executable production path:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/architecture/harness-contract.md:145-156`.
  - The plan combines a package-copy/multi-document migration mechanism,
    normative-to-provider compiler, terminal taxonomy, assertion solver,
    capability broker, resolution-profile interpreter, dependency scheduler,
    multi-closure batching engine, gate engine, coordinator rollover protocol,
    and a large telemetry catalog:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:244-329,391-460,528-603,820-859`.
  - AC-6 expands the deterministic engine to “routine phase transitions,” not
    only the observed repair loop:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:101-112`.
- **Reasoning:** The claim that three sequential owner labels make the work
  bounded is unsupported. Group 3 in particular crosses from the adjudicated
  review-repair failure into a general phase engine and context framework. The
  package snapshot/recovery mechanism is also broad enough to become the
  forbidden generalized facility unless constrained to one executable
  production path. This is scope, not merely size: several mechanisms can be
  independently delivered and verified.
- **Concrete correction:** Split the work into bounded, independently
  certifiable features: (1) strict schema compilation/classification; (2)
  run-owned package identity plus one executable migration/recovery path; (3)
  executable repair-contract/capability gates; and only then (4) review-loop
  scheduling/rollover optimization. Limit the deterministic engine to
  `REVIEWING/fix` until a separate approved plan justifies other lifecycle
  phases. Give each feature its own production consumer, acceptance test, and
  diff bound.
- **Falsification condition:** This claim is falsified if the plan supplies
  per-mechanism complexity admission, bounded effort/path budgets, a single
  production consumer for each abstraction, and a staged sequence in which each
  group independently passes the required production lifecycle before the next
  framework layer is admitted.

### R1-008 — Coordinator hard limits are derived from certification data without the required authority

- **Severity:** high
- **Challenged plan section:** AC-6 lines 101-112; Group 3 lines 591-602; open
  assumption 6, lines 906-908.
- **Evidence:**
  - The plan mandates hard turn/context-slope limits but leaves their numeric
    values to be selected from certification data:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:101-108,591-602,906-908`.
  - The governing installed contract says performance observations cannot stop,
    block, retry, cancel, or change phase state unless an operator explicitly
    declares a separate hard limit:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/SKILL.md:116-119`.
  - The protocol is stricter: an exact numeric hard limit must be declared by
    the operator or a named safety contract and must not be inferred from a
    benchmark:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/references/protocol.md:42-50`.
- **Reasoning:** Certification measurements are observational; they do not have
  authority to create a new blocker. The plan also cannot test “exceeds limit”
  before the limit-selection rule exists. Selecting thresholds on the candidate
  being certified is circular and permits post-hoc tuning.
- **Concrete correction:** Either obtain and record explicit operator/named
  safety-contract authority with exact limits before implementation, or make
  rollover thresholds observational and non-blocking. If hard safety limits are
  authorized, freeze them in the planning input/package before certification and
  test exact boundary behavior.
- **Falsification condition:** This claim is falsified if an existing named
  safety contract or explicit operator authorization supplies exact numeric
  limits before certification and the plan binds implementation/tests to those
  values.

### R1-009 — Receipt-v2 compatibility is underspecified and conflicts with the current protocol discriminator

- **Severity:** medium
- **Challenged plan section:** Group 1 migration/backward compatibility,
  lines 331-343.
- **Evidence:**
  - The plan says readers accept missing fields as legacy receipt v1 and writers
    emit receipt v2, but does not specify a v2 protocol value or reader
    discriminator:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:331-343`.
  - The current schema fixes the protocol to
    `implement-v13-codex/process-receipt/1`:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/schemas/process-receipt.schema.json:1-9`.
  - `run_exec.py` emits that same v1 protocol at both receipt construction sites:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:482-530,795-840`.
  - The legacy synthetic consumer rejects any other process-receipt protocol:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_synthetic_flow.py:22-24,300-309`.
- **Reasoning:** Requiring new fields while retaining `/1` makes legacy versus
  new validity depend on field absence rather than an explicit version. Changing
  to `/2` breaks an existing consumer unless its compatibility behavior is also
  planned. The current wording supports neither a closed v2 schema nor a
  deterministic migration reader.
- **Concrete correction:** Specify the exact protocol/version strategy. Prefer a
  `/2` schema plus an explicit union reader for immutable `/1` receipts, and
  update every consumer/test. Alternatively keep `/1` and define a separately
  versioned extension envelope, but do not call it receipt v2.
- **Falsification condition:** This claim is falsified if the plan identifies an
  exact v2 discriminator, schemas for both versions, and all production/synthetic
  readers that accept immutable v1 and require the new fields for new writes.

### R1-010 — The response-schema certification inventory does not cover every production output-schema producer

- **Severity:** medium
- **Challenged plan section:** AC-1.3 lines 40-44; Group 1 contract/test language,
  lines 312-316 and 345-360.
- **Evidence:**
  - AC-1.3 says every canonical role-output schema must use the production
    preflight/binding path:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:40-44`.
  - The implementation text narrows the package enumeration to
    `schemas/*-result.schema.json`:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:312-316`.
  - Planner startup generates a task-bound output schema from
    `plan.schema.json`, so it is not a checked-in `*-result.schema.json`:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/start_planning.py:159-168,347-395`.
  - Plan reviewers use `plan-review.schema.json`, another non-`*-result` schema,
    and `run_exec.py` has a special production byte guard for it:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/references/protocol.md:244-248`;
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:300-320`.
- **Reasoning:** A filename glob is not the production dispatch registry. It can
  pass while planner, plan-review, coordinator, or dynamically bound schemas
  retain an unsupported provider shape. This contradicts AC-1.3's universal
  quantifier.
- **Concrete correction:** Build the certification inventory from the actual
  `run_exec` invocation roles/schema producers, including generated/bound
  schemas. Assert that every production role observed by the vertical slice has
  one compiled/preflighted transport schema and fail on an unregistered producer.
- **Falsification condition:** This claim is falsified if the package test
  derives its inventory from every production dispatch site and demonstrably
  includes planner, plan reviewer, coordinator, closure, implementation, review,
  UI, and Git role outputs after expected-field binding.

### R1-011 — Required event classes have neither a compatible schema mapping nor a writer in the declared scope

- **Severity:** medium
- **Challenged plan section:** `Observability requirements`, lines 820-859, and
  Group 1-3 exact write sets.
- **Evidence:**
  - The plan requires twenty new named event classes and says they use repository
    event schemas:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:820-859`.
  - The repository event schema has a closed top-level `event_type` enum that
    contains none of those names:
    `/Users/kirillzaslavsky/Documents/harness_labs/schemas/run-event.schema.json:1-37`.
  - The plan's own open assumption acknowledges that event location/schema
    alignment is unresolved:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:912-915`.
  - The exact write sets list neither the repository event schema nor a common
    append-only event writer:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:251-304,399-433,536-570`.
- **Reasoning:** The names might be payload classifications under a generic
  `phase_transition`, `verification`, or `retry` event, or they might be new
  top-level types. The plan does not decide. Without a writer, sequence owner,
  log path, and schema mapping, the observability acceptance condition is not
  implementable from the declared scope.
- **Concrete correction:** Define a single mapping from each proposed class to
  the existing `run-event.schema.json` envelope (prefer payload subtype unless a
  reviewed schema revision is necessary), identify the append-only writer and
  sequence authority, add those files to the exact write set, and validate the
  production vertical-slice log.
- **Falsification condition:** This claim is falsified if every proposed event
  class is mapped to an existing schema-valid envelope and an already scoped
  production writer with monotonic sequence/crash-safe append behavior is cited.

### R1-012 — The migration promises preservation of 124 historical ledger revisions that the current ledger does not store

- **Severity:** medium
- **Challenged plan section:** `SR-1`, lines 728-735.
- **Evidence:**
  - The plan says migration output will “preserve all 124 revisions' current
    durable content”:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-remediation-plan.md:728-735`.
  - The ledger implementation maintains one JSON document; every `_save`
    increments `state_revision` and atomically replaces that same path:
    `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:184-192,210-219`.
  - The adjudication establishes ledger revision 124 and embedded rejection/
    resolution history, not 124 retained document snapshots:
    `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-failure-analysis.md:482-490,512-520`.
- **Reasoning:** The current artifact can preserve its revision-124 bytes and
  embedded append-only histories, but it cannot reconstruct overwritten states
  for revisions 0-123 from a revision counter. The wording overstates available
  migration evidence and risks fabricating provenance.
- **Concrete correction:** Require byte preservation of the observed revision-124
  ledger plus every history entry actually embedded in it. Explicitly record
  prior revision snapshots as unavailable unless independently discovered and
  hash-verified; never synthesize them.
- **Falsification condition:** This claim is falsified if 124 independently
  retained, hash-verifiable ledger revision artifacts are cited and the
  migration consumes those artifacts rather than the counter.

## Explicit bounded-work assessment

The three-group plan **does violate** the repository's bounded-work and
no-generalized-framework rule as written. The immediate C1/C2 production failure,
the C14 package/migration safety prerequisite, and the later C3/C4/C13
optimization work are separable production changes. Combining them with a
general routine-phase engine, a package snapshot/recovery subsystem, a generic
resolution interpreter, a dependency scheduler, and a new telemetry catalog
does not become bounded merely because it is labeled as three sequential groups.
R1-007 gives the required correction. This assessment does not reject
purpose-built package identity or deterministic repair routing; it requires each
to be admitted and certified on its own production path.

## Explicit safe-resume/API assessment

The safe-resume procedure is **not executable through existing authority/state
APIs**. Existing APIs can (a) authorize queue resume, (b) reopen one blocked
checkpoint after that authorization, and (c) update one CAS-protected JSON file.
They cannot apply the proposed package migration across queue, dispatch,
checkpoint, transaction, and ledger, and the ledger lacks CAS. In addition, the
post-resume launch action is ambiguous between a fresh-planning entrypoint that
will reject the existing worktree and the direct recovery entrypoint. R1-001 and
R1-002 are release-blocking; no migration or live resume should occur until both
are corrected and crash-tested.

## Severity counts

- Critical: 2
- High: 6
- Medium: 4
- Low: 0
- Total: 12

## Machine-readable claim index

| claim_id | severity | release_blocking | challenged_lines | short_name |
|---|---|---:|---|---|
| R1-001 | critical | true | 759-778 | resumed launch selects fresh-planning path |
| R1-002 | critical | true | 742-757,798-811 | no executable atomic migration API |
| R1-003 | high | false | 747-752 | migration rewrites immutable dispatch |
| R1-004 | high | false | 56-68,268-285,322-329 | package snapshot omits runtime members |
| R1-005 | high | false | 114-126,663-688 | no successful production lifecycle gate |
| R1-006 | high | false | 87-99,572-590,618-647 | dependency completeness is circular |
| R1-007 | high | false | 7-14,216-661 | three-group scope is unbounded/generalized |
| R1-008 | high | false | 101-112,591-602,906-908 | hard limits lack authority |
| R1-009 | medium | false | 331-343 | receipt-v2 discriminator missing |
| R1-010 | medium | false | 40-44,312-316,345-360 | output-schema inventory incomplete |
| R1-011 | medium | false | 820-859 | event schema/writer edge missing |
| R1-012 | medium | false | 728-735 | unavailable historical ledger revisions |
