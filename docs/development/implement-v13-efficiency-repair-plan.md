# Implement v13 Efficiency Repair Plan

Status: implemented and certified
Date: 2026-07-22
Evidence run: `fr_fab27f53b373470d952a176658602e87`

## Outcome

Remove the demonstrated orchestration waste without weakening durable recovery,
independent review, adversarial closure tests, or final certification. The repair
targets three measured failure classes:

1. post-execution role-result rejection and sandbox-caused repair retries;
2. a fresh model coordinator turn after nearly every child result; and
3. one oversized implementation assignment spanning 41 changed paths.

The installed `implement-v13-codex` skill is the production consumer. Harness
Labs records the plan and acceptance evidence; production changes are validated
against the skill's existing component and production-entrypoint tests.

## Evidence and root causes

### Repair-designer retries

Two architectural closure groups launched eight repair designers. Four calls
were required initial/revised designs; four were preventable retries. One
read-only designer could not persist a required artifact, and three useful
outputs failed only because model-authored `phase`, `phase_detail`, or `role`
values differed from the invocation identity.

### Coordinator amplification

The production controller launched 30 fresh coordinator threads. The controller
passes no resume identity, asks every turn to reread the skill and references,
and returns to a model after each brokered child. These turns consumed about 96
minutes and 26.4 million input-token reads. Most closure transitions were already
deterministically defined by the checkpoint and closure ledger.

### Oversized implementation assignment

The validated plan marked all six steps as one linear critical path and supplied
no worker groups. The default-one-worker policy therefore assigned backend
storage, coordination, routes, UI, documentation, live-walk assets, and tests to
one Luna-high worker. The worker changed 41 paths and executed 102 commands.

## Implementation slices

### 1. Bind child identity before execution

- Compile scalar `spec.expected` values into the frozen attempt schema as typed
  top-level `const` constraints.
- Preserve the original schema path and hash separately from the bound attempt
  schema and continue terminal semantic revalidation.
- Reject an expected field absent from the output schema before launch.
- Prove a model cannot spend a full call producing a noncanonical role or phase.

### 2. Make architectural design output controller-owned

- Add one canonical repair-design result schema containing the strategy family,
  design invariants, changes, tests, related closures, and explicit exclusions.
- Require architectural designers to remain source-read-only and return the
  complete design through their schema-bound final output.
- Use the process output itself as the immutable design artifact; the controller
  records its path and hash in the closure ledger.
- Reject a design spec that requests workspace-write or uses a noncanonical
  schema before launch.

### 3. Drive routine closure transitions deterministically

- Add a controller-owned closure driver that maps ledger state to canonical test
  author, designer, design reviewer, first fixer, and targeted-review specs.
- Apply successful child results to the ledger without a coordinator model turn.
- Keep originating-reviewer independence, design approval, complete attempt
  history, one-group-at-a-time fixing, and regression checks intact.
- Return to a model coordinator only for ambiguous strategy changes, escalation,
  decomposition, or final integration judgment.
- Fingerprint the feature tree around workspace-write targeted reviewers and
  reject any source mutation.

### 4. Partition implementation work even when dependencies are sequential

- Derive at most three ordered worker groups from the validated DAG.
- Bound each group by effort and write-path count; require an explicit recorded
  exception when the bounds cannot be met.
- Preserve dependency order and disjoint ownership. Parallel dispatch remains
  limited to dependency-free groups; sequential groups still receive separate
  context packets and targeted tests.
- Validate every implementation-worker spec against the derived manifest.

### 5. Synchronize contracts and certification

- Update the skill and phase contracts to distinguish model judgment from
  deterministic state advancement.
- Add focused tests for schema binding, design sandbox/schema enforcement,
  closure-driver progression, dependency-aware closure ordering, worker
  partitioning, and unchanged independent-review invariants.
- Run the existing production vertical-slice, feature-controller, process-runner,
  closure-ledger, schema, and full skill test suites.

## Acceptance criteria

1. Wrong role/phase identity is schema-impossible before a child starts; terminal
   semantic validation remains enabled.
2. A repair designer can complete in read-only mode without writing a secondary
   artifact.
3. The normal first-attempt architectural closure path requires no model
   coordinator between test, design, approval, fix, and targeted review.
4. No targeted reviewer may change the feature tree even when test scratch space
   requires workspace-write.
5. A plan comparable to Q12 produces multiple bounded sequential worker groups
   rather than one 41-path assignment.
6. Existing durable receipts, crash recovery, originating-reviewer separation,
   regression reopening, and terminal queue settlement tests remain green.
7. Runtime metrics record coordinator turns avoided, deterministic transitions,
   preventable retry classifications, and worker-group sizes.

## Non-goals

- Removing substantive architectural design review.
- Parallelizing overlapping writers.
- Weakening required closure or regression tests.
- Replacing the full feature lifecycle with the generic debug phase-flow runner.
- Generalizing unrelated snapshot or scheduling frameworks.

## Executed changes

The production skill at
`<user-home>/.codex/skills/implement-v13-codex` now provides:

- typed schema binding for every scalar `spec.expected` field before process
  creation, while retaining separate source-schema and attempt-schema hashes;
- canonical read-only repair-design output recorded directly in the closure
  ledger as the immutable result artifact;
- dependency-aware closure groups plus one controller-owned closure program for
  the normal test -> design -> approval -> fix -> targeted-review route;
- feature-tree fingerprinting around any workspace-write reviewer, with a
  structured `reviewer_tree_mutation` failure class;
- one resumed coordinator thread after bootstrap, with a compact continuation
  prompt and thread-identity continuity enforcement; and
- deterministic one-to-three implementation groups bounded to effort 12 and 18
  write paths, with explicit exceptions if the three-group ceiling forces an
  overage and exact worker-spec-to-group validation.

The closure-program result records deterministic transition and avoided
coordinator-turn counts. Child failures classify identity/schema preflight,
reviewer mutation, and ordinary invocation failures. The implementation
partition records effort, write-path count, dependencies, and exceptions for
every worker group.

## Certification evidence

- Full installed-skill suite: `116` tests passed on 2026-07-22.
- All installed JSON schemas passed Draft 2020-12 schema checking.
- Skill package validation: `Skill is valid!`.
- The exact Q12 `plan.v1.json` deterministically produces three sequential
  groups with efforts `6`, `12`, and `12`; write-path counts `12`, `15`, and
  `15`; and no bound exceptions. This replaces the observed single 41-path
  assignment without pretending its dependent groups can run concurrently.

The failed historical Q12 run was not resumed or mutated during certification.
The next feature run will exercise the new controller path in production; the
historical receipts remain immutable evidence of the prior behavior.
