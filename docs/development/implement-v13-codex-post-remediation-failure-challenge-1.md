# Adversarial challenge of the post-remediation failure inventory

Status: complete; analysis only; no controller, queue, checkpoint, ledger, run
artifact, or feature worktree was changed

## Scope, authority, and method

This challenge tests only the claims in
`/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-post-remediation-failure-inventory.md`
against the current repository source and the same bounded observation window.
The lower bound is the committed v13 migration at
`2026-07-23T09:31:49.275303Z`; the upper bound is the operator pause at queue
revision 91 and review-closure-ledger revision 188. The exact implementation
scope is `skills/codex/implement-v13-codex`,
`skills/codex/serial-implement-codex`, and the paused feature run
`fr_0a8feb07a847488ea910a0ec5a2a99d7`.

Durable authority at audit time:

- Queue:
  `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/docs/development/serial_implementation_queue.json`,
  SHA-256
  `d0c4e39a7747f146d7c5da4e721f6f3148c8f9b90f07d702a4b3f46ffeeaa5c9`,
  fields `/state_revision=91`, `/features/0/status="blocked"`, and
  `/features/0/blocker/blocker_class="operator_pause"`.
- Closure ledger:
  `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/review-closure-ledger.v1.json`,
  SHA-256
  `90ab70400c0470bc788c24bcc3b6f7e6f1e32f5364988724850f7c8ac7785ea2`,
  fields `/state_revision=188`,
  `/active_closure_id="closure-005-disposable-attestation"`, and active
  closure `/status="fix_running"`.
- Package migration:
  `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/controller-migration-v14.v1.json`,
  SHA-256
  `fe3ce8f20691e8589ebd39ee9f308c8499a5610d10000a709b62921ffd4a104c`,
  fields `/state="committed"` and
  `/new_package_digest="1262c8fd5340ee69f50cbaba5ac49578eed9d33280d001a464563075c4a49021"`.

Process state was used only as liveness evidence. A claim survived only when
source or durable run evidence established a repeatable failure boundary. A
normal fail-closed gate, routine model tool retry, or non-terminal diagnostic
was not promoted to an independent harness defect without a material
correctness, recovery, or cost consequence.

## Disposition of F1 through F8

### F1 — confirmed historical harness defect; fixed, but contract text drift remains

The claim that an unbound `retry_fix` route incorrectly became terminal is
confirmed. Queue `/features/0/blocked_history` records the post-v13
`routine_program_missing` blocker at `2026-07-23T11:12:50.973248Z`. The current
fix is concrete:

- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/closure_driver.py:108-146`,
  symbol `continue_without_bound_program`, returns `next_ready`, `retry_fix`,
  and `redesign` without a blocker.
- The call site is the same file at `:247-258`, symbol `continue_routine`.
- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/tests/test_closure_driver.py:33-72`
  distinguishes legal unbound retry from an unknown unbound route.

**Disposition:** survives as confirmed/fixed, not actionable by itself. The
remaining prose/source contradiction is enumerated separately as N2 because it
is not the original runtime defect and was not listed in F1.

### F2 — confirmed product implementation defect; fixed

Closure 004 attempt 4 in the authoritative ledger records the independent
`not_fixed` disposition because successful result/receipt artifacts remained in
quarantine. Attempt 5 records strategy
`terminal_failure_success_artifact_elimination_v14`, finding status `fixed`,
attempt status `accepted`, and closure status `closed`. Independent result
`/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-targeted_review-l1_l2_contract_boundary_reviewer-33-1.output.json`
(SHA-256
`767a83ed38f391f3e5aa30cd86509955028aac48ac2f4192034c6044924cf42a`)
states that no success result, success receipt, integration artifact,
dispatcher acknowledgement, or quarantine entry existed.

**Disposition:** survives as confirmed/fixed. Primary owner is the generated
`testing_harness` product implementation, not the implement-v13 control plane.
It is not actionable in the remediation plan for the harness.

### F3 — upheld and narrowed: broker evidence is not execution authority

The nested Seatbelt failure is real, but the inventory understates the precise
contract break. The v14 manifest
`/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7/capability-v14-host/capability-manifest.v1.json`
(SHA-256
`ac68ed74cfbb65f01a2e39883fd2afc721b853e197144f31f33714c1f2087c20`)
sets `/probes/*/same_broker_as_production=true`. That value is derived from
`production_real` rather than from an execution-path identity check:

- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/repair_preflight.py:248-293`,
  symbol `_sandbox_probe_command`, executes host `/usr/bin/sandbox-exec`.
- The same file at `:296-378`, symbol `probe_role_capabilities`, assigns
  `same_broker_as_production=production_real` and describes the execution path
  as `"run_exec host-broker policy and role subprocess"`.
- In contrast,
  `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:1083-1114,1198-1225`
  only validates the manifest and then constructs an ordinary `codex exec`
  child. It does not execute the certified host command.

The mismatch manifested in fixer result
`.../fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-fix-code_fixer-34-1.output.json`
(SHA-256
`7e34ce4af3b8e18fdb4306166fe8a8e537a8e1f9b0aab972404fbb3ac1a45dc5`):
the manifest validates, the exact nine-effect node passes, but the actual
invocation's direct host-broker attempt returns `sandbox_apply: Operation not
permitted`. The independent reviewer result at the corresponding
`targeted_review...-34-1.output.json` (SHA-256
`b4a6fc0200aec3b4629efb3e92c71f6f9c7b852aa14e06250583e4f445a51aee`)
therefore returns the finding `not_fixed`.

The durable queue is currently blocked by `operator_pause`, not by
`external_capability_unavailable`; F3 is the unresolved reason that motivated
the pause, not the current durable blocker class.

**Disposition:** survives, open, harness/runtime-boundary defect. **Bounded fix
boundary:** make one controller-owned host executor run the exact
dependency-mapped certification commands outside the model's already-managed
sandbox, bind its broker/interpreter/policy and outputs in a gate receipt, and
let read-only reviewers consume that receipt. Manifest validation alone must
not claim per-invocation enforcement. Do not weaken production Seatbelt checks
or grant the model broader repository write access.

### F4 — upheld and strengthened: provider usage already exists but is ignored

This is not merely missing provider support. The provider-authoritative usage is
already present in each coordinator stdout `turn.completed` event. For example:

- `.../fr_0a8feb07a847488ea910a0ec5a2a99d7-COORDINATOR-drive-migrated-2d5133f1e4487e32-feature_coordinator-10-1.stdout.jsonl`
  (SHA-256
  `fe2359d439808605f3f83b2399c1db5fd25acae778afabfbbdf24c26d0105e06`)
  has final `/usage/input_tokens=13023321`,
  `/usage/cached_input_tokens=12185856`, and
  `/usage/output_tokens=82501`.
- Its sibling `.output.json` reports all three values as zero under
  `/telemetry`.
- The first nine v13 coordinator outputs use `telemetry=null` even though their
  corresponding stdout events contain nonzero usage.

The schema and consumer choose model-authored telemetry:

- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/schemas/feature-coordinator-result.schema.json:60-69`
  permits telemetry to be null and otherwise accepts arbitrary nonnegative
  self-reported values.
- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_feature.py:1066-1072,1211-1248`
  reads that field and converts null to zero rather than reading the process
  receipt's stdout usage.

The current resume selection has no operator-authorized
`coordinator_limits`; absent exact authority, lack of a hard stop is not itself
a defect. Encoding known nonzero usage as zero is.

**Disposition:** survives, open, harness observability/control defect.
**Bounded fix boundary:** extract and hash the terminal `turn.completed.usage`
from the process receipt, reject a mismatch with model-authored telemetry or
remove model ownership of those fields, and represent genuinely unavailable
usage as `unknown`, never zero. Only apply a hard rollover/block threshold when
an exact operator or named safety authority supplies it.

### F5 — upheld with corrected in-window evidence

The inventory's named fixer-19 receipt is outside the stated window: it was
prepared at `2026-07-23T05:31:51Z`, about four hours before the committed v13
migration. That citation cannot support an in-window inventory.

The mode nevertheless recurred inside the window. Independent reviewer receipt
`...-REVIEWING-targeted_review-l1_l2_contract_boundary_reviewer-33-1.receipt.json`
(SHA-256
`f0b033e66ca9565ac39e9ad7b1656402ad888014a99744a7743d249f77344ef1`)
ran from `2026-07-23T11:31:10Z` to `11:37:25Z`. Its output says the immutable
ledger command using ambient `python3` failed before collection because Python
3.14 lacked pytest, while the available certification interpreter passed the
same node 1/1. The ledger also retains legacy closure commands that name only
`python3`.

**Disposition:** survives, open, harness/legacy-command binding defect; the
original evidence citation is rejected. **Bounded fix boundary:** bind one
preflight-probed absolute interpreter plus dependency fingerprint into every
new test command and gate input; on migration, normalize or explicitly classify
legacy unpinned commands before model launch. An agent must not discover a
different interpreter ad hoc after a required command fails.

### F6 — upheld as a source-proven interruption-cleanup defect

The durable evidence proves an interrupted, unreconciled child, not the
historical PPID observation by itself:

- `...-REVIEWING-fix-code_fixer-35-1.receipt.json`, SHA-256
  `92c29fe1f2d34d14a5f238029ac7ac3a7fb41bea9aff51ebfcefdc67992fc28a`,
  remains `/status="running"`, `/state_revision=3`, with PID/PGID `72376`,
  and no `completed_at`.
- Its declared `.exit.json` and `.output.json` are absent.
- Ledger revision 188 retains attempt 6 as `status="running"`.

The source establishes the cleanup gap:

- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:550-570`
  has a fingerprint-safe group terminator.
- The same file at `:1258-1301` calls it only on wall timeout; the polling region
  has no `BaseException`/`KeyboardInterrupt` cleanup.
- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/supervised_child.py:14-78`
  uses blocking `subprocess.run` and has no signal-forwarding/finally path.

**Disposition:** survives, open, harness supervision defect. **Bounded fix
boundary:** wrap the owned polling lifecycle in exception-safe,
fingerprint-verified group termination; persist an interrupted terminal receipt
and scratch audit before re-raising; add one real SIGINT/operator-pause test
that proves no descendant survives and recovery preserves attempt history.

### F7 — rejected as an independent failure mode; collapse into F3

The broad suite is a required fail-closed integration gate under
`/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/references/repository-gates.md:5-18`.
Its failure is not non-authoritative noise that may be reclassified away. The
full-suite failure list captured in fixer-34 stdout shows the same early
`Seatbelt enforcement probe failed` leaves downstream expected role-result and
production-lifecycle artifacts absent. That is causal fallout from F3, not a
second gate-taxonomy defect. The inventory provides no in-window source-bound
test whose expectation is independently proved stale after controlling for the
Seatbelt failure.

The actual routing mistake—charging repeated implementation/review attempts
when the governing condition is an unavailable host capability—is already
inside the narrowed F3 fix boundary.

**Disposition:** does not survive independently. Preserve the complete suite as
a required final gate. Do not add a four-class taxonomy that can downgrade real
suite failures; route one source-proven external-capability failure before
another model repair attempt.

### F8 — upheld as a schema semantics ambiguity

The evidence is exact:

- `...targeted_review-l1_l2_contract_boundary_reviewer-33-1.output.json` has
  top-level `/status="blocked"` and its sole finding `/status="fixed"`;
  the ledger accepts it and closes closure 004.
- `...targeted_review-security_privacy_destructive_behavior_reviewer-34-1.output.json`
  has top-level `/status="passed"` and its sole finding
  `/status="not_fixed"`; the ledger rejects the attempt.
- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/schemas/closure-targeted-review-result.schema.json:5-12`
  gives both domains the unqualified name `status`.
- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/review_closure.py:1640-1705`,
  symbol `record_review`, correctly routes only from per-fingerprint statuses
  and never consumes the top-level status.

**Disposition:** survives, open, harness observability/schema defect; no
finding-routing correctness failure occurred. **Bounded fix boundary:** version
the result protocol/schema, name the two domains
`review_execution_status` and controller-derived `closure_disposition`, and
retain an explicit v1 reader for immutable historical receipts. Add all four
execution/finding cross-product tests.

## Gaps not represented by F1-F8

### N1 — Codex model-cache schema incompatibility floods nearly every child

- **Stable ID:** `N1-codex-model-cache-schema-incompatibility`
- **Evidence:** 35 of the 36 process receipts prepared at or after the v13
  lower bound contain diagnostics matching `failed to load models cache` or
  `failed to renew cache TTL` because
  `supports_reasoning_summaries` is missing. A representative is
  `...-REVIEWING-fix-code_fixer-34-1.receipt.json`, SHA-256
  `0dea92da909cd2ea28454207aa9e5072385012d34b66621a1a3b280385eb128d`,
  field `/diagnostics`; it identifies Codex CLI `0.144.1`. The child still
  completed successfully, so this is not a terminal provider failure.
- **Responsible contract/source:** primary owner is the Codex CLI/local cache
  compatibility boundary, not model reasoning. The harness copies every
  nonempty stderr line without classification or deduplication at
  `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:617-682,737-838`,
  symbols `classify_terminal_cause`, `_stderr_diagnostics`, and
  `_finalize_receipt`.
- **Impact:** durable evidence is flooded with repeated error lines and the
  model-availability/cache path is demonstrably unhealthy. No correctness
  failure or exact latency attributable to these messages is established.
- **Status/classification:** open external product/environment defect with a
  bounded harness observability mitigation; not a Luna/Terra/Sol quality
  failure.
- **Bounded fix boundary:** preflight the selected CLI/cache schema once,
  surface one source-hashed capability diagnostic, and deduplicate identical
  per-receipt lines with counts. Pin or upgrade the compatible CLI/cache pair
  outside the run; do not delete user cache data and do not classify a
  process-successful call as terminal solely for this warning.

### N2 — normative retry text contradicts the F1 implementation and regression test

- **Stable ID:** `N2-unbound-retry-contract-source-drift`
- **Evidence/source:** the implemented behavior and regression test are
  described under F1. The normative repository reference instead says that a
  same-closure retry/redesign without a pre-bound program is a deterministic
  blocker:
  `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/references/phase-contracts.md:125-130`.
  That directly contradicts
  `closure_driver.py:108-146,247-258` and
  `test_closure_driver.py:33-61`, which return the route to the outer
  coordinator.
- **Responsible contract/source:** implement-v13 repository-owned normative
  contract synchronization.
- **Impact:** a coordinator or future maintainer following the normative phase
  contract can recreate the just-fixed false blocker, and certification cannot
  say whether prose or executable behavior is authoritative. No new live
  recurrence after v14 is claimed.
- **Status/classification:** open harness contract defect.
- **Bounded fix boundary:** change only the retry-routing paragraphs in
  `SKILL.md`, `references/protocol.md`, and
  `references/phase-contracts.md` to match the adjudicated F1 behavior, or
  explicitly reverse the implementation and its test. Given the accepted v14
  fix, synchronization to the current implementation is the bounded path.
  Add one contract/source conformance assertion for the three legal unbound
  routes and unknown-route fail-closed behavior.

## Finite surviving/new set

The audit yields exactly the following finite sets.

### Confirmed but already fixed; no harness remediation work

1. `F1-unbound-retry-terminal` — historical harness defect, fixed in v14.
2. `F2-stale-base-success-artifact-retention` — historical generated-product
   defect, fixed by closure 004.

### Open actionable harness defects

1. `F3-host-broker-evidence-not-execution-authority`
2. `F4-provider-usage-ignored-and-unknown-encoded-zero`
3. `F5-unpinned-test-interpreter`
4. `F6-interruption-does-not-clean-owned-process-group`
5. `F8-review-status-domain-ambiguity`
6. `N2-unbound-retry-contract-source-drift`

### Open external defect with bounded harness mitigation

1. `N1-codex-model-cache-schema-incompatibility`

### Rejected/collapsed

1. `F7-broad-suite-noise` — not independent; causal fallout and retry routing
   are absorbed into F3.

No additional item is introduced for the in-window
`legacy_assertion_map_contract_conflict`: the new assertion/effect gate correctly
detected that a legacy immutable test did not prove its canonical effects, the
controller suppressed model work, and the operator-authorized supplemental
path preserved provenance. That is expected fail-closed migration behavior, not
a new defect. Routine model patch-context misses and one recovered provider
stream retry are likewise not material independent failure modes.

**Self-check:** 8 original claims were dispositioned exactly once; 2 genuinely
new items were added; neither new item renames or duplicates F1-F8; the final
open set contains 7 finite items (6 harness defects plus 1 external defect with
bounded mitigation).
