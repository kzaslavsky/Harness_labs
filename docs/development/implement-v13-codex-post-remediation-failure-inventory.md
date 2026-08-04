# Post-remediation implement-v13-codex failure inventory

Status: historical analysis

## Scope and method

**Observation window.** This inventory begins with the first post-adjudication
controller adoption used by the live testing-harness run and ends at the
operator pause recorded at `2026-07-23T11:53:32.043444Z`. Operationally, the durable
lower bound is the v13 controller migration
`controller-migration-v13.v1.json`; the upper bound is queue state revision 91
and closure-ledger revision 188.  It deliberately excludes defects that were
only described in the earlier failure-analysis/remediation documents unless
they reoccurred in this window.

Primary evidence is the immutable run directory:

`/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7`

The paused durable state is `serial_implementation_queue.json` field
`state_revision=91`, `features[0].status="blocked"`,
`features[0].blocker.blocker_class="operator_pause"`; the ledger has
`state_revision=188`, `active_closure_id="closure-005-disposable-attestation"`,
and its sixth attempt is `running`.  No process state was treated as authority.

Classifications below identify the *primary* owner. “Harness” means a
deterministic contract/control-plane/runtime-boundary defect; “model” means an
otherwise valid agent made a bad implementation or reasoning choice.  A model
can expose a harness defect, but it does not turn that defect into a model
failure.

## Observed modes

### F1 — Unbound retry route was incorrectly terminal (fixed in v14)

- **Symptom/evidence.** After closure 004 was independently returned
  `not_fixed`, the v13 controller blocked with `routine_program_missing` rather
  than giving the outer coordinator a chance to bind the required fresh
  retry.  The blocker is recorded in the v13 checkpoint/queue transition and
  the run’s migration evidence; package v14 is committed at
  `controller-migration-v14.v1.json`, fields `state="committed"`,
  `journal_revision=8`, and
  `new_package_digest=1262c8fd5340ee69f50cbaba5ac49578eed9d33280d001a464563075c4a49021`.
- **Source binding/root cause.** The old routine path treated a missing
  pre-bound `retry_fix`/`redesign` program as a failure.  The correction is
  `skills/codex/implement-v13-codex/scripts/closure_driver.py:108-145,258`:
  `continue_without_bound_program()` returns such routes to the coordinator;
  only unknown routes still produce `routine_program_missing`.
- **Classification/impact.** Harness. It produced a false hard block,
  controller migration work, and lost wall time; it did not weaken correctness.
- **Status/risk/correction.** Fixed in the source and v14 package (reported
  source tests: 167 implement-v13 and 33 serial tests).  Recurrence risk is
  moderate until route-table/property tests cover every post-review route.
  Bounded follow-up: add one parameterized test over all legal unbound routes
  and assert only unknown routes block.

### F2 — Stale-base failure preserved forbidden success artifacts (fixed by closure 004)

- **Symptom/evidence.** The independent reviewer receipt
  `fr_...-REVIEWING-targeted_review-l1_l2_contract_boundary_reviewer-32-1.output.json`
  reports fingerprint `l1l2:base-head-drift-not-gated-before-merge:v1` as
  `not_fixed`: the guard blocked merge, but moved a successful role result and
  receipt to `quarantine/succeeded-role-pairs` rather than keeping them absent.
  That violates the effect contract’s `must_remain_absent` semantics.
- **Source binding/root cause.** The feature implementation’s stale-base
  error path implemented quarantine as preservation, whereas the contract
  required non-creation/non-retention of terminal-success artifacts.  The
  harness exposed it correctly because assertion-map effects were checked;
  the older immutable test did not assert both absences.
- **Classification/impact.** Primarily model/product implementation defect,
  detected by the harness (not a harness false positive).  It is correctness
  critical: a failed stale-base run could look partially successful.  It caused
  one repair/review cycle and rerun cost.
- **Status/risk/correction.** Fixed: the subsequent coordinator record bound
  `closure-004-v14-terminal-absence-program.v1.json`; ledger revision 184 records
  `closure-004-stale-base-guard.status="closed"` and attempt 5 as accepted.
  Keep the two absence effects in the canonical assertion map and add a direct
  regression test that discovers no success result/receipt anywhere, including
  quarantine.

### F3 — Production-real Seatbelt recertification conflicts with nested worker sandboxes (open)

- **Symptom/evidence.** Closure 005 attempt 4’s ledger rejection says the
  independent reviewer could not collect its required pytest node because its
  read-only sandbox could not write controller-owned ephemeral scratch; the
  Terra fixer separately reported `Seatbelt enforcement probe failed`.
  Attempt 5 is stronger evidence: exact supplemental test passed 1/1 and four
  closed regressions passed, but focused checks were `11 passed, 4 failed` and
  the full suite `87 passed, 20 failed`, primarily unavailable Seatbelt
  enforcement/downstream lifecycle artifacts.  All appear verbatim in
  `review-closure-ledger.v1.json`,
  `closures[closure-005-disposable-attestation].attempts[3..4].rejection_evidence`.
- **Source binding/root cause.** `repair_preflight.py:271` invokes exact pytest
  with an ephemeral TMPDIR; `repair_preflight.py:304-314` requires a real host
  Seatbelt broker.  The contract does not yet distinguish (a) certification of
  host enforcement from (b) a restricted review worker that cannot create the
  required ephemeral state.  The resulting nested sandbox is contradictory:
  a runtime check required for acceptance cannot execute under the assigned
  reviewer permission set.
- **Classification/impact.** Harness/runtime-environment contract defect, not
  Luna/Terra/Sol reasoning. It fail-closes correctly, but has repeatedly
  consumed fixer/reviewer/coordinator turns and prevents completion.
- **Status/risk/correction.** Open and the immediate blocker.  Bounded fix:
  introduce one controller-owned, capability-probed certification executor
  whose only writable area is a unique run scratch directory; pass its signed
  receipt to read-only reviewers.  Do not let reviewers substitute inherited
  sandbox success or a manifest-only claim.  Add tests for both permitted
  broker execution and denied direct reviewer execution.

### F4 — Coordinator context accounting is misleading and does not constrain real cost (open observability/control defect)

- **Symptom/evidence.** Post-remediation coordinator output records declare
  `telemetry.input_tokens=0`, `output_tokens=0`, and
  `cached_input_tokens=0` even while substantial coordinator work occurred.
  Examples are
  `...COORDINATOR-drive-migrated-2d5133f1e4487e32-feature_coordinator-10-1.output.json`
  and `...COORDINATOR-drive-migrated-eb2d3018af23690c-feature_coordinator-3-1.output.json`,
  both under `.telemetry`.  Therefore the durable run artifacts cannot
  substantiate token cost or enforce a slope limit from observed values.
- **Source binding/root cause.** `run_feature.py:1066-1072,1212-1260` only
  accumulates numeric telemetry returned by the coordinator; the schema accepts
  zero.  The driver’s budget mechanism is present but receives no authoritative
  provider usage receipt.  This is why fresh/resumed turns can still grow
  expensive despite intended rollover limits.
- **Classification/impact.** Harness observability/control-plane defect.  It
  is not proof that the model alone “ran long”; it prevents the harness from
  measuring and stopping that behavior.  It affects token and wall-time cost,
  not acceptance correctness directly.
- **Status/risk/correction.** Open.  Require the supervised child receipt to
  carry provider-authoritative usage or explicitly classify usage as unknown;
  never encode unknown as zero.  Apply token/turn limits to an immutable
  receipt sequence and roll over before dispatching another coordinator turn.

### F5 — Toolchain/interpreter drift caused false certification failures (open, partially mitigated)

- **Symptom/evidence.** A fixer receipt,
  `fr_...-REVIEWING-fix-code_fixer-19-1.output.json`, reports repeated
  `python3 -m pytest` failures before collection: `No module named pytest`,
  while a manual fixture and `py_compile` passed.  It also states the `uv`
  fallback was inaccessible.  These are environment failures, not test
  failures.
- **Source binding/root cause.** The contracts still document generic
  `python3` invocation while the capability contract depends on the actual
  runner/interpreter environment.  The v13 protocol acknowledges this risk
  (packaged `references/protocol.md:273-274`), but worker invocation did not
  consistently bind to the preflight-probed interpreter and dependency set.
- **Classification/impact.** Harness toolchain contract defect. It creates
  false negative certification, retries, and wall time; it did not admit an
  unverified success.
- **Status/risk/correction.** Partially mitigated by later capability manifests
  (`capability-v13-host/` and `capability-v14-host/`), but recurrence remains
  evidenced by later nested certification failures.  Bounded fix: include the
  exact interpreter command and dependency fingerprint in every worker packet,
  and reject dispatch before model launch if it cannot run the certified pytest
  probe.

### F6 — Controller interruption orphaned its supervised child process group (open)

- **Symptom/evidence.** Interrupting the foreground v14 `run_feature.py` process
  stopped the controller, but its active `supervised_child.py` (PID 72376) and
  `codex.js exec` child (PID 72378) survived with PPID 1. Both required explicit
  `SIGTERM`. The ledger therefore retains attempt 6 as `status="running"` with
  no terminal result while the queue is correctly blocked by `operator_pause`.
  Process state is liveness evidence, not phase authority, but the live PPID/PID
  observation demonstrates failure to clean up an owned process group.
- **Source binding/root cause.** `run_exec.py:1260-1291` starts the supervisor in
  a new session and kills the owned group only on its explicit timeout path.
  There is no `KeyboardInterrupt`/`BaseException` cleanup around the polling
  loop. `supervised_child.py:55` uses blocking `subprocess.run()` and likewise
  installs no signal-forwarding/finally cleanup. The group ownership data exists
  in the receipt, but controller interruption does not consume it.
- **Classification/impact.** Harness process-supervision defect. It can leave a
  model mutating the feature worktree after the user believes the run is paused,
  and it leaves a nonterminal attempt requiring recovery reconciliation.
- **Status/risk/correction.** Open. Wrap the `run_exec.py` polling/wait region in
  exception-safe owned-group termination and persist an interrupted terminal
  receipt before re-raising. Add a real pause/SIGINT integration test asserting
  no descendant survives and resume appends an interrupted result without
  deleting attempt history.

### F7 — Legacy broad-suite expectations create noisy, non-authoritative failures (open hygiene issue)

- **Symptom/evidence.** Closure 005 attempt 5 shows the exact canonical node
  and four closed regression nodes passed, while the broad suite still had 20
  failures downstream of unavailable Seatbelt enforcement.  Earlier receipts
  also used stale broad expectations after targeted fixes.  These failures are
  valuable diagnostics, but were repeatedly mixed into fix status despite not
  isolating the actual closure contract.
- **Source binding/root cause.** The closure ledger records broad-suite output
  as rejection evidence alongside exact effect-contract proof.  The source
  contracts provide exact node/assertion-map gates, but no explicit taxonomy
  separates “environment-unavailable dependent suite” from “closure regression.”
- **Classification/impact.** Harness gate-composition defect. It can magnify
  unrelated environmental faults into repeated closure cycles; it should never
  convert a real failing dependent safety gate into a pass.
- **Status/risk/correction.** Open, coupled to F3.  Classify every test result
  as exact-closure, required-regression, dependent-environment, or diagnostic;
  only the first two decide a closure once the host capability receipt is valid.
  Preserve the full suite as a fail-closed integration gate, with a single
  environment blocker rather than fresh implementation retries.

### F8 — Reviewer top-level status and finding disposition have inverted meanings (open observability ambiguity)

- **Symptom/evidence.** Closure 004's reviewer output
  `...targeted_review-l1_l2_contract_boundary_reviewer-33-1.output.json` has
  top-level `status="blocked"` while its only finding is `fixed`; the ledger
  accepts and closes the closure. Closure 005's reviewer output
  `...targeted_review-security_privacy_destructive_behavior_reviewer-34-1.output.json`
  has top-level `status="passed"` while its only finding is `not_fixed`; the
  ledger rejects the attempt. Both are schema-valid.
- **Source binding/root cause.** `closure-targeted-review-result.schema.json:9`
  allows only `passed|blocked` for review execution, while
  `review_closure.py:1652-1699` deliberately decides closure acceptance from
  per-fingerprint `finding_statuses`. The two status domains are semantically
  independent but share an unqualified field name.
- **Classification/impact.** Harness observability-contract ambiguity. The
  controller routed both correctly, but operators and downstream metrics can
  invert the outcome or report a closure as blocked after it was accepted.
- **Status/risk/correction.** Open. Rename or derive the top-level field as
  `review_execution_status`, retain `finding_statuses` as the closure outcome,
  and emit one controller-derived `closure_disposition` in the durable child
  result. Add cross-product tests for blocked/fixed and passed/not-fixed.

## Prioritized open work

| Priority | Defect | Why now | Bounded correction |
|---|---|---|---|
| P0 | F3 nested Seatbelt contradiction | Blocks closure 005; repeats costly recertification | Controller-owned certified broker receipt; reviewer consumes receipt read-only |
| P1 | F4 zero/unknown coordinator telemetry | Cannot control or explain cost | Provider-authoritative usage receipt; unknown is not zero; enforce limits pre-dispatch |
| P1 | F5 interpreter/toolchain binding | Turns missing pytest into model retries | Preflight-pinned interpreter/dependency command in every worker packet |
| P1 | F6 interrupt cleanup | A paused controller left owned model processes alive | Exception-safe process-group termination plus interrupted receipt/recovery test |
| P2 | F7 gate taxonomy | Broad diagnostic failures churn closures | Four-class result taxonomy; one environment blocker, not repair loops |
| P2 | F8 reviewer status ambiguity | Durable status can appear opposite to finding outcome | Separate execution status from closure disposition |

F1 and F2 are retained as newly observed regressions that were fixed; they are
not reasons to keep the current run paused.  The current pause is justified by
F3, with F4/F5/F7 explaining why the review phase has consumed disproportionate
time.
