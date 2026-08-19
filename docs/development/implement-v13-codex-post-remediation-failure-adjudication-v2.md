# Implement-v13-codex post-remediation failure adjudication v2

Status: final; analysis only

## Scope and authority

This adjudication resolves only the seven candidate IDs presented in:

- `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-post-remediation-failure-challenge-1.md`
- `<user-home>/Documents/harness_labs/docs/development/implement-v13-codex-post-remediation-failure-challenge-2.md`

The only candidates are `F3`, `F4`, `F5`, `F6`, `F8`, `N1`, and `N2`.
No candidate is split or renamed, and no additional failure mode, phase, or
remediation concept is introduced. Additional source and run artifacts were
inspected only to validate evidence cited by the two challenges.

The bounded source is
`<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex`.
The bounded run is `fr_0a8feb07a847488ea910a0ec5a2a99d7` through the
operator pause represented by review-closure-ledger revision 188.

## Claim-by-claim adjudication

### F3 — accepted, narrowed

**Exact surviving statement.** The production-real capability manifest proves
that the controller host could run the recorded Seatbelt probe, but it does not
prove that a later `codex exec` child can execute the same command with the same
broker authority. The manifest's `same_broker_as_production=true` value is
derived from `production_real`, not from an execution-path identity check. This
is a harness/runtime-boundary attestation defect. It does not establish that
ordinary Codex children are unsandboxed or that host Seatbelt is generally
unavailable.

**Resolution of the disagreement.** Challenge 2 correctly refutes the broader
reading: `run_exec.py` passes a valid Codex sandbox selection, and the host
probe is real. That does not refute the narrower candidate because the actual
fixer invocation accepted the manifest and then could not perform its direct
Seatbelt attempt.

**Evidence bindings.**

- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/repair_preflight.py:248-293`,
  `_sandbox_probe_command`, directly constructs and executes the host
  `sandbox-exec` probe.
- The same file at `:296-378`, `probe_role_capabilities`, assigns
  `same_broker_as_production=production_real` and emits the asserted execution
  path without comparing it to the later child invocation.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:986-1041`,
  `_codex_argv`, gives the child a Codex sandbox; `:1083-1114` validates inputs
  and constructs the ordinary child invocation rather than executing the
  manifest's recorded host command.
- Run capability manifest
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/capability-v14-host/capability-manifest.v1.json`,
  SHA-256
  `ac68ed74cfbb65f01a2e39883fd2afc721b853e197144f31f33714c1f2087c20`,
  records `status="ready"`, `simulation_only=false`, and
  `same_broker_as_production=true` for all probes.
- Fixer result
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-fix-code_fixer-34-1.output.json`,
  SHA-256
  `7e34ce4af3b8e18fdb4306166fe8a8e537a8e1f9b0aab972404fbb3ac1a45dc5`,
  accepts the manifest but records `sandbox_apply: Operation not permitted`
  for the actual invocation.

**Responsible source/contract.** Harness capability attestation and execution
boundary in `repair_preflight.py` and `run_exec.py`.

**Permitted fix boundary.** Use one controller-owned host executor for the
exact dependency-mapped certification commands outside the model's
already-managed sandbox; bind its broker, interpreter, policy, and outputs in a
gate receipt consumed by read-only reviewers. Manifest validation alone may not
claim per-invocation enforcement. Do not weaken production Seatbelt checks or
broaden model repository-write authority.

### F4 — accepted, narrowed

**Exact surviving statement.** Durable coordinator usage accounting ignores
provider-emitted `turn.completed.usage` and instead accepts nullable or
model-authored telemetry, converting unavailable telemetry to zero. This makes
persisted usage inaccurate and makes it an invalid input when authorized
token-slope limits are configured. It is not established as the cause of this
run's review stall, and the absence of an operator-authorized hard limit is not
a defect.

**Resolution of the disagreement.** Challenge 2 correctly limits causality:
this dispatch had no explicit coordinator limits, so zero telemetry did not
bypass a live hard stop. Challenge 1 remains correct that an authoritative
nonzero provider value was already available and was persisted as zero.

**Evidence bindings.**

- Coordinator stdout
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-COORDINATOR-drive-migrated-2d5133f1e4487e32-feature_coordinator-10-1.stdout.jsonl`,
  SHA-256
  `fe2359d439808605f3f83b2399c1db5fd25acae778afabfbbdf24c26d0105e06`,
  ends with `turn.completed.usage` values of 13,023,321 input, 12,185,856
  cached input, and 82,501 output tokens.
- Its sibling output
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-COORDINATOR-drive-migrated-2d5133f1e4487e32-feature_coordinator-10-1.output.json`,
  SHA-256
  `09ad0a08b89d1b9bf7bc3a6bbbd50a130736bb779f9e581e2699ebb1aef705fa`,
  records all three token values as zero.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/schemas/feature-coordinator-result.schema.json:60-69`
  permits nullable, nonnegative self-reported telemetry.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_feature.py:153-178`,
  `_coordinator_limits`, makes limits authority-dependent; `:1065-1072` and
  `:1211-1248` consume result telemetry and encode absence as zero.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:685-734`
  reads stdout terminal events for protocol validation but does not supply
  their usage to the coordinator metric consumed by `run_feature.py`.

**Responsible source/contract.** Harness coordinator result schema, invocation
receipt processing, and coordinator accounting in
`feature-coordinator-result.schema.json`, `run_exec.py`, and `run_feature.py`.

**Permitted fix boundary.** Extract and hash terminal
`turn.completed.usage` from the process receipt; reject disagreement with
model-authored telemetry or remove model ownership of those fields; represent
genuinely unavailable usage as `unknown`, never zero. Apply hard rollover or
block thresholds only when supplied by exact operator or named safety
authority.

### F5 — accepted, narrowed

**Exact surviving statement.** Closure-test commands are persisted as
unstructured command strings without an interpreter identity. The bounded run
contains one legacy closure command whose ambient `python3` resolved to Python
3.14 without pytest while the available certification interpreter passed the
same test. This is a closure-test and legacy-command binding defect, not a
claim that every harness subprocess uses the wrong interpreter.

**Resolution of the disagreement.** Challenge 1's original pre-window
fixer-19 citation is not admissible for the bounded window. Challenge 2's
narrowing is accepted because the in-window closure-004 reviewer result and
the current recording schema independently prove the limited claim.

**Evidence bindings.**

- Reviewer result
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-targeted_review-l1_l2_contract_boundary_reviewer-33-1.output.json`,
  SHA-256
  `767a83ed38f391f3e5aa30cd86509955028aac48ac2f4192034c6044924cf42a`,
  records that ambient Python 3.14 lacked pytest and that the same immutable
  test node passed with the available certification interpreter.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/schemas/closure-test-result.schema.json:5-12`
  models every command only as a nonempty string.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/review_closure.py:760-853`,
  `record_test`, records command strings verbatim; `:1027-1038` reuses legacy
  strings during assertion-map backfill.

**Responsible source/contract.** Harness closure-test result protocol and
legacy closure-command migration in `closure-test-result.schema.json` and
`review_closure.py`.

**Permitted fix boundary.** Bind one preflight-probed absolute interpreter and
dependency fingerprint into every new test command and gate input. During
migration, normalize or explicitly classify legacy unpinned commands before
model launch. An agent must not discover a substitute interpreter ad hoc after
a required command fails.

### F6 — accepted, narrowed

**Exact surviving statement.** Interruption of an owned child can leave its
receipt and closure attempt durably `running` because process-group termination
is confined to the wall-timeout branch and the blocking supervised child lacks
an exception/signal finalization path. The evidence proves interruption-time
cleanup and durable reconciliation are missing; it does not prove that a
descendant is still alive now.

**Resolution of the disagreement.** Challenge 2 correctly rejects the stronger
live-orphan inference because the operator later terminated the processes.
That cleanup does not cure the missing interruption path or the unreconciled
durable state.

**Evidence bindings.**

- Receipt
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-fix-code_fixer-35-1.receipt.json`,
  SHA-256
  `92c29fe1f2d34d14a5f238029ac7ac3a7fb41bea9aff51ebfcefdc67992fc28a`,
  remains `status="running"`, revision 3, with PID and process group 72376
  and no completion timestamp. Its declared exit and output artifacts are
  absent, and ledger revision 188 retains attempt 6 as running.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:550-570`
  provides fingerprint-safe group termination, while `:1258-1301` invokes it
  only for wall timeout; the polling lifecycle has no exception-finalization
  path.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/supervised_child.py:14-78`
  blocks in `subprocess.run` and writes its exit record only after that call
  returns.

**Responsible source/contract.** Harness child supervision, interruption
cleanup, and durable receipt lifecycle in `run_exec.py` and
`supervised_child.py`.

**Permitted fix boundary.** Add exception-safe, fingerprint-verified
termination around the owned polling lifecycle; persist an interrupted
terminal receipt and scratch audit before re-raising; prove with one real
SIGINT/operator-pause test that no descendant survives and recovery preserves
attempt history.

### F8 — rejected

**Exact rejected statement.** The shared lexical name `status` at the result
root and within each finding does not establish a material harness failure in
the bounded run.

**Resolution of the disagreement.** Challenge 2 prevails. JSON scope and
disjoint enum domains make the fields mechanically distinct, and the
controller routes solely from per-fingerprint finding statuses. The two cited
cross-product examples were handled correctly. Challenge 1 demonstrates
potentially confusing naming, but no incorrect route, schema rejection,
recovery failure, or measured material cost.

**Evidence bindings.**

- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/schemas/closure-targeted-review-result.schema.json:5-12`
  defines root `status` as `passed|blocked` and finding `status` as
  `fixed|not_fixed|regression`.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/closure_driver.py:471-496`
  builds the controller input only from each finding's status.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/review_closure.py:1640-1705`,
  `record_review`, routes closure state only from the per-fingerprint map.
- The reviewer outputs with SHA-256
  `767a83ed38f391f3e5aa30cd86509955028aac48ac2f4192034c6044924cf42a`
  and
  `b4a6fc0200aec3b4629efb3e92c71f6f9c7b852aa14e06250583e4f445a51aee`
  contain the two orthogonal combinations and were routed according to their
  findings.

**Responsible source/contract.** No failed harness contract is established.

**Permitted fix boundary.** None. `F8` is outside implementation scope.

### N1 — rejected

**Exact rejected statement.** Repeated Codex model-cache compatibility
diagnostics are not established as a material implement-v13 harness failure in
the bounded run.

**Resolution of the disagreement.** Challenge 2 prevails. The cited calls
completed successfully, source intentionally persists stderr diagnostics
separately from terminal cause, and neither challenge binds the messages to a
correctness, latency, token, retry, or recovery consequence. This does not
deny the external warning; it rejects its inclusion in the finite actionable
failure list.

**Evidence bindings.**

- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:617-682`,
  `classify_terminal_cause` and `_stderr_diagnostics`, intentionally separates
  terminal classification from captured stderr.
- Successful reviewer receipt
  `<user-home>/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-targeted_review-l1_l2_contract_boundary_reviewer-33-1.receipt.json`,
  SHA-256
  `f0b033e66ca9565ac39e9ad7b1656402ad888014a99744a7743d249f77344ef1`,
  records `status="succeeded"` and terminal cause `none` despite the cache
  diagnostics.

**Responsible source/contract.** The warning belongs to the external Codex
CLI/local-cache compatibility boundary; no failed implement-v13 harness
contract is established.

**Permitted fix boundary.** None in the harness-source implementation. The
external item is non-actionable within this scoped fix.

### N2 — accepted, narrowed

**Exact surviving statement.** Current executable behavior and regression
tests correctly return unbound `next_ready`, `retry_fix`, and `redesign`
routes to the outer coordinator, while two normative references still declare
an unbound same-closure retry or redesign to be a deterministic blocker. This
is an open normative contract/source synchronization defect confined to
`references/protocol.md` and `references/phase-contracts.md`; no post-v14
runtime recurrence or contradiction in `SKILL.md` is established.

**Resolution of the disagreement.** Both challenges agree on the underlying
conflict. Challenge 2's narrower file scope prevails because its source audit
found the contradiction only in the two normative references.

**Evidence bindings.**

- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/closure_driver.py:108-146`,
  `continue_without_bound_program`, returns the three legal unbound routes;
  `:247-258`, `continue_routine`, invokes that behavior when no program is
  bound.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/tests/test_closure_driver.py:33-72`
  distinguishes a legal unbound retry from an unknown route.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/references/protocol.md:160-163`
  says a missing same-closure retry or redesign program is
  `routine_program_missing`.
- `<user-home>/Documents/harness_labs/skills/codex/implement-v13-codex/references/phase-contracts.md:125-130`
  says absence of the continuation program is a deterministic blocker.

**Responsible source/contract.** Harness normative contract synchronization in
`references/protocol.md` and `references/phase-contracts.md`.

**Permitted fix boundary.** Change only the retry-routing paragraphs in those
two references to match the accepted v14 executable behavior, and add one
contract/source conformance assertion for the three legal unbound routes and
unknown-route fail-closed behavior.

## Final finite list

### Accepted actionable harness-source defects

1. `F3` — capability-manifest broker equivalence is not execution authority.
2. `F4` — provider usage is ignored and unavailable telemetry is encoded as
   zero.
3. `F5` — closure-test command interpreter identity is unbound.
4. `F6` — interruption does not finalize owned process-group and receipt
   state.
5. `N2` — normative unbound-retry text contradicts executable behavior.

### Rejected external or non-actionable items

1. `N1` — external cache diagnostics have no established material consequence
   and require no scoped harness-source fix.

### Rejected harness claim

1. `F8` — distinct status scopes routed correctly; no material defect is
   established.

No candidate is collapsed into another candidate.

## Scope proof

- Input IDs, exactly:
  `F3`, `F4`, `F5`, `F6`, `F8`, `N1`, `N2`.
- Final accepted IDs, exactly:
  `F3`, `F4`, `F5`, `F6`, `N2`.
- Final rejected IDs, exactly:
  `F8`, `N1`.
- Collapsed IDs: none.
- New IDs: none.
- Split or renamed IDs: none.
- New failure modes, phases, or remediation concepts: none.
- Harness-source implementation scope is limited to the permitted fix
  boundaries for `F3`, `F4`, `F5`, `F6`, and `N2`.
- External/non-actionable scope contains only rejected `N1`; rejected `F8`
  likewise authorizes no source change.
