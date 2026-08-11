# PlanGraph Parallelization Run: Defect, Recovery, Retry, and Cost Postmortem

## Bottom line

The run exposed:

- **1 non-terminalized interruption**: the mistaken standalone PG-00 launcher.
- **13 unsuccessful terminal graph attempts**:
  - 7 `blocked`
  - 6 `failed`
- **2 successful graph attempts**: PG-00 attempt 3 and successor 13.
- **56 structured review defects**:
  - 47 fixed during review-fix
  - 5 marked deferred
  - 4 left open and directly terminalizing
- **No native PlanGraph recovery or resume event fired.** Recovery was performed manually by launching linked successor graphs and selectively reusing verified commits.
- **22 measured node retries**, plus two PG-05A pre-launch integration retries and the unmeasured mistaken standalone launch.
- **86.646M measured tokens total**.
- **57.898M tokens spent on retry executions**: 66.82% of measured tokens.
- **49.782M tokens attributable to discarded/non-final candidates**: 57.45% strict waste.
- Estimated strict wasted cost: **$61.07 at GPT-5.6 Terra Priority rates**.
- The final candidate is committed at `e848c0e7fc42890eb626bde999daea990f6be033`, but it has **not been merged into the repository's current base branch**.

## 1. Operational defect and recovery ledger

| Attempt | Defect / terminal cause | Effect | Recovery fired? | Outcome and resume point | Fix type / persistence |
|---|---|---:|---|---|---|
| Original PG-00 | Wrong `serial-implement-codex` launcher created a standalone FeatureRun and entered planning | **Interruption/orphan**; never terminalized | No | Relaunched as PlanGraph-bound attempt 2 from PG-00 `implement` | Bespoke launcher correction; not committed |
| Attempt 2 PG-00 | Worker wrote fixture paths outside its grant | **Blocked** | No native recovery | Attempt 3 restarted PG-00 from original base, `implement` | Bespoke grant/prompt correction; not committed |
| Initial graph registration | Decomposition registered only PG-00, so dashboard could not show PG-01 through PG-07 | Did not terminate PG-00, but interrupted the intended full graph | No | Successor 1 started from verified `b152bbe...` at PG-01/PG-02 | Bespoke successor decomposition; not committed |
| Successor 1 | PG-01 through PG-04 completed, then PG-05A dependency integration conflicted in `NEXT_STEPS.md` | **Failed** | No | Successor 2 resumed at PG-04; later successor 3 retried PG-03 through PG-05A | Bespoke integration/recomposition; not committed |
| Successor 2 | PG-04 review left closed-request/lane-custody enforcement open | **Failed** | Review-fix fired but failed to close finding | Successor 8 ultimately resumed PG-04 on rebuilt PG-01 through PG-03 ancestry | Generalizable product fix eventually committed in `bc75...` |
| Successor 3 | Reimplemented PG-03, then PG-05A integration conflicted in `INDEX.md` | **Failed** | No | Successor 4 retried PG-03 and manually constructed PG-05A base | Bespoke conflict handling; not committed |
| Successor 4 | PG-05A worker claimed descriptive criterion text rather than exact `PG05A-01` | **Blocked** | No | Successor 5 resumed at PG-05A | Bespoke criterion-prompt correction; not committed |
| Successor 5 | PG-07 could not certify broad catalog/dashboard/E2E checks; socket binding was denied | **Blocked** | Worker requested replan; no automatic recovery | Successor 6 resumed at PG-06 to repair upstream visibility | Mixed: product fixes generalizable; environment workaround bespoke |
| Successor 6 | PG-06 review found missing barrier, resumability, and retention visibility | **Failed** | Review-fix fired but finding remained open | Successor 7 resumed upstream at PG-01/PG-03 | Generalizable implementation eventually committed in later PG-06 commits |
| Successor 7 | PG-03 unsafe parallel-success adoption remained open after three review cycles | **Blocked** | Review-fix fired, exhausted cycle limit | Successor 8 rebuilt PG-02 through PG-06 from verified PG-01 | Generalizable scheduler/custody fix committed in `582a...` |
| Successor 8 | PG-06 catalog projection violated the canonical closed execution schema | **Failed** | Review-fix fired but fixer lacked authority for schema path | Successor 9 resumed at PG-06 from `1fb093...` | Generalizable schema/projection fix committed in `f1cbb...` |
| Successor 9 | PG-06 succeeded; PG-07 exposed PG-05B descriptor-lineage incompatibility and socket restriction | **Blocked** | No native cross-node recovery | Successor 10 resumed at PG-06; successor 11 resumed at PG-05B | Generalizable compatibility repair committed in `c66...`/`7e348...` |
| Successor 10 | Review "fixed" schema compatibility by reverting changes, leaving no repository diff | **Failed** Git transaction | Review-fix technically succeeded, integration failed | Successor 11 resumed at PG-05B and PG-06 | Revealed a general harness defect: successful fix cycles can erase the candidate; unresolved |
| Successor 11 | PG-07 worker sandbox could not bind loopback and therefore withheld criterion coverage | **Blocked** | No | Successor 12 resumed at PG-07 from `7e348...` | Bespoke controller/worker responsibility handoff |
| Successor 12 | PG-07 worker made no repository change | **Blocked** | No | Successor 13 resumed at PG-07 from `7e348...` | Bespoke prompt requiring a durable documentation change |
| Successor 13 | Review found browser-E2E preconditions were not deterministic | Review **interruption**, not terminal | Review-fix fired and succeeded | Continued within PG-07 review-fix; committed `e848c...` | Generalizable, committed in final candidate |

The terminal evidence is reconstructible from `logs/runs/`, including `logs/runs/plangraph-parallelization-20260810-attempt-2/events.jsonl` and `logs/runs/plangraph-parallelization-20260810-successor-13/summary.json`.

## 2. Review defects, grouped by cause similarity

These groups account for all 56 ledger findings.

| Class | Defects identified | Terminal impact | Recovery result | Final disposition |
|---|---|---|---|---|
| PG-00 contract completeness | Incomplete ADR semantics; missing schema families; allocation exclusivity; missing fixtures; missing scheduling/liveness tests; seal identity binding; allocation contender CAS uniqueness | Mostly review interruptions; contender uniqueness was deferred | Review-fix closed 6/7 | Generalizable PG-00 contract committed at `b152bbe...`; one deferred concurrency issue was not explicitly closed |
| Allocation and migration | Invalid commit identities; unsafe legacy checkpoint migration; impossible sibling batch reservation; invalid integration receipts; succeeded-attempt invalidation | Review interruptions; drove PG-01 retry | Review-fix succeeded | Committed through `b670...` and `ab7...` |
| Child request and worktree custody | Missing canonical seal receipt; allocated worktree substitution; Unicode/bool/empty-grant schema acceptance | Review interruptions | Review-fix succeeded | Committed through `23d...` and `dd203...` |
| Scheduler correctness | Invalid active-ready state; production scheduler not connected; partial join dependencies; duplicate relaunch after interruption; unhandled launcher exception; unsafe unsealed success adoption; invalid sealed dependency state | One direct PG-03 blocker after cycle exhaustion | Review-fix failed once, later successor succeeded | Generalizable fixes committed in `582a...`; earlier discarded candidates not retained |
| Join and staging safety | Stale/unbound allocation; missing leases and audit trail; no pre-dispatch verification; non-schema join requests; lane-custody loss; crash window around CAS/audit; arbitrary protected ref; optional audit bypass; stale post-advance lease | One direct PG-04 failure; other review interruptions | Several review-fix cycles; later PG-04 succeeded | Main fixes committed in `bc75...`; post-advance stale-lease recovery was deferred |
| Liveness and force reconciliation | Live-process seal adoption; accepting missing/mismatched process identity; extra properties bypass; unjournaled seals; stale force records; nonexistent or unverified evidence references | Review interruptions | Review-fix succeeded enough to pass PG-05A | Committed in `1fb093...`; byte verification of nonterminal force evidence was deferred |
| Repair-resume compatibility | Ad-hoc resume authority; nonexistent blocker evidence; changed decomposition/dependencies/tests accepted; omitted plan sections and acceptance maps from digest | Review interruptions | Review-fix succeeded | Committed through `47d...` and `c66...`; an earlier deferred form was superseded |
| Catalog/dashboard observability | Recovery disposition dropped; false evidence availability; missing barrier/resume/retention projection; schema-invalid execution records; weak runtime validator; lineage dropped | Directly caused PG-06 failures and downstream PG-07 blockers | Review-fix failed in successors 6/8, later succeeded | Committed through `f1cbb...` and `7e348...` |
| Certification/E2E | Broad suite failures outside PG-07 scope; worker socket denial; no-change candidate; browser test could silently skip | Four PG-07 blockers or interruptions | Manual relaunches; final review-fix succeeded | Generalizable E2E precondition committed at `e848c...`; sandbox workaround was bespoke |
| Review bookkeeping | Final review ledgers contain five deferred findings, while successful `final-result.json` records list `technical_debt_keys: []` | Did not terminalize; corrupts reporting | No recovery | General harness defect; not fixed or committed |
| Usage aggregation | Parent graph summaries report zero tokens and `$0` while children report 86.6M tokens | Did not terminalize; prevents native cost accounting | No recovery | General harness defect; not fixed or committed |
| Recovery observability | No `recover`, `retry`, `resume`, `reconcile`, or `adopt` events were emitted for the manual successor chain | Did not terminalize but breaks causal reconstruction | No native recovery | General harness defect; successor correlation was recorded manually but not solved generally |

Review-fix itself worked reasonably well at the local finding level: **47/56 findings were closed**. It was much less successful as a run recovery mechanism: all four open findings terminalized their child, and no terminal graph was automatically resumed.

## 3. Retry counts and token cost

"Retry" below means every measured execution of a logical node after that node's first measured execution. It therefore includes both failed retry candidates and the successful replacement that finally got past the defect.

| Node | Measured attempts | Retries | Retry tokens | Priority-rate equivalent |
|---|---:|---:|---:|---:|
| PG-00 | 2 | 1 | 2.457M | $3.96 |
| PG-01 | 2 | 1 | 3.393M | $4.12 |
| PG-02 | 2 | 1 | 2.506M | $3.33 |
| PG-03 | 5 | 4 | 11.472M | $16.03 |
| PG-04 | 3 | 2 | 5.895M | $7.98 |
| PG-05A | 3 | 2 | 9.521M | $10.07 |
| PG-05B | 3 | 2 | 4.325M | $5.36 |
| PG-06 | 6 | 5 | 15.013M | $19.27 |
| PG-07 | 5 | 4 | 3.315M | $4.40 |
| **Total** | **31** | **22** | **57.898M** | **$74.50** |

PG-05A additionally encountered two pre-launch integration conflicts. They consumed no separately reported child tokens, so they are absent from the token table.

PG-06 was the costliest retry hotspot: six executions, five retries, approximately 15.0M retry tokens. PG-03 was second at 11.5M.

## 4. Waste calculation

Two definitions are used because "retry spend" and "waste" are not equivalent:

| Measure | Tokens | Share | Estimated cost |
|---|---:|---:|---:|
| All measured child execution | 86.646M | 100% | $108.26 |
| All retry executions | 57.898M | 66.82% | $74.50 |
| Strict discarded-candidate waste | 49.782M | 57.45% | $61.07 |
| Work represented in final commit ancestry | 36.864M | 42.55% | $47.19 |

Strict waste includes tokens from every candidate not ancestral to `e848c...`. It is an accounting upper bound: those attempts produced diagnostic evidence that informed later fixes, even though their code was discarded.

Conversely, this understates total waste because it excludes:

- The mistaken standalone launcher, which has no terminal summary or token record.
- Parent-agent orchestration and manual diagnosis.
- Non-model launcher and test activity.
- Any usage that was not emitted into child summaries.

The cost calculation uses the logged GPT-5.6 Terra usage and the official Priority rates of $5/M uncached input, $0.50/M cached input, and $30/M output. The logs do not record a billable service tier and explicitly say the usage is unpriced, so these are **rate-equivalent estimates, not observed invoices**.

- OpenAI Priority pricing: <https://openai.com/api-priority-processing/>
- GPT-5.6 Terra model pricing: <https://developers.openai.com/api/docs/models/gpt-5.6-terra>

At ordinary GPT-5.6 Terra rates, before long-context adjustments, the equivalent figures would be approximately half: **$54.13 total, $37.25 retry spend, and $30.54 strict waste**.

`DPN` is not defined anywhere in the repository. If it means **dollars per PlanGraph node**, then across PG-00 through PG-07:

- Gross DPN: **$13.53/node**
- Retry DPN: **$9.31/node**
- Strict wasted DPN: **$7.63/node**

## 5. Persistence assessment

The product fixes are generalizable and exist in committed candidate history:

- PG-00: `b152bbe...`
- PG-01: `b670...`, `ab7dc8...`
- PG-02: `23d374...`, `dd203...`
- PG-03: `582a...`
- PG-04: `bc75...`
- PG-05A: `1fb093...`
- PG-05B: `47d620...`, `c66...`
- PG-06: `f1cbb...`, `7e348...`
- PG-07: `e848c...`

However, "committed" here means reachable from the final candidate branch. The final candidate has **not been merged into the current repository base**, so none of these should yet be described as installed on the main harness line.

The orchestration remedies--successor decomposition files, manual verified-commit reuse, criterion prompt corrections, conflict workarounds, and controller-side socket execution--were largely bespoke and ephemeral. The three most important general harness defects remain uncommitted and unresolved:

1. Native terminal recovery/resume did not fire.
2. Parent graphs do not aggregate child usage or cost.
3. Deferred review findings disappear from final technical-debt reporting.
