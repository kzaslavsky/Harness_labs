# Second adversarial challenge of the post-remediation failure list

Status: complete; analysis only; no controller source, queue, checkpoint,
ledger, run artifact, or feature worktree was changed

## Scope and method

This review attempts to refute only the seven candidate items emitted by
`implement-v13-codex-post-remediation-failure-challenge-1.md`: `F3`, `F4`,
`F5`, `F6`, `F8`, `N1`, and `N2`. It does not revisit any other inventory
item, split any candidate into another item, or introduce a remediation. The
bounded source scope is
`/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex`;
the bounded run scope is feature run
`fr_0a8feb07a847488ea910a0ec5a2a99d7` through the operator pause represented by
ledger revision 188.

For each candidate, the strongest refutation is stated first. A candidate
survives only where the cited source or immutable artifact still establishes a
material contract, correctness, recovery, or observability failure after that
refutation.

## Dispositions

### F3 — survives, narrowed

**Strongest refutation.** The production child is not launched without a
sandbox. `run_exec.py` validates the requested sandbox and passes either
`--sandbox read-only` or `--sandbox workspace-write` to `codex exec`:

- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/scripts/run_exec.py:986-1041`,
  symbol `_codex_argv`.

The capability manifest also contains a real successful host
`/usr/bin/sandbox-exec` probe rather than a simulated result. In
`capability-v14-host/capability-manifest.v1.json` (SHA-256
`ac68ed74cfbb65f01a2e39883fd2afc721b853e197144f31f33714c1f2087c20`),
`/simulation_only=false`, `/broker="macos-seatbelt-sandbox-exec"`,
`/status="ready"`, and every `/probes/*/rc=0` and `/probes/*/passed=true`.
Therefore the evidence does not establish that ordinary Codex children lack
sandbox enforcement, nor that host Seatbelt is generally unavailable.

**Why the refutation does not defeat the candidate.** The manifest's stronger
per-invocation equivalence statement is not measured. In
`repair_preflight.py:248-293`, `_sandbox_probe_command` directly executes
host `sandbox-exec`; at `:349-376`,
`same_broker_as_production` is assigned from the Boolean
`production_real`, and the prose `/execution_path` is emitted without an
identity comparison to the later child. By contrast,
`run_exec.py:1083-1114,1198-1267` validates the manifest and launches a
normal `codex exec`; it does not execute the manifest's recorded command.

The distinction became material in immutable fixer output
`fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-fix-code_fixer-34-1.output.json`
(SHA-256
`7e34ce4af3b8e18fdb4306166fe8a8e537a8e1f9b0aab972404fbb3ac1a45dc5`):
`/blockers/0/evidence/1` accepts the v14 production-real manifest, while
`/blockers/0/evidence/2` records the actual invocation's direct attempt as
`sandbox_apply: Operation not permitted`; `/tests/2/status="failed"` records
return code 71. The manifest proves a host capability but not that the child
has the same authority to exercise it.

**Disposition:** survives, narrowed to the false equivalence/attestation
boundary. It does not support the broader proposition that the Codex child is
unsandboxed.

### F4 — survives, narrowed

**Strongest refutation.** The zero telemetry did not itself cause this run's
review stall. Coordinator limits are optional:
`run_feature.py:153-178`, `_coordinator_limits`, returns `None` when the
dispatch does not carry explicit authority. Token-slope enforcement and its
use of telemetry are guarded by `if limits is not None` at
`run_feature.py:1195-1264`. The current run had no such limits, so no
operator-authorized token stop was bypassed in this observation window.

**Why the refutation does not defeat the candidate.** The machine-emitted
usage and persisted telemetry disagree exactly. The terminal event in
`fr_0a8feb07a847488ea910a0ec5a2a99d7-COORDINATOR-drive-migrated-2d5133f1e4487e32-feature_coordinator-10-1.stdout.jsonl`
(SHA-256
`fe2359d439808605f3f83b2399c1db5fd25acae778afabfbbdf24c26d0105e06`)
has `/type="turn.completed"`,
`/usage/input_tokens=13023321`,
`/usage/cached_input_tokens=12185856`, and
`/usage/output_tokens=82501`. Its sibling output
`...feature_coordinator-10-1.output.json` (SHA-256
`09ad0a08b89d1b9bf7bc3a6bbbd50a130736bb779f9e581e2699ebb1aef705fa`)
instead has `/telemetry/input_tokens=0`,
`/telemetry/cached_input_tokens=0`, and `/telemetry/output_tokens=0`.

The source selects the weaker value: the result schema permits
`/telemetry=null` and accepts arbitrary nonnegative values at
`feature-coordinator-result.schema.json:60-69`;
`run_feature.py:1065-1072` reconstructs context usage only from model result
telemetry, while `:1211-1248` converts absent telemetry to zero and uses that
value when limits exist. `run_exec.py:685-734` reads stdout events only to
validate terminal protocol and does not persist the terminal usage as the
coordinator metric consumed by `run_feature.py`.

**Disposition:** survives, narrowed to inaccurate durable usage accounting and
an invalid input to configured token-slope enforcement. It is not established
as the cause of the current stall, and the absence of an unauthorized hard
limit is not a defect.

### F5 — survives, narrowed

**Strongest refutation.** The in-window evidence demonstrates one immutable
legacy command failure, not that every harness subprocess or every test uses a
drifting interpreter. The relevant ledger field is closure 004
`/closure_test/commands/0`:
`TMPDIR=/private/tmp PYTHONDONTWRITEBYTECODE=1 python3 -m pytest ...`.
The reviewer output
`fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-targeted_review-l1_l2_contract_boundary_reviewer-33-1.output.json`
(SHA-256
`767a83ed38f391f3e5aa30cd86509955028aac48ac2f4192034c6044924cf42a`)
states only at `/evidence/0` that ambient Python 3.14 lacked pytest and that
the same node passed under the available certification interpreter.

**Why the refutation does not defeat the candidate.** The controller contract
does not bind command interpreter identity even for newly recorded closure
tests. `review_closure.py:760-804`, `record_test`, accepts `commands` as
nonempty strings and records the first string verbatim in the assertion map;
`:841-853` persists the same strings in the closure ledger. The JSON schema at
`schemas/closure-test-result.schema.json:5-12` likewise defines each command
only as a nonempty string. Legacy backfill at
`review_closure.py:1027-1038` reuses the existing strings without interpreter
normalization. The observed failure is therefore an instance of a source-visible
unbound command identity, even though the evidence does not show universal
interpreter drift.

**Disposition:** survives, narrowed to closure-test command identity and the
recorded legacy instance. It is not a claim that all harness-launched Python
processes use the wrong interpreter.

### F6 — survives, narrowed

**Strongest refutation.** The evidence does not prove that a descendant
survived the operator pause. PID 72376 is no longer present, and the parent
operation explicitly terminated the orphaned child processes. A receipt left
at `running` is evidence of interrupted reconciliation, not by itself proof of
a currently live orphan.

**Why the refutation does not defeat the candidate.** The durable state and
source still prove an uncovered interruption boundary. Immutable receipt
`fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-fix-code_fixer-35-1.receipt.json`
(SHA-256
`92c29fe1f2d34d14a5f238029ac7ac3a7fb41bea9aff51ebfcefdc67992fc28a`)
has `/status="running"`, `/state_revision=3`, `/pid=72376`,
`/process_group_id=72376`, and no `/completed_at`; its declared
`.exit.json` and `.output.json` are absent. Ledger revision 188 retains
closure 005 `/attempts/5/status="running"`.

`run_exec.py:1258-1301` terminates the fingerprint-checked process group only
inside the wall-timeout branch. The polling and `process.wait()` region has no
exception-finalization path. Recovery at `run_exec.py:1163-1192` can reconcile
a later invocation, but it does not make the interrupted call persist terminal
state when the interruption occurs. `supervised_child.py:14-78` blocks in
`subprocess.run` and writes the exit record only after that call returns.

**Disposition:** survives, narrowed to interruption-time owned-group cleanup
and durable receipt reconciliation. A presently live descendant is not
claimed.

### F8 — rejected

**Strongest refutation.** The schema uses the same lexical property name in
two structurally distinct scopes, but it does not use one field for two
controller decisions. At
`closure-targeted-review-result.schema.json:5-12`, top-level `/status` has
domain `passed|blocked`; each `/findings/*/status` has domain
`fixed|not_fixed|regression`. JSON scope and disjoint enum domains make the two
fields mechanically distinguishable.

The controller preserves that distinction. `closure_driver.py:471-496` derives
`finding_statuses` exclusively from `/findings/*/status` and does not map the
top-level field. `review_closure.py:1640-1705`, `record_review`, routes closure
state exclusively from that per-fingerprint map. The two cited outputs
therefore demonstrate legal orthogonal states rather than a routing ambiguity:

- `...targeted_review-l1_l2_contract_boundary_reviewer-33-1.output.json`
  has `/status="blocked"` and `/findings/0/status="fixed"`;
- `...targeted_review-security_privacy_destructive_behavior_reviewer-34-1.output.json`
  (SHA-256
  `b4a6fc0200aec3b4629efb3e92c71f6f9c7b852aa14e06250583e4f445a51aee`)
  has `/status="passed"` and `/findings/0/status="not_fixed"`.

The ledger closed the former and rejected the latter exactly according to
finding disposition. No incorrect route, rejected schema-valid result,
recovery failure, or measured cost is bound to the shared spelling.

**Disposition:** rejected as a failure mode. The evidence establishes redundant
or potentially confusing naming, but not a material harness defect in the
bounded run.

### N1 — rejected

**Strongest refutation.** The cache messages are non-terminal diagnostics and
the cited receipts completed successfully. For example,
`...targeted_review-l1_l2_contract_boundary_reviewer-33-1.receipt.json`
(SHA-256
`f0b033e66ca9565ac39e9ad7b1656402ad888014a99744a7743d249f77344ef1`)
has `/status="succeeded"` and `/terminal_cause/class="none"` despite repeated
`supports_reasoning_summaries` lines in `/diagnostics`.

That behavior is intentional in source. `run_exec.py:617-662`,
`classify_terminal_cause`, explicitly normalizes terminal causes without
interpreting stderr diagnostics. `run_exec.py:674-682`,
`_stderr_diagnostics`, captures nonempty stderr lines, and
`:805-836`, `_finalize_receipt`, persists them independently from the terminal
cause. Thus the source does not misclassify these warnings as call failures.

Challenge 1 itself establishes no correctness failure and no exact latency,
token, retry, or recovery consequence attributable to the cache messages.
Repeated diagnostic volume alone does not meet the stated material-failure
threshold.

**Disposition:** rejected from the finite failure-mode list. This conclusion
does not dispute the external compatibility warning; it rejects only the claim
that the bounded evidence elevates it to a material harness failure mode.

### N2 — survives, narrowed

**Strongest refutation.** Executable behavior is internally consistent:
`closure_driver.py:108-146`, `continue_without_bound_program`, returns
unbound `next_ready`, `retry_fix`, and `redesign` routes to the outer
coordinator, and `:247-258`, `continue_routine`, calls it when no program is
bound. `tests/test_closure_driver.py:33-72` distinguishes lawful unbound retry
from an unknown route. No post-v14 recurrence is evidenced.

The candidate also overstates the documentation scope. No contradictory
same-closure rule was located in `SKILL.md`. The contradiction is confined to
two normative references:

- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/references/protocol.md:160-163`
  says a missing same-closure retry or redesign program is
  `routine_program_missing`;
- `/Users/kirillzaslavsky/Documents/harness_labs/skills/codex/implement-v13-codex/references/phase-contracts.md:125-130`
  says absence of the same continuation program is a deterministic blocker.

Those statements directly contradict the current function and regression
test, so the source-of-truth conflict remains even though runtime behavior is
fixed.

**Disposition:** survives, narrowed to normative contract drift in
`references/protocol.md` and `references/phase-contracts.md`. It is not a new
live runtime recurrence and is not established in `SKILL.md`.

## Finite result

Exactly five candidates survive adversarial review:

1. `F3` — survives, narrowed.
2. `F4` — survives, narrowed.
3. `F5` — survives, narrowed.
4. `F6` — survives, narrowed.
5. `N2` — survives, narrowed.

Exactly two candidates are rejected:

1. `F8` — no material ambiguity or routing failure established.
2. `N1` — non-terminal external diagnostics have no established material
   consequence in the bounded evidence.

No candidate collapses into another candidate.

## Scope audit

- Dispositioned IDs: exactly `F3`, `F4`, `F5`, `F6`, `F8`, `N1`, and `N2`.
- New IDs introduced: zero.
- Candidate IDs split or renamed: zero.
- New failure modes implied or named: zero.
- New remediation concepts proposed: zero.
- Files written: only
  `/Users/kirillzaslavsky/Documents/harness_labs/docs/development/implement-v13-codex-post-remediation-failure-challenge-2.md`.
