# Parallel PlanGraph execution and serial-baseline replay plan

Status: proposed
Date: 2026-08-09

## Objective

Extend audited PlanGraph execution from one linear candidate chain to a bounded
dependency scheduler that can launch ready sibling FeatureRuns from the same
immutable parent, preserve their isolated candidates, and join those candidates
at an explicit downstream node. Prove the behavior by replaying the Retinology
Flow Node Mockup Parity graph from exactly the same starting point as its
overnight serial run.

This work does not change the accepted product plan. It changes how an already
accepted decomposition is executed and audited.

## Preserved Retinology baseline

The executable decomposition is committed byte-for-byte at
`tests/fixtures/plan_graph/retinology-flow-node-mockup-parity-baseline.json`.
Its SHA-256 is
`7c92bf45ccfa94dee75ab145fbc004882daaa5c8db7da1bf8062bf7844f8fca3`.

Source identity at capture:

- repository:
  `/Users/kirillzaslavsky/Documents/retinology-clinician-first-ui-20260809t220000z`;
- immutable execution base:
  `47931ebc9e5993a0dd7babd00350a766b1977601`;
- intended base branch: `codex/flow-node-mockup-parity`;
- intended graph staging branch:
  `codex/flow-node-mockup-parity-plangraph-completion`;
- external decomposition SHA-256:
  `7c92bf45ccfa94dee75ab145fbc004882daaa5c8db7da1bf8062bf7844f8fca3`;
- external narrative PlanGraph SHA-256:
  `a953ee9642946e17563771d91d815bc25b205c3a1c6194d06f15ef3d0997cb7f`;
- external completion-plan SHA-256 at capture:
  `7ed79f58338cfbe4b39ae186a958ad9fb276c20f6ddd73b66ea20b10750d01ac`;
- completion plan at immutable Git base SHA-256:
  `79f89f9bcfd08dc32670c01377182cdb80e3d85cc720eee13b2365f7905fbc8b`.

The differing completion-plan hashes are intentional evidence that the accepted
working plan contained uncommitted changes at capture. Before either execution,
the operator must place the accepted plan bytes on an immutable graph branch or
provide them as a content-addressed launch artifact. A path plus a dirty working
tree is not sufficient replay authority.

The later parallel replay must use the preserved decomposition and
`47931ebc...` again. It must not use the overnight serial result as its base.
The two runs need distinct graph IDs, branches, run directories, and worktrees.
Retain the complete serial audit directory even if the run fails.

## Required semantics

### Lifecycle ownership

- Standalone FeatureRuns retain the full
  `orient -> plan -> implement -> verify -> review -> integrate -> report`
  lifecycle.
- PlanGraph-child FeatureRuns bind an approved scope and execute
  `bind -> implement -> verify -> review/repair -> commit -> integrate -> report`.
- A child integration targets its graph-owned lane or staging transaction. The
  PlanGraph owns the final graph-staging-to-base integration.

### Dependency frontier

Replace the single mutable `candidate_commit` loop with a durable ready frontier:

1. A node is ready only when every declared dependency has a sealed successful
   candidate.
2. All nodes admitted in one parallel batch are reserved before any launch.
3. Siblings with the same dependency set receive the same parent candidate.
4. The scheduler launches at most the configured graph parallelism.
5. Completion order never changes node identity, dependency identity, or the
   deterministic presentation order.
6. Failure uses a declared policy. The initial policy is `collect_all` for an
   already-started batch and blocks every unstarted descendant of a failed node.

Reuse the existing bounded parallel scheduler and fresh-executor patterns where
their contracts fit. Do not create a second general thread-pool abstraction.

### Candidate and join contracts

Extend the decomposition and runtime contracts so topology is executable rather
than ignored:

- preserve `execution_policy`, `parallel_groups`, and each `parallel_group`;
- add a graph-level `max_parallelism` with a bounded default;
- give each request an immutable `parent_candidate_commit`;
- give join nodes a complete `dependency_candidates` mapping;
- identify the graph staging branch and each lane branch explicitly;
- require writable-path grants before dispatch;
- reject a join if a dependency candidate is missing, unverified, duplicated,
  or not descended from its recorded parent;
- require the join result to contain every dependency candidate in its Git
  ancestry or in a content-equivalent, audited semantic resolution.

For the Retinology fixture the required topology is:

```text
47931eb -> FR-00 -> C00
                     |-- FR-10(C00) -> C10 --|
                     |-- FR-11(C00) -> C11 --|-> FR-20(C00,{C10,C11,C12}) -> C20
                     |-- FR-12(C00) -> C12 --|                         |
                                                                         FR-30 -> C30
```

`FR-10`, `FR-11`, and `FR-12` must overlap in time and must all record `C00` as
their parent. `FR-20` is the only node authorized to join those three lanes into
the graph staging history.

### Git custody

Every child starts in its own branch and worktree. A parallel sibling seals its
candidate on its lane branch; it does not race to advance the shared staging
branch. The join node performs the ordered integration under one graph-owned
lease and expected-head check. Record:

- source branch, worktree, parent commit, and candidate commit for each lane;
- integration lease acquisition and release;
- expected and observed staging heads;
- merge order and merge commits;
- conflict paths, classification, resolution actor, and verification evidence;
- final staging head and final base-branch merge receipt.

Never force-push or silently select `ours`/`theirs`. A semantic conflict blocks
the join until an authorized repair produces new evidence.

### Audit and recovery

The PlanGraph journal is the scheduling authority. Add events for batch
reservation, child launch, candidate sealing, descendant blocking, join start,
each dependency integration, join verification, and staging advancement.
Checkpoint the ready/running/sealed/blocked sets, dependency-candidate map,
active batch, lane identities, integration lease, and staging head.

Resume must reconcile the journal before launching work. It may adopt a child
result only when the child descriptor, parent correlation, parent commit,
candidate receipt, and terminal journal all agree. An interrupted join resumes
from its last verified merge receipt or restarts in a new integration worktree;
it never guesses from the current branch head.

### Verification environment

The default graph functionality runner currently checks out a detached clone.
Retinology commands reference `.venv/bin/...`, which is not present in a fresh
clone. Add an explicit verification-environment contract: either provision from
a locked dependency specification, use a declared reusable read-only runtime,
or invoke an injected runner that records its environment identity. Missing
environment prerequisites must block before the overnight-scale test suite.

## Implementation slices

1. **Contract and fixture admission**
   - Parse and validate execution policy, parallel groups, writable paths, and
     maximum parallelism.
   - Make the preserved Retinology fixture a contract test.
   - Reject unsupported parallel input rather than silently serializing it.
2. **Durable frontier scheduler**
   - Compute ready sets from dependency completion.
   - Reserve batches atomically and dispatch with the existing bounded scheduler.
   - Persist deterministic identities and recovery state.
3. **Lane custody and child mode**
   - Create siblings from one verified parent commit.
   - Add the PlanGraph-child lifecycle and lane candidate receipts.
   - Keep standalone FeatureRun behavior unchanged.
4. **Join transaction**
   - Pass dependency candidates to join nodes.
   - Add graph-owned serialized integration, conflict evidence, expected-head
     advancement, and combined verification.
5. **Final integration and observability**
   - Verify the completed staging candidate and merge it to the declared base.
   - Expose topology, live parallelism, lane candidates, join state, and metrics
     in the dashboard without inferring missing relationships.
6. **Serial-versus-parallel replay**
   - Launch a fresh parallel run from the preserved base and input.
   - Compare acceptance outcomes, candidate tree, test evidence, elapsed time,
     agent time, token use, retries, and integration latency.
   - Treat the comparison as valid only if both runs use equivalent plan bytes,
     environment, criteria, and final gates.

## Acceptance criteria

1. The preserved fixture remains byte-identical to the captured external JSON.
2. Unsupported parallel fields fail admission on the old sequential path; they
   are never silently ignored.
3. Independent siblings demonstrably overlap while respecting the configured
   cap and starting from the same parent commit.
4. Join requests contain the exact dependency candidate map and cannot run with
   partial or unverified inputs.
5. Parallel children cannot mutate or race the shared staging head.
6. Resume after interruption does not relaunch sealed children or duplicate an
   accepted integration.
7. Standalone FeatureRuns retain orient and plan; PlanGraph children do not run
   open-ended orient or plan agents.
8. The Retinology parallel replay starts from `47931ebc...`, executes the true
   three-lane fork and explicit `FR-20` join, and passes the same final gates as
   the serial baseline.
9. The dashboard shows declared dependencies, actual overlap, candidate lineage,
   join evidence, and serial-versus-parallel efficiency without fabricating
   unavailable data.

## Stop conditions

Stop rather than launch or integrate when source-plan hashes differ, the base
branch has advanced without adjudication, a writable-path grant overlaps an
active sibling, the verification environment is not reproducible, dependency
lineage is incomplete, a candidate lacks audited verification, an integration
head changed unexpectedly, or a semantic conflict remains unresolved.
