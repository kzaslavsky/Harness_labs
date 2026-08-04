# Implement-v13-codex post-remediation remediation plan final v2

Status: final; source-bound; implementation not started

## Authority and bounded scope

Implement exactly the five actionable items accepted by
`implement-v13-codex-post-remediation-failure-adjudication-v2.md`, in this
order:

1. `F5` — bind closure-test commands without making repository gates
   Python- or pytest-only.
2. `F3` — execute exact certification commands through the controller-owned
   host broker.
3. `F4` — make provider terminal usage authoritative.
4. `F6` — finalize interruption state and reconcile the complete repair batch.
5. `N2` — synchronize the legal unbound-route normative text.

The implementation root is `skills/codex/implement-v13-codex`. The paused run
`fr_0a8feb07a847488ea910a0ec5a2a99d7` remains immutable evidence: do not resume,
rewrite, or use it as a source-certification manifest. No queue/E2E work,
general executor or migration framework, unrelated refactor, new defect or
phase, `F8`, or `N1` is authorized.

## Protocol and compatibility rules

New writes use these explicit versions; readers retain hash-checking support
for the listed legacy versions without rewriting old artifacts:

| Artifact | New protocol | Retained read compatibility |
| --- | --- | --- |
| Capability manifest | `implement-v13-codex/capability-manifest/2` | `/1` is legacy evidence only and cannot authorize the new certification gate. |
| Test command | `implement-v13-codex/test-command/1` | Legacy command strings are handled only by the bounded normalizer below. |
| Closure-test result and dependent command-bearing artifacts | Advance each current protocol by one version for the structured command shape. | Existing v1 artifacts remain readable through explicit v1 readers. |
| Process receipt | `implement-v13-codex/process-receipt/3` | `/1` and `/2` remain hash-checked legacy readers; usage is derived from their bound stdout and may be `unknown`. |
| Feature-coordinator result | `implement-v13-codex/feature-coordinator-result/2` | `/1` remains readable for recovery but its model-authored token fields never become accounting authority. |
| Coordinator rollover | `implement-v13-codex/coordinator-rollover/2` | `/1` retains its integer-only meaning and is not mutated to represent unknown usage. |

All schemas remain closed with `additionalProperties=false`. Version dispatch
must occur before shape validation. Any incompatible or ambiguous artifact
fails closed before a model launch, gate authorization, ledger mutation, or
token arithmetic.

## Phase 1 — structured commands and certified Python binding (`F5`)

### Exact targets

- `SKILL.md`: only the exact-closure-command and capability paragraphs.
- `scripts/repair_preflight.py`:
  `probe_role_capabilities`, `validate_capability_manifest`, and one local
  certification-runtime identity helper.
- `schemas/capability-manifest.schema.json`
- `schemas/closure-test-result.schema.json`
- `schemas/repair-assertion-map.schema.json`
- `schemas/repair-dependency-graph.schema.json`
- `schemas/repair-batch.schema.json`
- `scripts/review_closure.py`:
  `record_test`, the `validate_assertion_effects` boundary,
  `backfill_assertion_map`, `_load_dependency_graph`, `_selected_tests`, and
  `validate_invocation_spec`.
- `tests/test_review_closure.py`
- `tests/test_repair_gates.py`
- Only the closure-command/capability/repository-gate paragraphs in
  `references/protocol.md`, `references/phase-contracts.md`, and
  `references/repository-gates.md`.

### Contract and implementation

1. A fresh production-real capability manifest `/2` contains a closed
   `certification_runtime` with canonical absolute interpreter path,
   executable SHA-256, exact Python version, pytest version and resolved module
   path, plus a canonical dependency fingerprint over those fields. Probe the
   dependency facts with that same absolute interpreter. Simulation manifests
   cannot authorize production gates.
2. New command-bearing artifacts use a closed `test-command/1` object with
   nonempty generic `argv`, the capability runtime hash, and the fixed
   controller-owned environment profile. Preserve command bytes and immutable
   test-node/source hashes through assertion map, dependency graph, selected
   batch, and gate input.
3. Apply certified-Python validation only when the command is a Python/pytest
   closure command: `argv[0]` must equal the manifest's absolute interpreter
   and the entry must be `-m pytest`. Exact non-Python and non-pytest repository
   argv remain generic and byte-preserved; they are not rejected or rewritten
   merely because they are not pytest. Existing executable and repository-gate
   validation still applies.
4. The legacy string normalizer accepts only optional known `TMPDIR` and
   `PYTHONDONTWRITEBYTECODE` assignments followed by `python3 -m pytest` or
   `pytest`, parsed with `shlex.split`. It replaces only that prefix with the
   certified absolute interpreter and `-m pytest`. Shell operators,
   substitutions, redirections, unknown executables, and all other unpinned
   legacy strings are classified `legacy_unpinned` and block before model
   launch.
5. Validate the manifest, runtime hash, command shape, and complete downstream
   propagation before one atomic ledger/assertion-map write. A validation
   failure leaves both unchanged. Never fall back to ambient `python3`.
6. Synchronize the named `SKILL.md` and reference paragraphs to this structured
   generic-command contract; do not state or imply that all repository gates
   are Python/pytest.

### Deterministic tests

- Reject a relative Python/pytest interpreter or mismatched runtime hash before
  ledger mutation.
- Prove identical command bytes/runtime hash survive closure result, assertion
  map, dependency graph, repair batch, and gate input.
- Normalize the observed
  `TMPDIR=/private/tmp PYTHONDONTWRITEBYTECODE=1 python3 -m pytest ...` form
  while preserving arguments.
- Classify `|`, `>`, `$()`, or an unknown legacy executable as
  `legacy_unpinned`; prove no model call occurs.
- Preserve an existing exact non-pytest dependency-graph argv, including
  `python -m unittest` or `python -c`, without rejection solely for being
  non-pytest.
- Meta-validate each changed schema and validate one new artifact plus one
  retained legacy fixture.

## Phase 2 — controller-owned production certification (`F3`)

### Exact targets

- `SKILL.md`: only the capability and ordered repair-gate paragraphs.
- `scripts/repair_preflight.py`:
  `_sandbox_probe_command`, `probe_role_capabilities`, and
  `validate_capability_manifest`.
- `schemas/capability-manifest.schema.json`
- `scripts/repair_gates.py`:
  `GATE_ORDER`, `_production_sandbox`, `_dependency_regression`,
  `run_repair_gates`, and one module-local host-certification helper.
- `scripts/review_closure.py::_validated_gate_receipt`
- `scripts/closure_driver.py::run_closure_program` at the existing
  `run_repair_gates` boundary.
- New bounded schemas:
  `schemas/repair-gate-input.schema.json` and
  `schemas/repair-gate-receipt.schema.json`.
- `tests/test_repair_gates.py`
- `tests/test_closure_driver.py`
- Only the production-capability and ordered-gate paragraphs in
  `references/protocol.md` and `references/phase-contracts.md`.

### Contract and implementation

1. The capability manifest proves host availability only. It binds the broker
   absolute path and executable SHA-256, probe-policy SHA-256, and Phase 1
   runtime hash; it does not emit or consume
   `same_broker_as_production` as per-invocation authority.
2. Keep `run_repair_gates` at its existing controller-owned call site outside
   all model invocations. Replace the manifest-only sandbox claim plus ambient
   regression run with ordered `capability_manifest` and
   `production_certification` gates.
3. For each selected `test-command/1`, the local helper constructs the
   controller-owned repository-read/scratch-write Seatbelt policy and executes
   exactly `[broker, "-p", policy, *argv]` once. Generic repository argv remain
   exact; Python/pytest argv retain the Phase 1 certified interpreter.
4. The controller alone selects broker, policy, repository root, private
   scratch, and environment profile. Hash policy before launch; retain existing
   scratch containment, then hash and remove scratch at terminal.
5. Atomically write a gate receipt binding selected node, command bytes/hash,
   broker path/hash, policy hash, runtime hash, cwd/repository identity, return
   code, stdout/stderr hashes, scratch hash, and removal proof. A missing or
   nonzero command result fails the gate and suppresses targeted review.
6. `_validated_gate_receipt` accepts only the exact gate order
   `forbidden_access`, `pre_communication_output_bound`, `process_evidence`,
   `capability_manifest`, `production_certification`, with a valid receipt
   path/hash. Read-only reviewers consume that receipt and never rerun
   Seatbelt.
7. Synchronize only the named `SKILL.md` and reference paragraphs to the new
   gate names and authority boundary.

### Deterministic tests

- Observe exactly one broker-wrapped invocation per selected command and no
  ambient duplicate.
- Reject manifest-only success, simulation manifests, or changed
  broker/policy/runtime/command evidence before targeted review.
- Assert all receipt bindings, stdout/stderr hashes, scratch hash, and scratch
  removal.
- Prove passed certification reaches targeted review by receipt path/hash;
  failed certification records deterministic failure and makes no reviewer
  call.
- Prove the reviewer receives no instruction to execute `sandbox-exec`.

## Phase 3 — provider-owned coordinator usage (`F4`)

### Exact targets

- `scripts/run_exec.py`:
  `PROCESS_RECEIPT_PROTOCOL`, `_read_events`,
  `_terminal_validation_errors`, `_finalize_receipt`,
  `_assert_receipt_matches`, and one `_provider_usage` extractor.
- `schemas/process-receipt.schema.json`
- `schemas/feature-coordinator-result.schema.json`
- `scripts/run_feature.py`:
  `COORDINATOR_PROTOCOL`, `COORDINATOR_ROLLOVER_PROTOCOL`,
  `_invoke_real`, `_recover_coordinator_position`,
  `_write_rollover_summary`, `drive`, and one shared receipt-usage reader.
- `schemas/coordinator-rollover.schema.json`
- `tests/test_run_exec.py`
- `tests/test_run_feature.py`
- `tests/test_production_vertical_slice.py` only where its stub emits the
  coordinator protocol.
- Only coordinator-accounting and process-receipt paragraphs in
  `references/protocol.md`.

### Contract and implementation

1. New process receipts `/3` persist a closed `provider_usage`:
   `status="recorded"` with nonnegative integer input, cached-input, and output
   tokens, or `status="unknown"` with all three values null. Extract the last
   terminal `turn.completed.usage`; reject malformed, Boolean, negative, or
   conflicting terminal usage.
2. Receipt stdout hash is authority. Revalidation recomputes usage from that
   stdout. Legacy `/1` and `/2` readers derive usage from their hash-bound
   stdout without mutation; absent usage becomes `unknown`, never zero.
3. Feature-coordinator result `/2` removes all model-owned token fields. Its
   optional metrics may retain only `coordinator_turns_avoided`. Because the
   schema is closed, a model result containing token fields, including zeroes,
   is rejected. Legacy `/1` token fields are ignored for accounting.
4. Fresh and recovery paths obtain the contiguous usage series only from
   process receipts. `_invoke_real` returns receipt-derived usage with output
   and thread identity.
5. Coordinator rollover `/2` uses a closed union:
   - `status="recorded"` with integer token aggregates and integer slope; or
   - `status="unknown"` with null aggregates and null slope.
   Do not alter rollover `/1` semantics.
6. Before `_write_rollover_summary` performs `sum`, `min`, or `max`, check the
   complete series. If any usage is unknown and an exact operator- or
   named-safety-authorized limit is configured, atomically persist the existing
   scoped coordinator-limit blocker and perform no arithmetic. With no
   configured limit, persist rollover `/2` as unknown and continue. Leave
   `_coordinator_limits` authority rules unchanged.

### Deterministic tests

- Persist and revalidate exact nonzero provider values; persist missing usage
  as explicit unknown/nulls.
- Reject malformed/conflicting usage and receipt values inconsistent with
  hash-bound stdout.
- Under coordinator result `/2`, reject model-owned token fields; prove model
  zeroes cannot override receipt values.
- Recover the same nonzero series from contiguous receipts.
- Prove unknown plus no limit remains unknown; unknown plus an authorized limit
  writes the existing blocker before arithmetic.
- Prove rollover `/2` recorded slope/aggregates use receipt values exactly and
  legacy `/1` remains integer-only.

## Phase 4 — interruption-safe process and batch recovery (`F6`)

### Exact targets

- `scripts/run_exec.py`:
  `_terminate_owned_group`, the fresh `Popen` polling lifecycle, the
  nonterminal-receipt recovery branch, and one local interrupted-receipt
  finalizer.
- `scripts/supervised_child.py::main`
- `schemas/process-receipt.schema.json`
- `scripts/review_closure.py`:
  `start_attempt`, `finish_attempt`, `attempt_history_sha256`, and one atomic
  `reconcile_interrupted_attempts` transition.
- `scripts/closure_driver.py::run_closure_program`
- `schemas/review-closure-ledger.schema.json`
- `tests/test_run_exec.py`
- `tests/test_review_closure.py`
- `tests/test_closure_driver.py`
- Only the process-receipt/recovery paragraph in `references/protocol.md` and
  closure-attempt paragraph in `references/phase-contracts.md`.

### Contract and implementation

1. Wrap only the owned fresh-child poll/wait region and equivalent live-recovery
   wait in `try/except BaseException`. On interruption, verify PID, PGID, and
   process-start fingerprint; use the existing bounded TERM-then-KILL helper;
   wait and reap; capture or parent-write the exit record; hash available
   stdout, stderr, child spec, exit evidence, and scratch; remove scratch;
   atomically terminalize the receipt; then re-raise the original exception.
2. The terminal interruption evidence distinguishes:
   - `termination_status="verified"` only when ownership was positively
     fingerprint-verified, the owned group was terminated, and the supervisor
     was reaped; and
   - `termination_status="unverified"` when identity, termination, or reaping
     cannot be proven.
   Never signal after fingerprint mismatch. Never leave the durable receipt
   `running`.
3. Replace the supervisor's blocking `subprocess.run` with `Popen`, minimal
   `SIGINT`/`SIGTERM` forwarding to its direct child, and exit-record writing
   in `finally`. The controller remains group-termination owner; the wrapper
   must not signal its own group recursively.
4. Reconciliation accepts only a run-owned, path-contained, hash-matched
   terminal receipt with an interruption marker,
   `termination_status="verified"`, and an exact invocation ID matching every
   affected latest running attempt.
5. Determine the complete repair batch as all ledger closures whose latest
   `running` attempt has that shared invocation ID. In one atomic ledger
   revision, change every member to `interrupted`, bind receipt path/hash, and
   return each to `ready_for_fix`, preserving member order, prior attempts,
   strategy family, and attempt-history hashes. Do not count interruption as a
   rejected strategy, prohibit its strategy family, or create an attempt.
6. If the member set is partial, mismatched, ambiguous, outside the artifact
   root, nonterminal, or unverified, terminalize process state as unreconciled
   and leave closure recovery deterministically blocked. No ledger mutation or
   new fixer launch is authorized.
7. After successful whole-batch reconciliation, emit the existing `retry_fix`
   route and require a newly bound attempt before a new model launch. Existing
   wall-timeout semantics remain unchanged.

### Deterministic tests

- Send real `SIGINT` to a controller running a blocking fake Codex child; prove
  supervisor and descendant disappear within a bound and the schema-valid
  receipt is terminal, verified, revision-incremented, and scratch-audited.
- Re-run the same spec and prove recovery returns the terminal receipt without
  another child.
- On fingerprint mismatch, prove no signal, no positive termination proof, no
  ledger reconciliation, and no new launch.
- Atomically reconcile a two-member repair batch sharing one invocation:
  both attempts become `interrupted`, order/history/hashes remain intact,
  neither strategy is rejected, and no duplicate launch occurs.
- Reject incomplete membership, mixed invocation IDs, changed receipt hash,
  nonterminal/unverified receipts, and paths outside the artifact directory
  without any ledger mutation.
- Prove successful reconciliation returns existing `retry_fix`; ambiguous or
  missing evidence remains blocked.

## Phase 5 — normative unbound routes (`N2`)

### Exact targets

- Only the routine-routing paragraph in `references/protocol.md`.
- Only the corresponding REVIEWING paragraph in
  `references/phase-contracts.md`.
- `scripts/closure_driver.py`: expose the existing legal set as one constant
  used by `continue_without_bound_program`; no behavior change.
- `tests/test_closure_driver.py`

### Contract and tests

Both paragraphs and the executable constant must enumerate exactly
`next_ready`, `retry_fix`, and `redesign`. These unbound routes return to the
outer coordinator for a fresh source-hashed program; they are not
`routine_program_missing`. Unknown routes remain fail-closed, and supplied
programs retain cycle/path validation.

Parameterize the existing behavioral test over all three legal routes, retain
the unknown-route blocker assertion, and add one conformance assertion comparing
the executable set with the exact route names in both paragraphs.

## Bounded certification

1. After implementation, invoke the production-real preflight entry point to
   mint a fresh source-certification capability manifest `/2` in a new
   certification artifact directory. Do not read or modify the paused run's
   `/1` manifest.
2. Validate the fresh manifest against its schema; require
   `simulation_only=false`, `status="ready"`, and a valid closed
   `certification_runtime`. Recompute and compare the manifest file SHA-256,
   interpreter executable SHA-256, pytest module/version evidence, and
   dependency fingerprint before executing tests. Failure blocks
   certification; ambient `python3` is never a fallback.
3. Use only the validated absolute
   `certification_runtime.interpreter_path` for six invocations:

   ```text
   -m unittest discover -s skills/codex/implement-v13-codex/tests -p test_review_closure.py
   -m unittest discover -s skills/codex/implement-v13-codex/tests -p test_repair_gates.py
   -m unittest discover -s skills/codex/implement-v13-codex/tests -p test_closure_driver.py
   -m unittest discover -s skills/codex/implement-v13-codex/tests -p test_run_exec.py
   -m unittest discover -s skills/codex/implement-v13-codex/tests -p test_run_feature.py
   -m unittest discover -s skills/codex/implement-v13-codex/tests -p test_production_vertical_slice.py
   ```

4. With that same interpreter, run JSON Schema meta-validation for every
   changed/new schema and representative current plus legacy fixtures, then the
   complete `skills/codex/implement-v13-codex/tests` suite.
5. Run `git diff --check` and a source-scope audit proving implementation
   changes are confined to the targets above and no paused-run, queue, or E2E
   artifact changed.
6. Record each argv, interpreter identity and hashes, test counts, return code,
   stdout/stderr hashes, schema results, scope-audit result, and elapsed time in
   the implementation run's structured evidence.

## Completion traceability

| ID | Atomic completion proof |
| --- | --- |
| `F5` | Structured command bytes/runtime hash survive every artifact; observed pytest legacy input normalizes; unsafe legacy input blocks; a generic non-pytest argv remains valid. |
| `F3` | Each exact selected command runs once through the controller broker; the receipt binds broker, policy, runtime, command, outputs, and scratch; reviewer performs no Seatbelt execution. |
| `F4` | Receipt `/3` provider usage survives fresh/recovery/rollover paths; unknown is never zero; model token fields are rejected; configured-limit unknown blocks before arithmetic. |
| `F6` | Real SIGINT yields verified termination/reaping and no descendant; all members of the shared invocation reconcile in one ledger revision; unverified cleanup blocks reconciliation and relaunch. |
| `N2` | Executable legal-route set, both normative paragraphs, and behavior tests agree exactly. |

Completion requires all targeted tests, schema checks, the complete bounded
suite, diff check, and scope audit to pass with recorded evidence. The paused
run remains unchanged; any later resumption is a separate operator action.
