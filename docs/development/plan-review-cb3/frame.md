# CB-3 plan — FRAME lens review

Subject: `docs/development/CONTRACT_BURDEN_RELAXATION_3_PLAN.md` @ `035c1f3`
(nodes CB3-01..CB3-07). RED_BASE `b49c194` = HEAD~1. Lens: program structure and
ownership boundaries. Read-only review; no source or plan file was modified.

Method: for every source file in each node's `Owned paths`, enumerated every
`tests/` module that imports it or asserts on the exact strings/behaviours the
node's ACs change (`unknown provenance reference`, `writable worker requires a
clean repository baseline`, `dirty_baseline*`, `review_fix`/`fix_keys`,
`operator_input`), then compared the resulting file-ownership matrix against the
mermaid DAG under `max_parallelism = 2`.

---

## Ownership / DAG matrix (derived)

| File | Owning nodes | Ordered by DAG? |
|---|---|---|
| `harness_labs/controller_kernel.py` | CB3-01, CB3-03, **CB3-05** | 01→03 yes; **05 unordered** |
| `harness_labs/controller_live.py` | CB3-02, CB3-03, CB3-04, **CB3-05** | 02→03, 02→04; **03∥04 unordered, 05 unordered** |
| `harness_labs/claude_task_executor.py` | CB3-02, CB3-04 | yes (02→04) |
| `harness_labs/agent_mixture.py` | CB3-02, CB3-03 | yes (02→03) |
| `harness_labs/controller_coordinator.py` | CB3-05 | n/a |
| `harness_labs/review_fix.py` | CB3-06 | n/a |
| `tests/test_controller_kernel.py` | CB3-01, CB3-03, CB3-05 | same defect as kernel |
| `tests/test_controller_live.py` | CB3-02, CB3-03, CB3-04, CB3-05 | same defect as live |

---

# Critical findings

## C1 — The DAG omits three same-file ordering edges; CB-2's own join
contract turns them into hard `PlanGraphError`s

`plan_graph.py:1782-1788` (`_join_candidates`) states the rule the CB-2 program
was built around:

> "Conflicting joins raise — a conflict between siblings means their allowed
> paths were not disjoint in effect, which is a plan defect, not a repair
> target."

The CB-2 runner encoded that discipline explicitly
(`experiments/run_burden2_plan_graph.py:109-116`: "Dependency edges exist only
where nodes share owned files … Roots CB2-01, CB2-02, CB2-03 are file-disjoint
and parallel-eligible … Shared-file spine … serialized CB2-03 -> CB2-05 ->
CB2-06"). CB-3's DAG (plan lines 29-43) breaks it in three places:

1. **CB3-05 ∥ CB3-01** (both roots, both admissible in the first pair):
   both own `harness_labs/controller_kernel.py` and
   `tests/test_controller_kernel.py` (plan:49, plan:93). No edge.
2. **CB3-05 ∥ CB3-02/03/04**: CB3-05 owns `harness_labs/controller_live.py` and
   `tests/test_controller_live.py` (plan:93); so do CB3-02 (plan:60), CB3-03
   (plan:71) and CB3-04 (plan:82). No edge to any of them.
3. **CB3-03 ∥ CB3-04**: both depend only on CB3-02, so with
   `max_parallelism = 2` they are the natural second admitted pair — and both
   own `harness_labs/controller_live.py` **and** `tests/test_controller_live.py`
   (plan:71, plan:82). No edge between them.

Every one of these pairs is exactly the "not disjoint in effect" condition. Both
siblings will edit the same regions of `controller_live.py` (the preflight
refusal path at `controller_live.py:594-600` is literally shared between
CB3-02's typed refusal, CB3-03's grant attachment, and CB3-04's restoration
precondition), and the sink join at CB3-07 raises rather than merges. This is
not a merge inconvenience: `_join_candidates` has no repair path, and the plan's
recovery discipline (rule 5) resumes lineages — it does not re-plan the DAG.

Required surgery: add `CB3-05 → CB3-01` (or make CB3-05 a downstream of the
kernel/live spine), and `CB3-03 → CB3-04` (CB3-04's restoration must see
CB3-03's grant attachment anyway — it is also a *semantic* edge: AC-CB304-3
asserts "successful attempts' residue is never restored (it is adoption-grant
material for CB3-03)", which is a statement about CB3-03's mechanism).

## C2 — CB3-02, CB3-03 and CB3-04 do not own `tests/test_relax_adoption.py`,
and AC-CB302-2 provably invalidates two of its assertions

`tests/test_relax_adoption.py` is the densest existing assertion surface on the
dirty-baseline grant path (20 `dirty_baseline` references; the only module
besides the owned four). It is owned by no CB-3 node. Concretely:

- `tests/test_relax_adoption.py:618-668`
  (`CodexSemanticTaskExecutorDirtyBaselineTests.
  test_dirty_path_outside_receipted_change_set_is_refused_even_inside_writable_paths`)
  asserts `assertIn("clean repository baseline", …)` for the case
  **grant supplied, coverage insufficient** (test_relax_adoption.py:667).
  AC-CB302-2 (plan:63) says precisely: the typed refusal names the uncovered
  paths, and *"the generic 'writable worker requires a clean repository
  baseline' message remains **only** for the no-grant-supplied case."* That AC,
  satisfied literally, turns this test red — in a module CB3-02 may not touch.
  The exercised code (`CodexSemanticTaskExecutor`) lives in
  `harness_labs/controller_live.py:153`, which CB3-02 **does** own. Source
  owned, assertion not: the exact FRAME C5 failure mode from CB-2 adjudication
  §8.
- `tests/test_relax_adoption.py:193` hard-codes the generic message string in a
  test double (`{"error": "writable worker requires a clean repository
  baseline"}`), which the CB3-03/CB3-04 integration paths at
  `test_relax_adoption.py:227` and `:446` route through.
- `tests/test_relax_adoption.py:706-737`
  (`test_dirty_baseline_without_any_grant_is_refused`) survives, since it is the
  no-grant case AC-CB302-2 preserves — so the module is *partially* invalidated,
  which is the worst case: the node's own regression run (`tests/`) fails and the
  fix is out of grant.

Also unowned and importing the same sources: `tests/test_relax_claims.py`,
`tests/test_relax_semantic_floor.py` (both import `claude_task_executor` and
`controller_live`).

Required surgery: add `tests/test_relax_adoption.py` to CB3-02, CB3-03 and
CB3-04 owned paths (it is already serialized among them once C1's edges exist),
and `tests/test_relax_claims.py` / `tests/test_relax_semantic_floor.py` to
CB3-02.

## C3 — AC-CB306-2 silently kills the already-landed cross-node
finding-transfer mechanism (item 9), and CB3-06 owns none of its tests

`review_fix.py:176-208` (`transfer_scope_expanding`) implements item 9's landed
mechanism (`contract-burden-reduction.md:99`, commit `db003d5`): a finding whose
`required_paths` fall **outside** the node's `current_paths` and that resolves to
a unique downstream owner is transferred to that owner. Its loop body filters on
`record["outcome"] != "open"` (`review_fix.py:187`) — i.e. it can only move
findings that ingest entered as **open obligations**.

AC-CB306-2 (plan:107) says: *"at ingest, a finding whose file or required_paths
fall outside the node's writable paths is **not entered as an open obligation**;
it is journaled as an out-of-grant annotation."* That is the same population.
Implemented as written, `transfer_scope_expanding` finds nothing to transfer, and
item 9's mechanism becomes dead code. AC-CB306-3 (plan:108) enumerates what must
be preserved — "discovery freeze after cycle 1, deferred handling, fix/verify
semantics, and the cycle ceiling" — and conspicuously does **not** mention
`transferred_findings`.

Ownership gap compounding it: the transfer contract is asserted in modules
CB3-06 does not own —

- `tests/test_feature_run.py:1298-1304`
  (`assertEqual(result.review_fix.transferred_findings, (transferred,))`)
- `tests/test_plan_graph.py:249-250` (`evidence={"review_fix":
  {"transferred_findings": [transfer]}}`), feeding
  `plan_graph.py:2109-2112`'s `_advance_finding_obligations` path
- `tests/test_feature_run.py:922` (`assertEqual(result.review_fix.cycles, 2)`) —
  directly sensitive to any change in `fix_keys` arithmetic
- `tests/test_relax_adoption.py:551-554`,
  `tests/test_relax_gate_decomposition.py:406,472`,
  `tests/test_run_catalog.py:85` (`review_fix.cycles` projection via
  `run_catalog.py:419-431`)

Required surgery: state ordering explicitly — out-of-grant screening must run
**after** transfer-eligibility resolution, and a finding with a unique downstream
owner must transfer, not annotate. Add `tests/test_feature_run.py`,
`tests/test_plan_graph.py`, `tests/test_run_catalog.py` to CB3-06's owned paths
(or add an AC asserting `transferred_findings` is byte-unchanged).

## C4 — CB3-01's red phase is asserted-green at RED_BASE by an unowned test
module, and item 6 is already marked landed in the diagnosis source

The plan's own diagnosis source says item 6 is closed:

> `contract-burden-reduction.md:60`: "**Status:** landed (CB-01, commit
> `578ff4b5…`). Provenance validation in `harness_labs/controller_kernel.py`
> accepts a reference that resolves to an existing audit-journal event, so a
> rejected `task.dispatch` — already a journal event — is citable by later
> commands."

The mechanism is live at RED_BASE: `controller_kernel.py:1258-1263` registers
`f"command:{command.command_id}"` into `_state["rejected_task_dispatch_refs"]`
on rejection, and `controller_kernel.py:556-559` bypasses the
`unknown provenance reference` raise for exactly those refs. Three tests already
assert it, in a module **CB3-01 does not own**:

- `tests/test_relax_kernel.py:362` `test_rejected_dispatch_becomes_citable_provenance`
- `tests/test_relax_kernel.py:405` `test_rejected_dispatch_provenance_survives_resume`
- `tests/test_relax_kernel.py:456` `test_rejected_dispatch_provenance_is_checkpointed_immediately`

AC-CB301-4 (plan:54) claims the red is "a retry citing a rejected dispatch's
command id raises `unknown provenance reference`". For an `invalid_command`
`task.dispatch` that is **false at RED_BASE** and proven false by
`test_relax_kernel.py:362`. The plan's framing at plan:5 ("Item 6 is the one
remaining classic relaxation") contradicts its own cited source at :60.

The residual is genuinely narrower than the node claims and should be stated as
such: (a) registration is gated on `code == "invalid_command"`
(`controller_kernel.py:1258`) so `unauthorized_command` / `unknown_evidence` /
`terminal_run` rejections stay unreferenceable; (b) the ref is registered as
*evidence provenance* only — the **predecessor** paths (`supersedes_task_id`
validation at `controller_kernel.py:821-852`, which raises
`unknown superseded task` / `superseded task is not failed`) do not accept a
rejection record, which is item 17's actual second half
(`contract-burden-reduction.md:176-178`). AC-CB301-2's retry-budget clause has no
demonstrated red site at all.

Required surgery: re-aim AC-CB301-1/2/4 at the residual (non-`invalid_command`
rejection codes + predecessor acceptance in `supersedes`/retry admission), and
add `tests/test_relax_kernel.py` to CB3-01's owned paths — it will need updating
if the registration condition or the record shape changes.

## C5 — Keep-list loophole: AC-CB304-1 restoration can destroy gate-passing
work and defeat the deliverable-content floor

AC-CB304-1 (plan:84) triggers restoration on "the attempt's terminal status is
failed". The keep-list (plan:19) protects the deliverable-content floor. But the
diagnosis records exactly the case where those collide:
`contract-burden-reduction.md:50` — "worker produced a **gate-passing**
`physics.js` with a placeholder report" — and
`contract-burden-reduction.md:207` names "a placeholder summary refused by the
deliverable floor (CB2-08, attempt-3)" as one of the three item-19 triggers.

A deliverable-floor refusal yields `status == failed` with a *substantively
correct working tree*. Under AC-CB304-1 the controller then reverts every
receipted path — destroying the only copy of gate-passing work whose sole defect
was a hollow summary, i.e. converting a one-turn "rewrite the summary" repair
into a full re-implementation. The floor was added (CB-07,
`contract-burden-reduction.md` counterweight, commit `3ce8586`) precisely to
refuse hollow *reports*, not to invalidate code.

AC-CB304-2 protects "journals or evidence artifacts" but says nothing about the
candidate's own source deltas. No AC excludes the deliverable-floor refusal class
from the restoration trigger, and AC-CB304-3 only exempts *successful* attempts.

Required surgery: add a fourth conjunct — restoration is declined when the
attempt's failure classification is a semantic/deliverable-floor refusal rather
than a workspace-integrity failure — or require the pre-restoration state to be
captured as a retained artifact so the revert is recoverable.

## C6 — Keep-list loophole: AC-CB306-1 receipt discharge has no freshness
binding to the candidate under review

AC-CB306-1 (plan:106) verifies that the cited artifact (a) resolves in the
evidence catalog, (b) exists, (c) is controller-owned. AC-CB306-3 forbids only
self-discharge ("a worker cannot self-discharge by citing its own output
artifact"). Nothing binds the receipt to the **tree state the finding was raised
against**.

Loophole: a fix stage discharges a cycle-2 finding about newly-changed code by
citing the *cycle-1* gate receipt — controller-owned, resolvable, not
worker-authored, and green. The obligation is marked
`discharged-by-receipt` "without requiring a working-tree change" (plan:106,
explicit). This weakens the keep-list item "controller-owned deterministic
verification" by allowing stale evidence to adjudicate current code — the same
class of defect the CB-2 adjudication guarded with digest binding (§5, MECH M7
`gate_digest` totality).

Required surgery: AC must require the receipt's bound commit/candidate digest to
match the candidate under review, and that the receipt post-dates the finding's
first `cycles_seen` entry (`review_fix.py:404`).

---

# Major / minor findings

## M1 — Precondition 3 asserts frozen properties of a file that does not exist

Plan:11 declares `experiments/run_burden3_plan_graph.py` "frozen unowned
infrastructure". It is absent from the tree (`ls experiments/` yields
`run_archimedes_feature.py`, `run_burden2_plan_graph.py`, `run_burden_plan_graph.py`,
`run_orbit_plan_graph.py`, `run_retinology_demo_feature.py`,
`run_rocketship_feature.py`, `run_trebuchet_feature.py` — no burden3 runner).
Program rule 1 (plan:16) asserts it is "born with the evidence, path-grant, and
summary-floor instruction pins", but no AC in any node verifies that, no node
owns it, and the plan does not quote the pin text or cite the CB-2 lines it is
cloned from (`run_burden2_plan_graph.py:95-101` `BASE_INSTRUCTIONS`;
`:395` `Edit only these paths: …`; `:404` deliverable substantiveness). The
program's entire frozen-infrastructure defence rests on an unwritten,
unverified artifact.

Required: make runner authorship a *pre-decomposition operator step* with an
explicit checklist (three pins + the CB-2 `claims_rule` retirement decision),
recorded in the plan with the commit sha of the created runner, before
RED_BASE is frozen for decomposition.

## M2 — Frozen-infrastructure exposure is asymmetric and unmapped per node

The plan states the general fact (precondition 4, plan:12) but never maps it. The
map:

| Node | Live defect it runs under | Armed by |
|---|---|---|
| CB3-01 | item 6 residual — a rejected dispatch of a *non-`invalid_command`* code is uncitable | nothing |
| CB3-02 | item 20 — its own implementer's grant may be granted-then-refused | nothing (rule 1 pins do not cover it) |
| CB3-03 | item 17 — coordinator follow-up after a successful implementer strands the node | nothing |
| CB3-04 | item 19 — **its own** failed implementer dirty-baselines its node; the node fixing the dead-end is the likeliest to hit it | nothing |
| CB3-05 | item 19 escalation half — a coordinator `operator_input.request` kills the session | nothing |
| CB3-06 | item 18 — an out-of-grant review anchor deadlocks the node fixing out-of-grant anchors | rule 2 (instruction pin) |

Only CB3-06's exposure is armed (rule 2, plan:17). Rule 1's three pins address
the CB-2 *proximate triggers* (out-of-grant edits via the path-grant pin,
placeholder summaries via the summary-floor pin) but nothing addresses the
*amplifier* — once any node's writable attempt fails, the tree is dirty and the
node is dead, which is precisely CB3-04's subject. The honest mitigation is
operational, not mechanical: state that a dirty-baseline block is an expected
recovery event (rule 5) and that CB3-04's lineage in particular should be run
with the smallest possible per-attempt write surface. The plan should say so.

## M3 — CB3-05 has zero existing test coverage to inherit, and one unowned
source touch point

`grep -rn "operator_input\|operator_questions" tests/*.py` returns **nothing** —
no existing module exercises `operator_input.request` at all. Good news for
ownership (no assertion-invalidation risk), bad news for AC-CB305-3
("attended runs are byte-for-byte unchanged"): there is no attended-run
regression baseline to protect, so that AC is unfalsifiable by the existing suite
and must be discharged by new tests inside
`tests/test_relax_headless_operator.py`. State that explicitly.

Unowned source touch point: `controller_projection.py:105,116` projects
`operator_questions` and statically enumerates `"operator_input.request"` in
`allowed_commands`. If headless mode changes the command's availability or the
question record's shape, this file (and its assertions in
`tests/test_run_catalog_contracts.py`, `tests/test_controller_run.py`) is
affected. CB3-05 owns neither. Either add `controller_projection.py` to CB3-05 or
add an AC clause asserting the projection surface is unchanged.

## M4 — AC-CB306-2's out-of-grant classification is driven by
reviewer-supplied fields, giving a review-nullification path

`review_fix.py:411-412` shows `required_paths` and `file` are copied verbatim from
the reviewer's finding payload; `scope_expanding` (`review_fix.py:398-401`) is
likewise reviewer-declared. Under AC-CB306-2, a reviewer (or a compromised /
lazy fix stage that re-emits findings) that anchors everything to a path outside
the grant converts every obligation into a non-blocking annotation, excluded from
`fix_keys` and from cycle-limit arithmetic (plan:107). The review gate then
adjudicates nothing while reporting "no open obligations".

Guard to require in the AC: a finding with **no** `file`/`required_paths`, or
whose anchor cannot be normalized, must remain an in-grant open obligation
(fail closed); and the annotation count must be surfaced in the cycle report in
a way the controller can gate on.

## M5 — Effective parallelism after C1's edges is ~1, not 2

Once CB3-05 is serialized into the kernel/live spine and CB3-03 → CB3-04 is
added, the only file-disjoint node is CB3-06 (`review_fix.py` alone). The
achievable schedule is a mostly serial chain with CB3-06 riding alongside one
other node. `max_parallelism = 2` is then a correct setting but a misleading
expectation, and — per `contract-burden-reduction.md:137-141` (item 14, CB-06's
red phase timing out at 701 s under the first concurrent gate execution) —
concurrency is the CB-2 defect that consumed a whole node. Since CB2-03 landed
the exclusive gate slot, gate execution is already serialized; the plan's
budget rationale (rule 3, plan:18) should say so rather than leaving the reader
to infer that two concurrent full-suite runs are budgeted for.

## M6 — CB3-04's restoring actor is ambiguous and the ownership hints at the
wrong side of the keep-list

The keep-list protects "controller-owned deterministic verification" and the
executor's refusal of a dirty baseline (plan:19). AC-CB304-1 says "the
controller restores", but CB3-04's owned paths include
`harness_labs/claude_task_executor.py` (plan:82) — the executor preflight side
(`claude_task_executor.py:509`). If restoration is reachable from the executor's
own preflight, the clean-baseline precondition degrades from "refuse" to
"silently self-heal", which is a keep-list weakening even though AC-CB304-3
asserts the opposite in prose. The AC should name the calling site (controller
side, before dispatch) and state that the executor preflight is
restoration-unaware — its only legal outcomes stay {accept clean, accept with
valid grant, refuse}.

## M7 — CB3-01's kernel ownership leaves nine importing test modules unowned

Beyond C4's `test_relax_kernel.py`, `controller_kernel` is imported by
`tests/test_controller_run.py`, `tests/test_controller_scenarios.py`,
`tests/test_controller_scheduler.py`, `tests/test_coordinator_dispatcher.py`,
`tests/test_feature_run.py`, `tests/test_relax_adoption.py`,
`tests/test_relax_delta_repair.py`, `tests/test_relax_gate_criteria.py`,
`tests/test_relax_gate_decomposition.py`. Spot-checked assertions on receipt
acceptance (`test_relax_gate_criteria.py:104,155,197`,
`test_controller_scheduler.py:76`, `test_coordinator_dispatcher.py:454`) are
positive-path (`assertTrue(receipt.accepted)`) and therefore insensitive to a
*widening* of provenance acceptance — genuine risk here is low. Recorded as
minor rather than critical for that reason; `test_relax_kernel.py` is the one
that must be owned.

---

# Self-refutations (attacks considered and rejected)

**R1 — "CB3-06 cannot implement AC-CB306-2 without owning `feature_run.py`."**
Rejected. `ReviewFixLoop.__init__` already receives `allowed_paths`
(`review_fix.py:451`) and `changed_paths` (`:452`), stored at `:466-467`, so the
node's writable grant is in scope inside the owned file. No new plumbing through
`feature_run.py` is required. (C3's transfer-ordering problem is real and
independent of this.)

**R2 — "CB3-02 is not a predecessor of the sink CB3-07, so its work is lost."**
Rejected. `_final_join` (`plan_graph.py:1775-1780`) computes sinks as runs not
depended on by anyone; CB3-02 is depended on by CB3-03 and CB3-04, and
`_join_candidates` prunes ancestors (`plan_graph.py:1787-1796`), so CB3-02's
lineage arrives transitively. The missing direct edge is correct plan hygiene,
not a defect.

**R3 — "The CB3-01 → CB3-03 edge is false (no data dependency)."**
Rejected. CB3-03 does not consume CB3-01's rejection records semantically, but
both own `harness_labs/controller_kernel.py` and `tests/test_controller_kernel.py`
(plan:49, plan:71). Under the CB-2 convention (`run_burden2_plan_graph.py:109-110`,
"Dependency edges exist only where nodes share owned files or consume another
node's mechanism") a shared-file serialization edge is a legitimate edge. Same
verdict for CB3-02 → CB3-03 and CB3-02 → CB3-04, which are *both* shared-file
and semantic.

**R4 — "Program rule 7 already fixes the ownership problem, so C2/C3/C4 are
moot."** Rejected. Rule 7 (plan:22, "Every node owns the existing test modules
for its owned sources") is a *policy statement*, but the per-node `Owned paths`
lists are the operative grant, and they do not implement it: `test_relax_adoption.py`,
`test_relax_kernel.py`, `test_relax_claims.py`, `test_relax_semantic_floor.py`,
`test_feature_run.py` and `test_plan_graph.py` all assert on owned sources and
appear in no node's list. Write-grant enforcement is post-hoc and mechanical
(keep-list, plan:19) — it enforces the list, not the rule's intent. The rule
being stated makes the omission worse, not better: it will read as satisfied.

**R5 — "CB3-05 is file-disjoint enough in practice; the kernel/live regions it
touches (`_operator_input_request`, `controller_kernel.py:990-1000`) don't
overlap CB3-01/02/03/04's regions."** Rejected on the contract, not the
guess. `_join_candidates` conflicts are decided by git merge over the whole file,
and both siblings will also edit `tests/test_controller_kernel.py` /
`tests/test_controller_live.py`, where new test methods land at file-adjacent
positions and conflict routinely. The CB-2 adjudication accepted "DAG same-file
ordering is sound" as a *refutation* precisely because CB-2 had no such pair;
CB-3 has three.

**R6 — "C5's deliverable-floor collision is hypothetical."** Partially
self-refuted: I could not find a code path proving a floor refusal produces
`terminal status == failed` *with* a workspace-change receipt attesting the
residue, because `harness_labs/claude_task_executor.py`'s floor refusal may
precede receipt minting. The finding stands as an AC-completeness gap — the AC
must exclude the class explicitly rather than rely on ordering that is not stated
anywhere in the plan — but its severity depends on that ordering, which the plan
should assert.

**R7 — "AC-CB302-3 already protects the keep-list, so C2 is only a test
inconvenience."** Partially accepted, does not change the verdict.
AC-CB302-3 (plan:64, "the unification levels *up*") is a genuinely strong,
well-drafted keep-list guard — I found no way to satisfy the CB3-02 ACs while
weakening the clean-baseline precondition. C2 is an *ownership* finding: the
correct behaviour breaks an unowned assertion, which by CB-2's FRAME C5 is
unfixable mid-run regardless of how correct the change is.

---

# Per-node verdicts

| Node | Verdict | Blocking findings |
|---|---|---|
| CB3-01 | **Re-aim + expand ownership** | C4, M7 |
| CB3-02 | **Expand ownership** | C2 (mechanism sound; R7) |
| CB3-03 | **Expand ownership + add edge** | C1, C2 |
| CB3-04 | **Add edge + AC surgery** | C1, C2, C5, M6 |
| CB3-05 | **Add ordering edges (root status unsafe)** | C1, M3 |
| CB3-06 | **AC surgery + expand ownership** | C3, C6, M4 |
| CB3-07 | **Accept** | none (R2 refuted) |
| Program | **Preconditions incomplete** | M1, M2, M5 |
