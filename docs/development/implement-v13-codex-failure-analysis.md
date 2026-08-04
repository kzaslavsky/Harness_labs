# implement-v13-codex observed failure analysis

Status: evidence-bound analysis of queue run `qr_405df6b197f24c7fbd2e157278458e15`, feature run `fr_0a8feb07a847488ea910a0ec5a2a99d7`

## Scope and method

This report analyzes the failures observed while `implement-v13-codex` developed `testing_harness`. It does not claim that every listed defect exists in every v13 run. Durable queue, checkpoint, closure-ledger, invocation receipt, stdout/stderr, and source artifacts are authoritative; chat recollections are not.

Evidence roots:

- Base worktree: `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base`
- Feature worktree: `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/.claude/worktrees/impl-codex-fr_0a8feb07a847488ea910a0ec5a2a99d7`
- Run artifacts: `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/handoff/serial-runs/qr_405df6b197f24c7fbd2e157278458e15/fr_0a8feb07a847488ea910a0ec5a2a99d7`
- Serial queue: `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/docs/development/serial_implementation_queue.json`
- Checkpoint: `/Users/kirillzaslavsky/.codex/worktrees/harness-labs-testing-base/.claude/worktrees/impl-codex-fr_0a8feb07a847488ea910a0ec5a2a99d7/docs/development/current_implementation_checkpoint.json`
- Installed controller source: `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex`

Unless a paragraph says **Inference**, it reports a directly observed fact. Confidence describes attribution, not severity.

## Executive finding

The run did not fail because one implementation worker was too weak. It reached review, then accumulated interacting control-plane failures: an invalid response schema escaped preflight; deterministic API rejection was mislabeled as generic child failure and retried across models; review closure was serialized with expanding regression obligations; executable contracts contradicted one another but only prose classifications were compared; sandbox requirements were impossible in the actual nested environment; operator resolutions were accepted before their implementability was established; and the globally installed controller was hot-patched during a long-lived resumed run.

At the latest durable state, queue revision 23 and checkpoint revision 88 are blocked in `REVIEWING/fix`. The closure ledger is revision 124: 2 of 12 closures are closed, 4 are active or reopened, and 6 have not started. The transaction remains `prepared`. From transaction preparation (`2026-07-22T11:36:06Z`) to the latest blocker (`2026-07-23T02:07:08Z`) elapsed wall time is 14 h 31 m 02 s; approximately 11 h 58 m was under an active lease.

The 277 process receipts contain 269 successes and 8 failures. None has `timed_out=true`. There are 155 successful coordinator turns on one recovered thread; receipt intervals total about 3 h 32 m of coordinator model time. The last cumulative coordinator usage event reports 117,144,148 input tokens, 113,773,056 cached input tokens, and 521,165 output tokens. These values are cumulative for the resumed thread and must not be summed across turns.

Historical role cost is also material: 26 repair-designer calls consumed about 1 h 43 m of summed model runtime, and 18 code-fixer calls about 3 h 24 m. Those child durations can overlap, so they are agent-time rather than wall-time totals.

## Canonical claims

### C1 — The current blocker is a response-schema defect that local preflight did not detect

**Failure mode.** Three supplemental adversarial-classification invocations (attempts 14, 15, and 16) were sent to the API with an invalid strict output schema. Each failed before the role produced a result.

**Root cause.** `schemas/closure-test-result.schema.json` declares `effect_contract` in `properties` but omits it from top-level `required`. The API rejects strict object schemas unless every declared property is required. The controller's `_check_codex_response_schema` only checks explicit types for `const`/`enum` and `items` for arrays; it does not recursively enforce `required == properties.keys()`. `_preflight` therefore accepts a schema that the API rejects.

**Impact.** The active closure could not advance, three attempts were consumed, and the queue blocked although no model reasoning occurred.

**Durable evidence.** The attempt 14/15/16 stdout JSONL files under the run root contain HTTP 400 `invalid_json_schema`, specifically “required ... missing `effect_contract`.” Their receipts are:

- `fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-closure_test-l1_l2_contract_boundary_reviewer-1-14.receipt.json`
- `fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-closure_test-l1_l2_contract_boundary_reviewer-1-15.receipt.json`
- `fr_0a8feb07a847488ea910a0ec5a2a99d7-REVIEWING-closure_test-l1_l2_contract_boundary_reviewer-1-16.receipt.json`

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/schemas/closure-test-result.schema.json:5-14`; `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:207-226,288-325`.

**Fix.** Make `effect_contract` required (nullable if semantic absence must remain expressible). Add a recursive strict-response-schema validator before an attempt identity is created, including complete `required` sets and API-required object restrictions. Add a fixture that runs every canonical schema through the same response-format validation path used by production.

**Confidence:** high.

### C2 — Terminal-error classification hid the actionable API error and caused futile model retries

**Failure mode.** Durable blocker text says `child_invocation_failure` and emphasizes missing `turn.completed`; the controller then retried Sol-medium twice and Luna-medium once. All three received the same schema HTTP 400.

**Root cause.** `_terminal_validation_errors` appends generic symptoms—exit code, missing thread/turn, terminal event, and missing output—but does not promote the structured API error event into a deterministic failure class. Model-cache stderr on attempts 15/16 (`missing field supports_reasoning_summaries`) was incidental: attempt 14 had no such warning and failed identically.

**Impact.** Two retries and a model switch could not alter the result and distracted diagnosis toward model/cache health.

**Durable evidence.** The three receipts/stdout files above; attempts 15/16 corresponding stderr logs; queue revision 23 blocker.

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:362-411`.

**Fix.** Parse terminal `error` events into structured classes such as `response_schema_transport_rejected`, store the server message in the receipt, and mark deterministic 4xx schema failures non-retryable across models. Keep cache warnings in a separate diagnostic field.

**Confidence:** high.

### C3 — A long-lived model coordinator amplified otherwise deterministic work

**Failure mode.** The run launched 155 coordinator turns on the same thread. The controller usually alternated a coordinator turn with one brokered child invocation. Context and token use grew throughout review.

**Root cause.** `run_feature.py` uses a model to choose every next action, persists a single contiguous thread identity, and loops from coordinator action to brokered child to another coordinator turn. It has no hard coordinator-turn or context-growth limit. Batching permits only two or three independent roles, and more than one repair action per batch is forbidden. Although the normal first closure attempt can run through `closure_driver.py`, every rejection, redesign, direct retry, or escalation returns to the coordinator.

**Impact.** Coordinator receipt intervals total about 3 h 32 m. The last cumulative usage event is 117.1 M input tokens. This directly increased wall time because most review work was serialized around coordinator turns.

**Durable evidence.** 155 `COORDINATOR-drive-feature_coordinator` receipts and their stdout JSONL files; final turn usage event; same `thread_id` across recovered receipts.

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:159-238,308-328,468-497,500-620`.

**Inference.** Not all 117.1 M input tokens were uncached or billable, but cumulative growth is a reliable indicator of a very large retained coordination context.

**Fix.** Move routine transitions, retry classification, closure routing, and artifact construction into a JSON-defined deterministic phase engine. Invoke a coordinator only for bounded judgment. Roll coordinator context at phase/closure boundaries using a hashed summary receipt; cap turns and context slope; emit compaction/rollover telemetry.

**Confidence:** high for the architecture and measured cost; medium for the exact fraction of wall time avoidable.

### C4 — Serial closure processing plus “rerun every prior closure” created regression amplification and starvation

**Failure mode.** Review triage created 12 closure groups. Only 2 are closed after 14.5 hours. Closure 4/5/6 were reopened or remain ready for fix, while closures 7-12 have no attempts.

**Root cause.** The controller permits only one finding-level repair action at a time. For every targeted review, `record_review` requires results for every previously closed closure and reopens any whose check is not true. As shared controller code changed, later repairs repeatedly regressed earlier closures, growing each subsequent review's obligations.

**Impact.** 18 fixer calls and repeated reviewer passes did not finish half of the queue. Work on later independent findings was starved.

**Durable evidence.** `review-triage.v1.json`; closure-ledger revision 124, including closure states and regression checks; fixer and targeted-review receipts.

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:308-328`; `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:660-697`.

**Fix.** Build a dependency graph between findings and code surfaces. Permit an atomic multi-closure repair only when dependency is explicit and reviewers remain independent. Run a deterministic incremental shared regression suite once per repair batch, then independently disposition affected findings. Add starvation limits and reorder unaffected closures around a repeatedly failing architectural cluster.

**Confidence:** high.

### C5 — Prose effect-contract comparison did not detect contradictory executable requirements

**Failure mode.** Closure 3 required both pre-persistence byte identity on failure and immutable tests proving that failure checkpoint, blocked queue, summary, and event state were persisted. Identical input could not satisfy both. Four attempts and an operator resolution were needed.

**Root cause.** The closure system compares author/designer/reviewer classifications in a canonical effect-contract object, but it does not bind those classifications to actual test assertions or execute a satisfiability check across immutable tests. Consistent prose labels therefore passed even when executable effects contradicted one another.

**Impact.** Multiple expensive fixer/design cycles, two blocker episodes, and operator intervention.

**Durable evidence.** Queue blocked histories for attempts 1 and 2; closure 3 attempt/design histories in the ledger; operator resolution selecting controller-owned failure persistence.

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:330-400`.

**Fix.** Require each immutable assertion to map to a canonical lifecycle effect and verify the mapping against real test execution before a fixer starts. Run a contradiction matrix over permitted/forbidden persistent effects. If no assignment satisfies all tests, block once with concrete mutually exclusive alternatives.

**Confidence:** high.

### C6 — Canonical-schema byte identity was confused with API-transport compatibility

**Failure mode.** An earlier closure-3 resume failed because canonical repair schemas used `uniqueItems`, which the response API rejected, while the controller rejected an API-compatible copy because it was not byte-identical to the canonical file.

**Root cause.** One artifact was treated simultaneously as the normative semantic schema and the provider-specific transport schema. Local JSON Schema validity was assumed to imply provider compatibility.

**Impact.** Queue attempt 2 blocked before productive repair work; the installed schemas had to be modified.

**Durable evidence.** Queue attempt-2 blocker and its resolution evidence; checkpoint resolution history recording removal of `uniqueItems` and the 120-test installed-skill suite.

**Source binding.** Canonical schema guards and preflight are in `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:288-325`; canonical schemas are under `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/schemas/`.

**Fix.** Keep one normative source schema and compile it deterministically into a provider transport schema. Record and validate both hashes. Test all generated transport schemas against the provider dialect before dispatch; never accept ad hoc child-authored copies.

**Confidence:** high.

### C7 — Historical rejection counters consumed a newly authorized repair budget

**Failure mode.** After an operator resolved closure 6's governing contract, preserved pre-resolution design rejections immediately exhausted the supposedly fresh budget and caused another blocker.

**Root cause.** Historical audit counts and post-resolution budget counts were initially the same counter. Resolution changed the contract but did not rebase rejection accounting.

**Impact.** Attempt 4 produced an immediate failed resume instead of one new design opportunity.

**Durable evidence.** Queue attempt-4 blocker and resolution; closure 6 `budget_recovery_history` and preserved rejection history.

**Source binding.** The repaired implementation is `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:233-251,547-602`.

**Fix.** The current baseline fields are the correct direction. Make them a ledger invariant and migration requirement; add tests proving history remains append-only while post-resolution counts start at zero exactly once.

**Confidence:** high. **Status:** fixed in the currently installed source, but observed in this run.

### C8 — Required nested Seatbelt enforcement was incompatible with the managed execution environment

**Failure mode.** Closure 6 required mandatory subprocess sandbox enforcement, but actual nested macOS Seatbelt launch failed under the outer managed sandbox (`rc=71`). Focused tests could pass via monkeypatch, while production-real enforcement did not.

**Root cause.** Environment capability was not tested at orient/planning time. The feature contract placed enforcement inside a role process even though the already-sandboxed host did not allow the required nested mechanism.

**Impact.** Five design rejections and two rejected fixes on closure 6; apparent focused-test success did not constitute runtime proof.

**Durable evidence.** Closure 6 design-review and targeted-review evidence in the ledger; corresponding designer, fixer, and reviewer outputs.

**Source binding.** The controller launches children with an outer Codex sandbox at `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:650-660`.

**Fix.** Probe the actual enforcement capability once before implementation. Put mandatory OS sandboxing in a controller/host-owned broker rather than nesting it inside a constrained child. If unavailable, fail at `orient` with an external-capability blocker. Certification must exercise the real enforcement path; monkeypatched launchers may localize logic but cannot satisfy the gate.

**Confidence:** high.

### C9 — Operator resolutions were accepted before subject and minting-path implementability were proven

**Failure mode.** Closure 1 blocked three times: metadata-fixture authority did not govern the active marker fixture; marker attestation authority named what was allowed but not a lawful controller-only minting path; exact marker authority still lacked a usable provenance channel. The final resolution added a controller-owned anonymous-pipe fixture contract.

**Root cause.** Resolution validation checked structured values but not the full dataflow from the active immutable test node through controller minting to role consumption. Early resolutions could therefore be valid-looking yet non-implementable or bound to the wrong subject.

**Impact.** Attempts 5-7 each blocked after additional designs instead of rejecting the insufficient authorization at resume time.

**Durable evidence.** Queue attempts 5, 6, and 7 blocker histories; closure 1 contract-resolution and design-rejection histories.

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:430-546`.

**Fix.** Resolution validation must prove: exact active closure/test identity; controller ownership of minting; non-caller-selectability; transport, lifetime, and consumption path; fail-closed behavior; and an executable test of the whole channel. Reject incomplete authority before resuming.

**Confidence:** high.

### C10 — Repository-specific operator policy was hard-coded into a globally reusable skill

**Failure mode.** The installed generic harness now contains exact `testing_harness` source paths, test node IDs, mutation bytes, and fixture transport rules for this one run.

**Root cause.** Live recovery encoded repository-specific authorization directly in `review_closure.py` rather than in a run-owned, source-hashed resolution artifact interpreted by generic machinery.

**Impact.** The shared skill became coupled to one repository and risked changing behavior for unrelated runs. It also made a live blocked run depend on global package mutation.

**Durable evidence.** Current installed source and checkpoint resolution history.

**Source binding.** `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:441-546`.

**Fix.** Define a generic resolution schema and store concrete profiles in the run artifact, bound to repository identity, test/source hashes, operator authorization hash, and active closure. Generic controller code should validate properties, not know project filenames or bytes.

**Confidence:** high.

### C11 — Reviewer environment mismatch caused a false rejection

**Failure mode.** Closure 1's first repair was rejected because the independent read-only reviewer lacked a writable temporary directory, not because the repair behavior failed. A later reviewer-specific temp profile allowed the same class of checks to proceed.

**Root cause.** Reviewer sandbox permissions did not include deterministic test-runner scratch requirements, and preflight did not check them.

**Impact.** One repair attempt and additional review/design work were consumed for an environment failure.

**Durable evidence.** Closure 1 attempt 1/2 histories and reviewer evidence; queue/checkpoint resolution permitting reviewer temp capability.

**Source binding.** Writable roots and child environment are preflighted in `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:288-325`, but test-runner temp capability is not semantically probed there.

**Fix.** Define per-role ephemeral scratch as a first-class permission distinct from repository writes. Preflight the exact test command's temp/cache requirements and record the granted scratch root in the receipt.

**Confidence:** high.

### C12 — Model policy was expensive, but model quality was not the primary cause

**Failure mode.** Historical repair-designer calls used Luna-high: 26 calls consumed about 1 h 43 m summed runtime. Eighteen Luna-high fixer calls consumed about 3 h 24 m. Many process-successful outputs were later rejected by independent review.

**Root cause.** The old role matrix allocated a high-reasoning implementation model to repeated design work, while the state machine allowed repeated redesigns around unsatisfied contracts. Process success was treated as transport success, not quality proof.

**Impact.** High agent time and latency. However, the strongest blockers were deterministic contract, schema, state, and environment defects; switching models alone could not fix them.

**Durable evidence.** Historical designer/fixer receipts and their model/reasoning fields; closure rejection histories. The current prompt has since changed designers to Terra-medium.

**Source binding.** Current policy text: `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:219-223`; current enforcement keeps Luna-high only for implementation workers/fixers: `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_exec.py:663-687`.

**Inference.** Terra-medium is likely cheaper/faster for bounded design, but this run contains no controlled Luna-vs-Terra comparison proving equal or better design quality.

**Fix.** Keep designers on Terra-medium, retain independent Sol review, and benchmark equivalent closures on acceptance rate, rework, tokens, and runtime. Stop redesign before model invocation when deterministic contradiction or environment gates fail.

**Confidence:** high that model choice was not the root blocker; medium on the expected benefit of Terra-medium.

### C13 — Repair gates ran too late for high-blast-radius controller changes

**Failure mode.** Rejected repairs included global file reads, output bounds checked only after unbounded communication, caller-selectable profile logic, incomplete process evidence, and regressions in closures 1/4/5. Focused fixer tests passed in some cases before the originating reviewer rejected the design or runtime properties.

**Root cause.** Fixers modified a shared controller surface, but deterministic static/policy/runtime checks were not required before the expensive targeted reviewer. The original implementation was partitioned into three groups—so it was not a single 40-minute worker—but review repairs were still narrow attempts against a tightly coupled shared module.

**Impact.** Eighteen fixer calls and repeated regression churn.

**Durable evidence.** `implementation-partition.v1.json`; closure 1/4/5/6 attempt and targeted-review histories; fixer outputs.

**Source binding.** Repair serialization and regression reopening are at `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:308-328` and `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/review_closure.py:660-697`.

**Fix.** Before targeted model review, run deterministic impact checks: forbidden reads/selectors, pre-communication bounds, process-evidence schema, production-real sandbox smoke, and all tests mapped to affected closure dependencies. Prefer smaller stable controller interfaces so repair write sets are disjoint.

**Confidence:** high.

### C14 — The control plane was not frozen across blocked resumes

**Failure mode.** The globally installed skill, schemas, model policy, resolution logic, and tests were modified between attempts. New controller processes resumed the same coordinator thread but loaded current installed code. Historical receipts show Luna-high designers while current continuation prompts override old memory with Terra-medium; multiple blocker resolutions cite newly installed test-suite counts.

**Root cause.** Dispatch binds planning inputs but does not freeze a complete versioned controller package for the run or require an explicit migration between controller revisions. Per-invocation prompt/schema snapshots provide local provenance but do not establish one coherent control-plane version across the run.

**Impact.** Attempt semantics changed during recovery, old coordinator memory coexisted with new code/prompt policy, and reproduction now requires reconstructing multiple global skill states.

**Durable evidence.** Historical receipts and prompt/schema snapshots; checkpoint resolution history; current installed model-policy source; same-thread recovery receipts.

**Source binding.** Continuation reads the installed package and overrides model policy at `/Users/kirillzaslavsky/.codex/skills/implement-v13-codex/scripts/run_feature.py:159-238`; recovery requires the same thread at lines 468-497.

**Fix.** Snapshot the complete controller package at dispatch and record its digest in queue, checkpoint, transaction, and every receipt. Resume the exact snapshot. If a controller fix is needed, perform an explicit migration with old/new hashes, schema migration proof, state invariants, and a fresh bounded coordinator context.

**Confidence:** high.

## Claims explicitly not established

1. **A four-hour execution ceiling killed this run:** not established. All 277 receipts have `timed_out=false`. Observed child specifications use 3,600, 7,200, or 43,200 second limits; none supplies evidence of a four-hour controller ceiling.
2. **PTY cleanup killed this run:** not established. The run artifacts contain no PTY ownership/cleanup event and no terminal receipt attributable to PTY loss.
3. **Model-cache corruption caused the current blocker:** refuted by direct evidence. The same API schema rejection occurred without the cache warning on attempt 14.
4. **One monolithic 40-minute implementation worker caused this run:** refuted for this feature. `implementation-partition.v1.json` has three implementation groups, and the three implementation-worker receipts total about 28 minutes of agent time. The dominant cost arose in review/repair coordination.
5. **Exact compaction count:** unavailable. Receipts expose cumulative usage but no compaction event or counter. A compaction count cannot be source-bound from this run.

## Recommended remediation order

1. Fix C1 and C2 first: strict schema preflight and deterministic terminal taxonomy stop the current false blocker and futile retry pattern.
2. Freeze/migrate the controller package (C14) before any further resume so evidence remains reproducible.
3. Move capability and contract satisfiability checks ahead of model work (C5, C8, C9, C11).
4. Replace repository-specific controller policy with run-owned generic resolution artifacts (C10).
5. Rework closure scheduling and early deterministic gates (C3, C4, C13).
6. Evaluate the Terra-medium designer policy only after equivalent correctness gates are held constant (C12).

## Canonical claim index

1. **C1:** strict response-schema preflight gap
2. **C2:** terminal-error misclassification and futile model retry
3. **C3:** long-lived coordinator/context amplification
4. **C4:** serial closure regression amplification and starvation
5. **C5:** executable contract contradiction escaped prose effect gates
6. **C6:** canonical-schema/transport-dialect conflation
7. **C7:** post-resolution rejection-budget baseline defect
8. **C8:** nested sandbox capability incompatibility
9. **C9:** operator resolution subject/minting-path incompleteness
10. **C10:** repository-specific recovery policy in global skill
11. **C11:** reviewer scratch-environment mismatch
12. **C12:** expensive historical model policy, not primary root cause
13. **C13:** late repair gates and shared-controller blast radius
14. **C14:** unfrozen control plane across resume

## Adversarial review of canonical claims C1-C14

Scope: this review tests only the canonical claims above. It does not add or investigate other failure modes. The review used the cited installed sources, queue revision 23, checkpoint revision 88, closure-ledger revision 124, and the immutable per-invocation receipts and logs under the report's run-artifact root. Verdicts mean:

- **Claim survives:** the attempted refutation did not materially weaken the canonical claim.
- **Partially refuted / narrowed:** the central observation survives, but its scope, causality, wording, or source binding is materially too broad.
- **Refuted:** durable evidence contradicts the canonical claim.

Verdict count: **6 survive, 8 partially refuted or narrowed, 0 refuted**.

### A-C1 — Claim survives

**Refutation attempted.** Determine whether the API failures could instead be model failures, whether `effect_contract` was absent from `properties`, or whether local preflight already enforced the provider's complete-`required` rule.

**Evidence.** The attempt-14, -15, and -16 stdout JSONL files each contain HTTP 400 `invalid_json_schema` with the exact missing key `effect_contract`; attempts 14/15 use `gpt-5.6-sol` and attempt 16 uses `gpt-5.6-luna`, so the response is model-independent. The three receipts are failed, `timed_out=false`, have no output hash, and record `thread.started`, `turn.started`, `error`, and `turn.failed`. The installed `schemas/closure-test-result.schema.json:5-14` lists `effect_contract` in `properties` but not in top-level `required`. The recursive walker at `scripts/run_exec.py:207-226` checks explicit types for `const`/`enum` and array `items`, but not complete object `required` sets; `_build_validator` calls that walker before dispatch at line 140. This is exactly the gap C1 describes.

**Conclusion.** No refutation succeeded. The current blocker is caused by a schema accepted by local preflight and rejected by the API before role reasoning.

**What would falsify this conclusion.** A receipt showing that the locally validated schema included top-level `effect_contract` in `required`, or a provider response showing a different terminal cause for any of attempts 14-16, would falsify it.

### A-C2 — Partially refuted / narrowed

**Refutation attempted.** Test whether the actionable API error was actually lost and whether a model retry could plausibly change the result.

**Evidence.** `scripts/run_exec.py:362-411` reduces the terminal state to generic validation strings (`exit code 1`, `missing turn.completed`, `terminal error event`, `missing final output`). Queue revision 23 correspondingly records blocker class `child_invocation_failure` and describes only missing `turn.completed`, followed by two Sol attempts and one Luna attempt. All three return the same 400 schema message. However, the API error was not erased: each receipt hashes its stdout, records the `error`/`turn.failed` event types, and points to a stdout file containing the complete server message. Thus the controller failed to promote and route the error, but durable evidence did preserve it.

**Conclusion.** C2 survives as a terminal-taxonomy and retry-policy defect. The word “hid” should be read narrowly as “hid from the blocker classification and retry decision,” not “removed from durable evidence.”

**What would falsify this conclusion.** A queue/blocker or machine-readable receipt field classifying these invocations as a non-retryable response-schema rejection before attempts 15/16, or evidence that attempts 15/16 used different accepted schemas, would falsify it.

### A-C3 — Partially refuted / narrowed

**Refutation attempted.** Verify the turn count, thread continuity, measured cost, controller limits, and whether the evidence proves the avoidable fraction of wall time.

**Evidence.** There are exactly 155 succeeded `COORDINATOR:drive:feature_coordinator` receipts, all on thread `019f899f-5446-7442-9c5b-e12dd025646f`. Their `running_at`-to-`completed_at` intervals sum to 12,555 seconds (3 h 29 m 15 s), close to the report's approximate 3 h 32 m. `scripts/run_feature.py:468-497` requires one contiguous thread, and lines 527-579 alternate coordinator output with brokered invocation. Lines 308-328 limit batches to two or three specs and only one finding-level repair action. No hard turn or context-growth limit appears in the cited loop. But receipts establish coordinator agent-time, not a counterfactual: they do not prove precisely how much of the total wall time a deterministic controller would have avoided, and some coordinator judgment was required.

**Conclusion.** The architectural amplification and measured cost survive. The assertion that this “directly increased wall time” is supportable for the serialized intervals, but the avoidable fraction remains an inference rather than a measured causal decomposition.

**What would falsify this conclusion.** Evidence of a hard enforced coordinator-turn/context-growth cap that fired in this run, multiple coordinator thread IDs, or a timeline proving coordinator intervals fully overlapped other critical-path work would falsify it.

### A-C4 — Partially refuted / narrowed

**Refutation attempted.** Check whether all prior closures really must be rerun, whether regressions are reopened, and whether those facts alone prove starvation of closures 7-12.

**Evidence.** `scripts/run_feature.py:325-327` forbids more than one finding-level repair action in a batch. `scripts/review_closure.py:672-685` requires a targeted review to provide a Boolean check for every *currently closed other closure* and reopens each false check as `ready_for_fix`. Ledger revision 124 has only closures 2 and 3 closed; closures 1, 4, 5, and 6 are active/reopened, while closures 7-12 remain `test_required`. The attempt histories show actual regression reopening, including closure 6 attempt 2 reopening closures 1, 4, and 5. The wording “rerun every prior closure” is slightly broader than the code: the set is every other closure whose status is `closed` at that review, not every historically encountered closure. The evidence also shows serial blocking in front of 7-12, but it does not isolate regression amplification from closure ordering, design contradictions, and retries as the sole cause of starvation.

**Conclusion.** The serial regression-amplification mechanism survives. Narrow the causal claim: it is a demonstrated contributor to starvation, not a uniquely measured cause, and its regression set is dynamically closed closures rather than all historical closures.

**What would falsify this conclusion.** A ledger transition showing two finding-level repairs advancing concurrently, a targeted review accepted without checks for all then-closed other closures, or evidence that closures 7-12 advanced while an earlier closure remained active would falsify it.

### A-C5 — Partially refuted / narrowed

**Refutation attempted.** Determine whether closure 3 itself contained mutually exclusive immutable assertions, or whether the contradiction arose between a repair design and separate required tests.

**Evidence.** Closure 3 attempt 1 was rejected because it persisted failed/blocked/summary state contrary to the then-approved pre-persistence byte-identity design. Attempt 2 satisfied that design but failed two required tests that demanded the failed checkpoint, blocked queue, and summary for the same malformed input. Attempt 3 confirmed the same conflict, and the operator resolution changed the governing contract to `controller_owned_failure_persistence`; attempt 4 then passed. The effect comparator at `scripts/review_closure.py:339-400` compares canonical effect-contract objects, not concrete assertions. This supports the missed executable contradiction. However, the contradiction was between the approved repair design and required immutable tests, not two assertions inside the original closure-test record itself. Also, only the first closure-3 blocker is the immutable-contract conflict; the subsequent closure-3 resume blocker is the distinct transport-schema incompatibility described by C6.

**Conclusion.** The escaped executable contradiction survives. Narrow C5's impact attribution from “two blocker episodes” to one immutable-contract blocker plus three rejected pre-resolution strategies; the second blocker belongs to C6.

**What would falsify this conclusion.** An executable assignment satisfying both byte identity and persisted failure state at the same canonical paths for the same input, or evidence that the effect gate executed and compared the actual assertions before attempt 1, would falsify it.

### A-C6 — Partially refuted / narrowed

**Refutation attempted.** Verify the queue blocker, the `uniqueItems` incompatibility, and the cited location of the canonical-byte guard.

**Evidence.** Queue blocked history explicitly records that canonical `repair-design-result.schema.json` contained `uniqueItems`, the response API rejected it, and an API-compatible copy was rejected by the byte guard. Installed tests now assert canonical repair schemas omit `uniqueItems` (`tests/test_run_exec.py:25` and `tests/test_review_closure.py:48-55`). The core claim therefore survives. The report's source binding is incomplete: the repair-design byte-identity guard is actually in `scripts/review_closure.py:786-794`; `scripts/run_exec.py:288-325` performs general preflight and only has a direct canonical byte guard for plan review at lines 300-311.

**Conclusion.** C6 is correct about semantic-schema/transport-dialect conflation, but its cited canonical-guard location should be corrected to `review_closure.py:786-794` for this failure.

**What would falsify this conclusion.** A historical schema snapshot without `uniqueItems`, an accepted provider invocation using that exact canonical hash, or queue evidence that the compatible copy was accepted would falsify it.

### A-C7 — Claim survives

**Refutation attempted.** Check whether the post-resolution blocker was instead another design rejection or a missing operator authorization.

**Evidence.** Queue blocked history states that the fresh closure-6 design was not recorded because retained pre-resolution rejections immediately exhausted the budget. Ledger revision 124 preserves four pre-resolution design rejections as a baseline and records one `budget_recovery_history` entry. Current `scripts/review_closure.py:339-364,547-580` separately computes post-resolution counts, stores `design_rejection_baseline`, and supports one migration path without deleting rejection provenance.

**Conclusion.** No refutation succeeded. This was an observed accounting defect and is fixed in the currently installed implementation, as C7 already states.

**What would falsify this conclusion.** A pre-fix ledger showing the fresh design was independently reviewed before the blocker, or a pre-fix budget computation already subtracting the resolution baseline, would falsify it.

### A-C8 — Partially refuted / narrowed

**Refutation attempted.** Separate direct runtime incompatibility from earlier design failures and check whether nested Seatbelt alone explains closure 6's full cost.

**Evidence.** Closure 6 attempt 2 review records permissive, strict, and generated policies returning Seatbelt `rc=71`, while malformed policy returned parse `rc=65`; it also records focused-test monkeypatching that removed the real `sandbox-exec` launch. The broker launches Codex children under an outer sandbox at `scripts/run_exec.py:649-660`, so the nested-enforcement incompatibility is source- and runtime-bound. But closure 6's five design rejections were not all caused by `rc=71`: their evidence concerns detached descendants, closure-4 base mutation, closure-1 marker/Git-metadata compatibility, and an attempt to modify immutable tests. Attempt 1 was rejected for global read authority, post-`communicate()` output checking, a pytest fallback, and invalid `self_check`; the direct `rc=71` proof appears in attempt 2.

**Conclusion.** The actual required enforcement path was incompatible with the managed environment, and capability preflight was absent. Narrow the impact: direct nested-sandbox incompatibility is proven for repair attempt 2; it does not explain all five preceding design rejections or every closure-6 failure.

**What would falsify this conclusion.** A production-real nested Seatbelt receipt with the approved policy returning success under the same outer sandbox, or evidence that orient performed the same probe and gated implementation, would falsify it.

### A-C9 — Claim survives

**Refutation attempted.** Check whether the repeated closure-1 resolutions were implementable when accepted and whether their validation bound the actual subject and minting channel.

**Evidence.** Queue attempts 5-7 successively block because the metadata authority does not govern the marker fixture, the marker authority lacks lawful provenance, and exact authority still lacks a controller-owned minting channel. Ledger revision 124 contains three resolution records progressing from metadata-node attestation, to marker-node attestation, to a fixture contract with an anonymous pipe. The current validator hard-codes exact subject fields at `scripts/review_closure.py:441-546`, but the first two accepted records validate declarations rather than execute end-to-end minting/consumption. The repeated blockers are therefore direct evidence that structured validity did not prove implementability.

**Conclusion.** No refutation succeeded. C9 accurately distinguishes authorization shape from a viable dataflow.

**What would falsify this conclusion.** End-to-end evidence that either of the first two accepted resolution forms could mint, transport, consume, and reject reuse without later authority, or a validator test that rejected those records before resume, would falsify it.

### A-C10 — Claim survives

**Refutation attempted.** Determine whether repository details lived only in run-owned resolution artifacts and the installed skill remained generic.

**Evidence.** The installed `scripts/review_closure.py:441-546` contains literal `testing_harness` source paths, exact pytest node IDs, `.git/config`, `.read-only-role-marker`, exact mutation bytes, and the anonymous-pipe fixture contract. Those values are also in the run ledger, but the global validator itself enumerates them and rejects any other profile. This is direct coupling of generic installed machinery to one repository's recovery.

**Conclusion.** No refutation succeeded. C10's scope and source binding are exact.

**What would falsify this conclusion.** A generic installed validator containing no repository path/node/bytes literals and loading a source-hashed run-owned profile through a generic schema would falsify it.

### A-C11 — Partially refuted / narrowed

**Refutation attempted.** Test whether the reviewer actually made a false substantive finding, or instead correctly refused certification because it could not execute tests.

**Evidence.** Closure 1 attempt 1 says static inspection aligned with the approved design, the fixer reported required tests passing, and the independent reviewer could not initialize pytest because its read-only sandbox exposed no writable temporary directory. The associated failed receipt uses `--sandbox read-only` with `writable_roots: []`. Attempt 2 preserved the implementation strategy, added a mutation-guarded review temp profile, and was accepted; later reviewer receipts show `--add-dir /private/tmp`. This proves environment-induced non-certification. It does not prove the reviewer falsely concluded the implementation was defective—the ledger explicitly says the reviewer could not certify it.

**Conclusion.** Replace “false rejection” with “environment-induced inability to certify that consumed a repair attempt.” The environment mismatch and wasted attempt survive; the stronger implication of a wrong substantive code verdict does not.

**What would falsify this conclusion.** Evidence that attempt 1's reviewer had a writable scratch directory and completed the exact tests, or that attempt 2 required a material implementation repair beyond reviewer scratch capability, would falsify it.

### A-C12 — Claim survives

**Refutation attempted.** Verify historical model allocation and cost, then look for evidence that Luna quality rather than deterministic harness defects caused the blocker.

**Evidence.** Twenty-six `repair_designer` receipts all use `gpt-5.6-luna` and sum to 6,155 seconds (1 h 42 m 35 s); 18 `code_fixer` receipts all use Luna and sum to 12,240 seconds (3 h 24 m). Their receipts report high reasoning where the role policy requires it. Many succeeded transports were independently rejected. Conversely, attempts 14-16 fail identically across Sol and Luna before reasoning, the closure-3 contradiction is executable, and closure-6 Seatbelt probes are environmental. Current `scripts/run_feature.py:219-223` routes repair designers to Terra-medium, while `scripts/run_exec.py:671-687` retains Luna-high for implementation/fix roles. There is no controlled Luna-versus-Terra quality experiment in this run.

**Conclusion.** No evidence supports refuting C12's bounded conclusion. Model policy was costly; model quality is not established as the primary cause, and Terra's expected benefit remains unproven pending a controlled comparison.

**What would falsify this conclusion.** A controlled equivalent-closure comparison showing Luna output quality was the dominant cause after schemas, contracts, environment, and gates were held constant would falsify it.

### A-C13 — Partially refuted / narrowed

**Refutation attempted.** Verify whether implementation was monolithic, whether deterministic checks preceded review, and whether all attributed churn follows from late gates.

**Evidence.** `implementation-partition.v1.json` has three groups, so C13 correctly rejects the earlier one-worker characterization. Closure 6 attempt 1 reports focused authority, immutable metadata, prior-closure, production-lifecycle, and full-suite tests passing before the originating reviewer found global read authority, non-streaming output enforcement, pytest fallback, and invalid `self_check`. Attempt 2 then found selector, real-sandbox, process-evidence, and prior-closure failures. This proves that the checks actually run before targeted review did not cover the decisive policy/runtime properties. `scripts/run_feature.py:308-328` and `scripts/review_closure.py:655-697` prove repair serialization and later regression reopening. However, the cited lines do not themselves enumerate an intended deterministic pre-review gate set, and the artifacts do not quantify how many of the 18 fixer calls each proposed early check would have prevented.

**Conclusion.** The late/missing high-blast-radius checks and shared-surface churn survive. Narrow the causal magnitude: the evidence identifies concrete preventable attempts but does not prove that every fixer call, or a specific fraction of total churn, came from late gates.

**What would falsify this conclusion.** A pre-targeted-review receipt showing all listed static, streaming-bound, selector, process-evidence, and production-real sandbox checks passed against the same implementation later rejected for those properties would falsify it.

### A-C14 — Claim survives

**Refutation attempted.** Determine whether dispatch froze a complete controller digest, whether resumes used that snapshot, and whether receipt provenance alone provided coherent run-level versioning.

**Evidence.** `dispatch.v1.json` has no complete controller-package digest/version field. Repair-designer receipts span three distinct `schema_source_sha256` values (`c7e4e984...`, `565714a9...`, and `fa6f4292...`) across the same feature run. All 155 coordinator receipts preserve one thread, while `scripts/run_feature.py:468-497` enforces same-thread recovery and lines 159-238 generate each continuation prompt from the currently installed package and current model-policy text. Receipts do preserve per-invocation prompt, schema, executable, child-spec, output, and log hashes; that is strong local provenance, but it does not freeze or hash all controller scripts as one package or declare a migration between the observed schema versions.

**Conclusion.** No refutation succeeded. C14 correctly distinguishes per-invocation provenance from a coherent, immutable or explicitly migrated run-level control plane.

**What would falsify this conclusion.** A dispatch-bound full-package digest used to load every resumed controller process, or explicit migration receipts binding old/new package hashes and ledger invariants at each observed schema change, would falsify it.

## Adjudication of C1-C14

This adjudication is limited to the original claims C1-C14 and the corresponding adversarial responses A-C1 through A-C14. It accepts no new failure mode and uses only evidence already cited by those claim pairs.

### J-C1

**Ruling:** Original claim upheld.

**Controlling evidence.** Attempts 14-16 each ended with HTTP 400 `invalid_json_schema`, explicitly naming missing `effect_contract`; the receipts span Sol and Luna, are not timeouts, and contain no role output. The cited canonical schema declares `effect_contract` under `properties` but omits it from top-level `required`, while the cited recursive preflight walker checks `const`/`enum` typing and array `items` but not complete object `required` sets.

**Corrected final claim wording.** The current blocker is a deterministic strict-response-schema defect: the canonical closure-test schema omitted `effect_contract` from `required`, local preflight accepted it, and the provider rejected it before any model reasoning.

**Accepted fix implication.** Require `effect_contract` in the canonical schema, recursively validate that strict object schemas require every declared property, and exercise every canonical response schema through the production response-format validation path before creating an attempt identity.

### J-C2

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** `run_exec.py:362-411` reduces the terminal result to generic symptoms, and queue revision 23 classifies the three identical provider rejections as `child_invocation_failure`, causing two same-model attempts and one alternate-model attempt. The complete API message nevertheless remains in hashed stdout and the receipts record `error` and `turn.failed` events.

**Corrected final claim wording.** Terminal-error classification hid the actionable provider rejection from blocker routing and retry policy, but did not erase it from durable evidence; this caused futile cross-model retries for a deterministic 4xx schema failure.

**Accepted fix implication.** Parse terminal error events into a structured, receipt-level `response_schema_transport_rejected` class, preserve the server message separately from incidental diagnostics, and make deterministic schema 4xx failures non-retryable across models.

### J-C3

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** The cited receipts establish 155 successful coordinator turns on one recovered thread and about 3 h 29 m of summed coordinator intervals. The cited controller source enforces contiguous same-thread recovery, alternates coordinator and brokered-child work, restricts repair batches, and contains no hard turn or context-growth limit. Those facts measure critical-path coordination cost, but they do not measure the counterfactual fraction avoidable under a deterministic controller.

**Corrected final claim wording.** A long-lived, repeatedly invoked coordinator measurably amplified coordination time and retained context during this run; the precise avoidable share of total wall time remains an inference because some coordinator judgment was necessary.

**Accepted fix implication.** Move routine transitions, retry classification, closure routing, and artifact construction into a deterministic phase engine; reserve model coordination for bounded judgment; roll context at phase or closure boundaries with hashed summaries; and enforce turn/context-slope limits with rollover telemetry.

### J-C4

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** `run_feature.py:308-328` allows only one finding-level repair action per batch. `review_closure.py:660-697` requires checks for every other closure that is currently `closed` and reopens failed checks. Ledger revision 124 shows actual reopenings, only closures 2 and 3 closed, closures 1 and 4-6 active or reopened, and closures 7-12 unstarted. This proves regression amplification and serial blocking, but not that they alone caused all starvation.

**Corrected final claim wording.** Serial finding repair plus mandatory rechecks of all then-closed peer closures created demonstrated regression amplification and materially contributed to starvation of later closures; the recheck set is dynamically closed closures, not every historically encountered closure, and other contradictions and retries also contributed.

**Accepted fix implication.** Build an explicit finding/code-surface dependency graph, permit atomic multi-closure repair only for proven dependencies with independent review, run one deterministic incremental regression suite per repair batch, and add starvation limits that allow unaffected closures to advance around a failing architectural cluster.

### J-C5

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** Closure 3 attempts show that the approved byte-identity repair design conflicted with immutable tests requiring persisted failure state for the same malformed input; operator selection of `controller_owned_failure_persistence` resolved that conflict and the next attempt passed. The cited effect comparator compares canonical prose-derived effect objects rather than executable assertions. The second closure-3 blocker concerned transport-schema incompatibility and belongs to C6.

**Corrected final claim wording.** The effect-contract gate failed to detect a contradiction between an approved repair design and required immutable test assertions. This caused three rejected pre-resolution strategies and one immutable-contract blocker; it did not cause the separate schema-transport blocker.

**Accepted fix implication.** Map each immutable assertion to canonical lifecycle effects, execute a satisfiability/contradiction matrix before invoking a fixer, and block once with concrete mutually exclusive alternatives when no permitted effect assignment satisfies the tests.

### J-C6

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** The queue's attempt-2 blocker records that the byte-identical canonical repair schema contained provider-rejected `uniqueItems`, while an API-compatible copy failed the canonical-byte guard. The core conflation is therefore established. The controlling byte guard for this repair path is `review_closure.py:786-794`, not the general `run_exec.py:288-325` preflight cited by the original claim.

**Corrected final claim wording.** The harness conflated the normative canonical repair schema with a provider-specific transport schema, and its byte-identity guard made the conflict irresolvable at dispatch; the repair-path guard is source-bound to `review_closure.py:786-794`.

**Accepted fix implication.** Maintain one normative source schema, compile it deterministically to a provider-dialect transport schema, bind and record both hashes, validate generated transport schemas before dispatch, and reject ad hoc child-authored copies.

### J-C7

**Ruling:** Original claim upheld.

**Controlling evidence.** The attempt-4 queue blocker states that retained pre-resolution design rejections immediately exhausted the new budget before a fresh design could be reviewed. Ledger revision 124 preserves the rejection history and budget-recovery record. Current cited source separates post-resolution counts with `design_rejection_baseline` and a one-time recovery path.

**Corrected final claim wording.** Historical design rejections were incorrectly charged against a newly operator-authorized post-resolution design budget, causing an immediate blocker; the current baseline mechanism fixes the observed defect while preserving audit history.

**Accepted fix implication.** Make design and fixer rejection baselines ledger invariants and migration requirements, with tests proving append-only historical provenance and a single zero-based post-resolution budget reset.

### J-C8

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** Closure 6 attempt 2 records real nested Seatbelt launches failing with `rc=71` under the outer Codex sandbox, while monkeypatched focused tests bypassed that production path; `run_exec.py:650-660` confirms the outer sandbox launch. Earlier design rejections and attempt 1 concerned additional authority, lifecycle, output-bound, selector, and evidence defects, so nested Seatbelt does not explain all closure-6 cost.

**Corrected final claim wording.** The production-real nested Seatbelt path required by closure 6 was incompatible with the managed outer sandbox, and the harness lacked an early capability gate. This incompatibility is directly proven for repair attempt 2, not as the cause of all preceding closure-6 design and repair failures.

**Accepted fix implication.** Probe the exact enforcement capability during orient/planning, place mandatory OS sandboxing in a controller/host-owned broker rather than a constrained child when supported, fail early with an external-capability blocker when unavailable, and never accept monkeypatched launchers as certification evidence.

### J-C9

**Ruling:** Original claim upheld.

**Controlling evidence.** Queue attempts 5-7 successively show authority bound to the wrong fixture, authority without lawful provenance, and authority without a concrete controller-owned minting channel. Ledger resolution history progresses through those incomplete forms to an anonymous-pipe fixture channel. The cited validator checked exact declared fields but did not prove end-to-end minting, transport, consumption, and fail-closed behavior before resume.

**Corrected final claim wording.** Operator resolutions were accepted on structured declaration alone before the active subject and complete controller-owned minting/consumption dataflow were proven implementable, producing three avoidable blocked resumes.

**Accepted fix implication.** Validate exact active closure/test identity, controller-only minting, non-caller-selectability, transport and lifetime, consumption, reuse rejection, and end-to-end fail-closed execution before authorizing resume.

### J-C10

**Ruling:** Original claim upheld.

**Controlling evidence.** `review_closure.py:441-546` contains literal `testing_harness` paths, exact pytest nodes, mutation targets and bytes, and the anonymous-pipe fixture contract. These values also exist in run artifacts, but the globally installed validator itself enumerates and constrains the repository-specific profiles.

**Corrected final claim wording.** Repository-specific recovery policy was hard-coded into a globally reusable installed skill, coupling unrelated runs to `testing_harness` details and making live recovery depend on global package mutation.

**Accepted fix implication.** Define a generic resolution schema and load concrete, source-hashed profiles from run-owned artifacts bound to repository, closure, test, authorization, and source identities; generic controller code should validate properties rather than project literals.

### J-C11

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** Closure 1 attempt 1 records static alignment and fixer test success, but the independent read-only reviewer could not initialize pytest because it lacked writable temporary storage. Attempt 2 preserved the implementation strategy, granted a mutation-guarded review temp profile, and was accepted. The reviewer therefore correctly withheld certification; it did not falsely find a substantive implementation defect.

**Corrected final claim wording.** A reviewer scratch-environment mismatch caused an environment-induced inability to certify and consumed a repair attempt; it was not a false substantive code verdict.

**Accepted fix implication.** Model ephemeral reviewer scratch as a first-class permission distinct from repository writes, preflight the exact test runner's temp/cache needs, and record the granted scratch root in the receipt.

### J-C12

**Ruling:** Original claim upheld.

**Controlling evidence.** The cited receipts show 26 Luna repair-designer calls totaling about 1 h 43 m and 18 Luna fixer calls totaling about 3 h 24 m, with many transport-successful outputs later rejected. Cross-model attempts 14-16 failed identically before reasoning, while C5 and C8 bind major failures to executable contracts and environment capability. No controlled Luna-versus-Terra quality experiment exists.

**Corrected final claim wording.** The historical Luna-high design/fix policy was expensive, but this run does not establish Luna quality as the primary cause; the dominant proven blockers were deterministic schema, contract, state, and environment defects, and Terra-medium's quality/cost advantage remains unproven here.

**Accepted fix implication.** Keep bounded repair design on Terra-medium with independent Sol review, retain stronger implementation identity only where justified, benchmark equivalent closures on acceptance, rework, tokens, and runtime, and stop before model invocation when deterministic gates already fail.

### J-C13

**Ruling:** Adversarial narrowing adopted.

**Controlling evidence.** `implementation-partition.v1.json` proves the initial implementation had three groups. Closure 6 attempt histories show focused/full tests passing before reviewers found global read authority, non-streaming output enforcement, pytest fallback, invalid self-check, selector, real-sandbox, process-evidence, and regression defects. This proves missing or late high-blast-radius gates for concrete attempts, but does not quantify how many of all 18 fixer calls each proposed gate would have prevented.

**Corrected final claim wording.** High-blast-radius shared-controller repairs reached expensive targeted review without deterministic coverage of decisive policy and production-runtime properties, causing concrete preventable churn; the evidence does not attribute every fixer call or a measured fraction of total churn to this defect.

**Accepted fix implication.** Before targeted model review, run deterministic forbidden-read/selector checks, pre-communication output bounds, process-evidence validation, production-real sandbox smoke tests, and dependency-mapped regression tests; reduce shared-controller coupling so repair write sets can remain disjoint.

### J-C14

**Ruling:** Original claim upheld.

**Controlling evidence.** `dispatch.v1.json` contains no full controller-package digest or version; repair-designer receipts use three schema-source hashes during the same run; and all coordinator receipts retain one thread while continuation prompts are regenerated from the currently installed package and current model policy. Per-invocation hashes preserve local provenance but do not define one immutable or explicitly migrated run-level controller version.

**Corrected final claim wording.** The control plane was not frozen across blocked resumes: the same run and coordinator thread consumed changing installed schemas, prompts, policy, and controller logic without a dispatch-bound package digest or explicit migration record.

**Accepted fix implication.** Snapshot the complete controller package at dispatch and bind its digest into queue, checkpoint, transaction, and receipts; resume that exact snapshot, or require an explicit migration with old/new hashes, schema/state invariant proof, and a fresh bounded coordinator context.

### Adjudication totals

- Original claim upheld: **6** (C1, C7, C9, C10, C12, C14)
- Adversarial narrowing adopted: **8** (C2, C3, C4, C5, C6, C8, C11, C13)
- Original claim rejected: **0**
- Total adjudicated claim pairs: **14**

### Final prioritized fixes from adjudicated C1-C14

1. **Unblock deterministic schema failures (C1, C2):** repair the canonical `required` set, add recursive strict-provider schema preflight, emit structured terminal error classes, and forbid cross-model retries for deterministic schema 4xx failures.
2. **Freeze or explicitly migrate the run control plane (C14):** dispatch-bind a complete package digest and require proven state/schema migration plus a fresh bounded coordinator context for any change.
3. **Move contradiction and capability proof before model work (C5, C6, C8, C9, C11):** compile provider schemas, execute immutable-effect satisfiability checks, probe the real sandbox and reviewer scratch environment, and prove operator-resolution dataflows before resume.
4. **Remove project policy from the global controller (C10):** interpret source-hashed, repository/closure-bound run-owned resolution artifacts through generic validation.
5. **Reduce repair regression amplification (C4, C13):** dependency-map closures and code surfaces, batch only explicit coupled repairs, run deterministic high-blast-radius checks before model review, and enforce starvation limits.
6. **Bound model coordination (C3):** make routine orchestration deterministic, roll context at phase/closure boundaries, and cap coordinator turns and context growth.
7. **Preserve fresh post-resolution budgets as an invariant (C7):** migrate old ledgers without deleting history and allow exactly one zero-based post-resolution budget reset.
8. **Evaluate the revised model policy under controlled gates (C12):** use Terra-medium for bounded repair design with independent Sol review, then compare equivalent closures on correctness, rework, tokens, and runtime before further policy changes.
