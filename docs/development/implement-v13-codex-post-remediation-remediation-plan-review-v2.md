# Adversarial review of implement-v13-codex remediation plan v2

Status: complete; source-bound; plan review only

## Scope and method

This review considers only the five phases already present in
`implement-v13-codex-post-remediation-remediation-plan-v2.md`: `F5`, `F3`,
`F4`, `F6`, and `N2`. It introduces no new failure mode, phase, remediation
concept, queue work, or E2E run. The paused feature run remains immutable
evidence.

The plan is directionally correct, but it is not implementation-ready. Five
critical corrections are required. The `N2` phase survives unchanged.

## Phase 1 — `F5`

### Critical F5-1 — the proposed command contract incorrectly makes the harness pytest-only

**Plan claim.** Phase 1 requires every new closure-test, assertion-map,
dependency-graph, and repair-batch command to start with the certified Python
interpreter and contain `-m pytest`
(`remediation-plan-v2.md:103-110`).

**Contrary source contract.**

- `references/repository-gates.md:3-10` requires the harness to discover and
  run the target repository's exact commands; it does not restrict repositories
  or gates to Python/pytest.
- `schemas/repair-dependency-graph.schema.json:67-94` deliberately models an
  immutable test command as a generic nonempty argv.
- `schemas/repair-batch.schema.json:55-67` preserves the selected generic argv.
- `review_closure.py:1412-1446`, `_selected_tests`, transports those argv values
  without adding a Python-only interpretation.
- Existing contract fixtures exercise both `python -m unittest` and
  `python -c`, not only pytest
  (`tests/test_review_closure.py:876-881` and `:962-967`;
  `tests/test_repair_gates.py:192-199`).

**Why critical.** Implementing the stated shape would reject valid exact
repository commands and would expand the narrow interpreter-binding repair
into a language/tooling policy change. The retained-v1 reader does not cure
new graph generation becoming Python-only.

**Required in-scope correction.** Keep the structured command object and its
runtime hash, but confine the certified-Python/`-m pytest` normalization rule to
the Python/pytest closure commands addressed by `F5`. Do not rewrite the
generic repository-gate command contract into a pytest-only contract. Preserve
generic argv for exact non-Python/non-pytest repository commands and preserve
the existing fail-closed treatment of unrecognized legacy shell strings. Add
an acceptance fixture proving that Phase 1 does not reject an existing exact
non-pytest dependency-graph argv solely because it is non-pytest.

### Critical F5-2 — the static plane is omitted from the exact targets

**Plan claim.** Phase 1 updates three reference files, and Phase 2 updates two
reference files (`remediation-plan-v2.md:85-89`, `:187-190`).

**Contrary source contract.**

- `SKILL.md:158-170` is the static instruction plane that tells the coordinator
  what an exact closure command and production capability manifest prove.
- `SKILL.md:185-188` still mandates the old ordered
  `production-real sandbox` and separate `dependency-mapped regression` gates.
- The proposed Phase 2 replaces those gates with `capability_manifest` and
  `production_certification` (`remediation-plan-v2.md:201-208`).

**Why critical.** The coordinator is explicitly told to read `SKILL.md`
(`run_feature.py:378-405`). Leaving the old gate authority and command wording
there would preserve the static/dynamic contradiction after the source change.

**Required in-scope correction.** Add only the corresponding closure-command,
capability, and ordered repair-gate paragraphs in `SKILL.md` to the Phase 1/2
target lists and synchronize them to the already-proposed structured command
and controller-owned production-certification contracts. No other `SKILL.md`
content is in scope.

## Phase 2 — `F3`

Subject to F5-1 and F5-2, the controller-owned execution boundary is sound:
`closure_driver.py:430-469` already calls `run_repair_gates` outside a model
invocation, while `repair_gates.py:179-265` currently demonstrates the exact
defect by validating a manifest and then running the regression argv ambiently.
The proposed receipt bindings and the negative simulated-manifest test are
necessary and sufficient for the narrowed `F3` contract. No additional phase
or executor is warranted.

## Phase 3 — `F4`

### Critical F4-1 — protocol migration and unknown rollover representation are underspecified

**Plan claim.** Every terminal receipt gains required `provider_usage`,
historical v1/v2 receipts remain readable, the coordinator protocol advances,
and `coordinator-rollover/1` records authoritative values including unknown
usage (`remediation-plan-v2.md:290-314`).

**Contrary source contract.**

- Fresh receipts are currently protocol 2
  (`run_exec.py:41`, `PROCESS_RECEIPT_PROTOCOL`), and
  `process-receipt.schema.json:6,66-70` defines protocol-2 required fields.
  Adding a required field to protocol 2 while also claiming old v2 receipts are
  valid is not a compatible contract.
- Coordinator output is protocol 1
  (`run_feature.py:43`; `feature-coordinator-result.schema.json:5-8`).
- Rollover is protocol 1 (`run_feature.py:44`;
  `coordinator-rollover.schema.json:23-26`) and its telemetry requires only
  nonnegative integer token totals and slopes
  (`coordinator-rollover.schema.json:49-64`). It cannot represent unknown.
- `_write_rollover_summary` calls `max`, `min`, and `sum` over integers
  (`run_feature.py:235-265`), so “fail closed before arithmetic” needs an exact
  precondition and durable outcome rather than an unspecified nullable value.

**Why critical.** A direct schema mutation either invalidates retained v2/v1
artifacts or silently changes the meaning of existing protocol identifiers.
The proposed unknown state cannot pass the current rollover schema, and its
arithmetic/block point is not deterministic enough to implement consistently
across fresh and recovery paths.

**Required in-scope correction.**

1. Name the version transitions explicitly: new process receipts use a new
   protocol version while v1/v2 readers remain hash-checking legacy readers;
   the model-owned coordinator result advances to a new protocol; and any
   changed rollover telemetry shape uses a new rollover protocol rather than
   changing `/1` semantics.
2. Define the closed rollover representation already implied by the plan:
   recorded usage has integer totals/slope; unknown usage has explicit unknown
   status and null token aggregates/slope. Before `_write_rollover_summary`
   performs `sum`/`min`/`max`, any configured limit plus unknown usage must
   produce the existing scoped coordinator-limit blocker. With no configured
   limit, receipts/recovery retain unknown without manufacturing a zero.
3. Make the “model zeroes cannot override” test precise: under the advanced
   closed coordinator schema, model-owned token fields are rejected, while
   provider receipt values remain authoritative.

These are corrections to the existing provider-usage and unknown-state design,
not new accounting policy.

## Phase 4 — `F6`

### Critical F6-1 — an interruption marker is not termination proof

**Plan claim.** The interruption finalizer never signals after a fingerprint
mismatch, but still terminalizes the receipt; reconciliation accepts a terminal
receipt with an interruption marker and returns the closure to
`ready_for_fix` (`remediation-plan-v2.md:373-408`).

**Contrary source contract.**

- `_terminate_owned_group` returns `False` when PID/PGID/fingerprint identity
  cannot be proven (`run_exec.py:550-570`).
- `_process_matches` likewise treats missing or changed identity as not owned
  (`run_exec.py:573-581`).
- `start_attempt` permits a new launch whenever the closure returns to
  `ready_for_fix` (`review_closure.py:1302-1334`).

**Why critical.** If fingerprint verification fails, the controller has no
proof that the old descendant is gone. Treating that receipt as reconcilable
would authorize a duplicate fixer launch—the exact safety outcome Phase 4 is
supposed to prevent.

**Required in-scope correction.** The interrupted receipt must distinguish
verified owned-group termination/reaping from unverified identity. Only an
interruption receipt with positive termination/reap proof may transition an
attempt to `interrupted` and `ready_for_fix`. Fingerprint mismatch or
unverified cleanup must terminalize durable process state as unreconciled and
leave closure recovery deterministically blocked. Extend the existing
fingerprint-mismatch acceptance test to prove that no ledger reconciliation or
new launch is authorized.

### Critical F6-2 — reconciliation must preserve every member of an existing repair batch

**Plan claim.** At entry, reconcile only the active closure's latest attempt
and return `retry_fix` (`remediation-plan-v2.md:395-408`).

**Contrary source contract.**

- One fixer invocation is recorded as a running attempt for every member of a
  repair batch (`closure_driver.py:401-422`).
- The same successful result later finishes every member
  (`closure_driver.py:422-428`).
- `start_attempt` rejects any member not in `ready_for_fix`
  (`review_closure.py:1302-1306`).

**Why critical.** Reconciling only the active member leaves secondary batch
members durably `fix_running`; the next batch cannot start and attempt history
diverges for a single shared invocation.

**Required in-scope correction.** Reconcile atomically all ledger closures
whose latest running attempt has the same exact invocation/receipt ID as the
interrupted batch fixer, preserving order and history for each. Any partial,
mismatched, or ambiguous member set remains blocked. Add a two-member batch
acceptance case proving both attempts become `interrupted`, neither is counted
as rejected, and no duplicate launch occurs.

## Phase 5 — `N2`

The phase is sufficient as written. `continue_without_bound_program` currently
admits exactly `next_ready`, `retry_fix`, and `redesign`
(`closure_driver.py:108-146`), while the two named normative paragraphs retain
the contradictory blocker text (`references/protocol.md:160-163`;
`references/phase-contracts.md:125-130`). Exposing that existing set as one
constant and conformance-checking only those paragraphs does not change
runtime semantics or broaden scope.

## Certification correction

The certification command is not yet executable as specified. It requires
`capability-manifest.certification_runtime.interpreter_path`
(`remediation-plan-v2.md:486-508`), but the only current manifest schema is
`capability-manifest/1` without that field
(`schemas/capability-manifest.schema.json:5-23`), and the paused run is
explicitly immutable. The final plan must state how a fresh real
source-certification manifest is generated and hash-validated before those six
commands; it may not read the field from the paused legacy manifest or fall
back to ambient `python3`. This is an execution detail inside the existing
bounded certification section, not a new phase.

## Required disposition

Revise the existing plan only as follows:

1. correct Phase 1's Python-only overreach and include the exact static-plane
   paragraphs;
2. make Phase 3's protocol versions and unknown representation explicit;
3. require positive termination proof and batch-complete reconciliation in
   Phase 4;
4. make the existing certification section self-contained by minting a fresh
   real manifest.

No correction is required to the `N2` phase. No rejected item (`F8` or `N1`)
is reopened.
