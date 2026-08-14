# CB-3 plan — NECESSITY lens

Subject: `docs/development/CONTRACT_BURDEN_RELAXATION_3_PLAN.md` (CB3-01…CB3-07)
at HEAD `035c1f3`. RED_BASE = `b49c194` = HEAD~1. Verified: `git diff --stat
b49c194 HEAD` touches only the plan doc, so every `harness_labs/` line cited
below is byte-identical at RED_BASE.

Lens question, applied to every node and every AC: **is the defect real at
RED_BASE, is this the smallest change that closes it, and can the finding
test's red phase actually fail by behavior?**

Verdict summary:

| Node | Verdict |
|---|---|
| CB3-01 | REWORK — stated red is unreachable; the live specimens are a different defect |
| CB3-02 | REWORK — defect real and precisely located, but the specimen's own code site is not in owned paths |
| CB3-03 | SHRINK/REWORK — mechanism already exists in `agent_mixture.py`; the dispatch chokepoint is unowned |
| CB3-04 | REWORK — trigger precondition (a receipt attesting a *failed* attempt's residue) is unreachable for two of its three cited specimens |
| CB3-05 | DELETE — the claimed "session death" does not happen; the journal shows the promised terminal state already reached at RED_BASE |
| CB3-06 | SHRINK — AC-CB306-2's red is reachable and the fix is one predicate; AC-CB306-1 has no specimen |
| CB3-07 | SHRINK — item 6 is already marked landed; AC-CB307-1 as written mandates re-landing a closed item |

---

## Node-level findings

### C1 — CB3-01's stated red phase cannot fail; item 6 landed at CB-01

AC-CB301-4 asserts the red is "a retry citing a rejected dispatch's command id
raises `unknown provenance reference`". That behavior does not exist at
RED_BASE.

- `controller_kernel.py:1259-1261` — on `_reject` with
  `code == "invalid_command"` and `command.type == "task.dispatch"`, the kernel
  appends `f"command:{command.command_id}"` to
  `self._state["rejected_task_dispatch_refs"]` and immediately
  `merge_checkpoint`s it.
- `controller_kernel.py:553-559` — `_validate_envelope` accepts a provenance
  ref that `self.evidence.contains(ref)` **or** that is in
  `rejected_task_dispatch_refs`, before raising `unknown provenance reference`.
- Regression coverage already exists:
  `tests/test_relax_kernel.py:456-517`
  (`test_rejected_dispatch_provenance_is_checkpointed_immediately`) and
  `tests/test_relax_kernel.py:521+`
  (`test_envelope_level_rejection_is_not_citable`).
- The living doc agrees: `contract-burden-reduction.md:60` records item 6 as
  **landed (CB-01, `578ff4b5…`)**.

A red test written to AC-CB301-4's words would pass at RED_BASE. Precedent:
CB2-04 and CB2-07 were deleted for exactly this.

### C2 — the actual live provenance rejections are a *different* defect than CB3-01 fixes

I checked what the CB-2 coordinators actually cited. All three live
`unknown_evidence` rejections name entities that **exist in kernel state**, not
rejected dispatches:

- `logs/runs/cb2-graph/contract-burden-relaxation-2-attempt-3-CB2-08/events.jsonl`
  line 125 — `unknown provenance reference: task:impl-cb208-r2`. `impl-cb208-r2`
  was a real, dispatched, failed task.
- `…attempt-2-CB2-05/events.jsonl` — three rejections:
  `command:impl-cb205`, `system-result:impl-cb205:5`,
  `command:system-result:impl-cb205:5`.

None of these is a rejected `task.dispatch` command id. The gap is that
`_validate_envelope` (kernel:553-559) resolves provenance **only** against the
evidence catalog and `rejected_task_dispatch_refs` — never against
`self._state["tasks"]` or against the kernel's own event/result ids, all of
which are in the hash chain. CB3-01 as specified would not have prevented a
single one of the four observed rejections.

**Rework direction:** re-aim CB3-01 at resolving `task:<id>` for any task the
kernel knows, and `system-result:<task>:<n>` for a recorded result event. That
is the smallest change that closes the observed burden, and it is a strictly
smaller change than minting a new typed rejection-record namespace.

### C3 — CB3-01's AC-CB301-1 duplicates the existing rejection journal event

`controller_kernel.py:1249-1258` already appends a `command_rejected` audit
event carrying `{"command": command.as_dict(), "receipt": receipt.as_dict()}`
— i.e. the full refused payload and the rejection reason, through the
hash-chained journal, not a side channel. AC-CB301-1's "typed rejection record
carrying a stable provenance id, the rejection reason, and the refused
command's payload digest" is a re-implementation of that with a digest added.
The only genuinely absent piece is a *stable id*, and `command:<command_id>` is
already that id. **Gold-plating: cut AC-CB301-1.**

### C4 — CB3-01's AC-CB301-2 second clause is scope growth beyond item 6

"the retry/supersede admission logic accepts a rejection record as a valid
predecessor reference … no retry-budget charge is spent" is genuinely absent —
`_task_dispatch` requires `supersedes_task_id` to name an existing task with
`status == "failed"` (`controller_kernel.py:826-832`), and a rejected dispatch
never created a task. But item 6's action line is only "let rejected/failed
dispatches be referencable provenance"
(`contract-burden-reduction.md:59`). Predecessor semantics + retry-budget
accounting is a second mechanism in a node whose first mechanism is already
landed. If kept, it needs its own AC and its own specimen; there is none in
items 6, 17, 18, 19 or 20.

### C5 — CB3-02's defect is real and I located it exactly

The item-20 divergence is not vague; it is a two-line asymmetry:

- **Issuer** `feature_run.py:1546-1560` (`_dirty_baseline_receipt_ref`) tests
  coverage as `dirty <= receipted` over `changed_paths` **only**.
- **Enforcer** `claude_task_executor.py:508-513` tests the same `changed_paths`
  coverage **and then** per-file content:
  `all(dirty_files.get(path) == receipted_files.get(path) for path in dirty_paths)`.
  `controller_live.py:596-598` is a byte-equivalent second copy.

A receipt whose path set covers the dirty set but whose recorded `files`
content no longer matches disk is granted-and-journaled, then refused. That is
the CB2-03 specimen verbatim. **Red phase reachable. KEEP the node.**

### C6 — CB3-02 cannot fix its own specimen: `feature_run.py` is not in its owned paths

The plan names `_controller_dirty_baseline_grant` (`agent_mixture.py:286-306`)
as the issuer to unify. It is **not** the issuer that produced the specimen. I
resolved the actual journal:

```
contract-burden-relaxation-2-attempt-1-CB2-03/events.jsonl
  dirty_baseline_adoption_grant_supplied  actor=review-fix-controller
  → review_fix_failed: "writable worker requires a clean repository baseline"
```

`dirty_baseline_adoption_grant_supplied` is emitted at exactly one site,
`harness_labs/feature_run.py:1595`, from `_attach_dirty_baseline_grant`
(`feature_run.py:1565-1601`), reached via `_grant_aware_review_fix_factory` /
`_grant_aware_repair_factory` (`feature_run.py:1605-1660`); the actor string
`review-fix-controller` is `feature_run.py:1659`.

There are therefore **three** coverage implementations at RED_BASE, not two:
`feature_run.py:1528-1560`, `agent_mixture.py:308-340`
(`_best_covering_receipt`, a near-verbatim clone), and the two executor
preflights. AC-CB302-1 ("implemented exactly once in a shared helper") is
**unsatisfiable inside the declared grant**, and AC-CB302-4's red — which
reproduces the specimen — must exercise the `feature_run.py` issuer.

**Required:** add `harness_labs/feature_run.py` and `tests/test_feature_run.py`
to CB3-02's owned paths, or the node cannot honor its own AC or its own red.

### C7 — CB3-03's mechanism already exists; the node is aimed at the wrong layer

AC-CB303-1 describes: per-dispatch, controller resolves the covering
workspace-change receipt from this run's evidence and attaches the grant
without coordinator action, gated on role eligibility. That is a line-for-line
description of `_controller_dirty_baseline_grant`
(`agent_mixture.py:286-306`), which is invoked **inside** the per-task factory
closures at `agent_mixture.py:268` and `agent_mixture.py:278` — i.e. evaluated
per dispatch, over live `workspace_snapshot(repository)["changed_paths"]`, from
the run's own `EvidenceCatalog`. AC-CB303-3's "role-level eligibility still
required" is `agent_mixture.py:299`.

Why did the CB-2 program still hit item 17? Because its runner does not use
`build_role_profiles`. `experiments/run_burden2_plan_graph.py:412-427`
constructs `ClaudeSemanticTaskExecutor` directly, passing the **deprecated**
`allow_dirty_baseline=True` and no `dirty_baseline_grant`. That flag is inert
for the preflight: `claude_task_executor.py:167-168` calls
`_resolve_dirty_baseline_grant` unconditionally whenever
`sandbox == "workspace-write"` and the tree is dirty, and
`allow_dirty_baseline` is consulted nowhere in that path (only in the
constructor sandbox check, `claude_task_executor.py:100-101`).

So item 17's red **is** reachable — but only through a caller-supplied
`RoleProfile.executor_factory`. The one controller-owned chokepoint that sees
every coordinator-dispatched task is
`controller_scheduler.py:123` (`executor = profile.executor_factory(task)`),
and `harness_labs/controller_scheduler.py` is **not** in CB3-03's owned paths.
Without it, CB3-03 can only fix runs that already route through
`agent_mixture` — which are already fixed.

**Rework:** either (a) own `controller_scheduler.py` and attach the grant at
dispatch regardless of who authored the factory, or (b) shrink CB3-03 to
"journal the grant issuance in `_controller_dirty_baseline_grant`" and accept
that the runner must adopt `build_role_profiles` — a much smaller node. Either
way AC-CB303-1's "resolved and attached by the controller without coordinator
action" cannot be honored by editing `controller_kernel.py`, which never sees
an executor.

### C8 — CB3-04's trigger cannot fire for the specimen it claims (CB2-05)

AC-CB304-1 requires "a workspace-change receipt attests its residue" for an
attempt whose terminal status is **failed**. At RED_BASE the receipt is written
at `claude_task_executor.py:291-309` (mirror: `controller_live.py:390-…`),
which sits **after** every refusal check on the success path. Walking the
refusals in order:

| Failure | Site | Receipt written? |
|---|---|---|
| dirty preflight refusal (item 17 case) | `claude_task_executor.py:167-168` → `:508-511` | no (worker never ran) |
| nonzero exit / error envelope | `:246-254` | no |
| HEAD/branch change | `:258-261` | no |
| **out-of-grant edit (CB2-05 specimen)** | `:275-278` | **no** |
| require/forbid repository change | `:281-289` | no |
| **placeholder summary, deliverable floor (CB2-08 specimen)** | `:311-312` | **yes** (`:291`) |

So CB3-04 covers **exactly one** of the three item-19 triggers. AC-CB304-4
explicitly names "the CB2-05/CB2-08 specimens"; the CB2-05 half is unreachable
under CB3-04's own conjunctive gate, and its red test, if honest, will decline
restoration rather than perform it.

Either add an AC that emits the receipt on the refusal paths too (a change to
`claude_task_executor.py`, which CB3-04 **does** own), or narrow AC-CB304-4 to
CB2-08 alone and say so. Silently claiming both is a false closure of item 19.

Note in CB3-04's favour: no restoration mechanism exists anywhere — the only
occurrences of `restore` in `harness_labs/` are evidence/checkpoint restoration
(`controller_evidence.py:115`, `controller_run.py:149`), and
`git_transaction.py` only *asserts* cleanliness (`:147`, `:249`). The core
defect is real.

### C9 — CB3-04 cannot observe its own trigger conditions from its owned paths

AC-CB304-1 gates on "the attempt's terminal status is failed" and "no newer
attempt has started". Neither fact exists inside `claude_task_executor.py` or
`controller_live.py` — those raise `LiveExecutionError` and return; attempt
lifecycle and ordering live in `feature_run.py` (`runner.run(attempt, …)`,
`feature_run.py:1884`, `:2221`) and the kernel's task state
(`controller_kernel.py:305-345`, `record_task_results`). As with C6/C7, the
node's owned paths do not contain the code that can make the decision.

### C10 — CB3-05: DELETE. The claimed session death does not occur at RED_BASE

AC-CB305-4's red is "an `operator_input.request` in a channel-less run leaves
the question unanswered and the run dies on session error rather than resolving
to a typed response." I traced the only live specimen and it contradicts this.

`controller_kernel.py:1320-1322`:

```python
elif event_type == "operator_input.requested":
    self._state["operator_questions"].append(copy.deepcopy(dict(payload)))
    self._state["status"] = "blocked"
```

The kernel answers the request **synchronously** by blocking the run and
preserving the question in state. Nothing awaits input; nothing hangs.

The journal
`logs/runs/cb2-graph/contract-burden-relaxation-2-attempt-3-CB2-08/events.jsonl`
confirms end to end:

- line 134 — `controller_event: operator_input.requested`, revision 9, full
  question text in payload (first-class, hash-chained evidence);
- line 135 — `command_processed` (accepted, not hung);
- tail-3 — `backend_process_terminated` `returncode 143`, i.e. the harness
  tearing the session down after the block, not a crash;
- tail-2 — `coordinator.session_ended` with
  `"outcome": "blocked", "run_status": "blocked"`;
- tail-1 — `run_failed {"terminal_status": "blocked"}`.

That is precisely the end state AC-CB305-2 promises to deliver: terminal
`blocked`, through the existing escalation path, with the question preserved as
evidence. The string `error_during_execution` appears **nowhere** in
`harness_labs/` or `experiments/`.

What CB3-05 would add is a typed `operator_unavailable` response event —
naming, not behavior. Item 19's "which dead-ends in a headless run" describes
a *coordinator ergonomics* complaint (it gets no answer), not a harness defect;
the harness's answer is the block. Under the CB2-04/CB2-07 precedent (a node
whose red phase cannot fail must be deleted), **CB3-05 should be deleted** and
item 19's escalation half withdrawn or re-diagnosed against a journal that
actually shows a session error.

If the coordinator ever did die, the burden is elsewhere: `operator_questions`
is projected to the coordinator (`controller_projection.py:116`) but there is
no response verb in `COORDINATOR_COMMANDS` — that is a different node with a
different red.

### C11 — CB3-06's AC-CB306-2 is genuine, but 90% of the mechanism already exists

Out-of-grant findings are already screened at RED_BASE:

- `review_fix.py:503-511` — each finding is stamped
  `scope_expanding = finding.scope_expanding or paths_outside_scope(finding["required_paths"], self.allowed_paths)`;
- `review_fix.py:283-289` — a `scope_expanding` record is set to outcome
  `scope_screened` and `continue`d;
- `review_fix.py:302-304` — `fix_keys` only includes records whose outcome is
  `open`, so a screened finding never becomes a fix obligation;
- `review_fix.py:176-208` — `transfer_scope_expanding` routes it to a
  downstream unique owner.

Two real holes remain, and both are reachable reds:

1. **The `file` anchor is never checked.** `_new_record` stores
   `"file": str(finding.get("file", ""))` (`review_fix.py:388`) and
   `_finding_key` keys on it (`review_fix.py:844`), but the only
   `paths_outside_scope` call in the module (`review_fix.py:505`) inspects
   `required_paths` alone. A finding anchored to the plan document via `file`
   with an empty `required_paths` — exactly the CB2-02 attempt-1 shape — sails
   past the guard, becomes `open`, and recurs to the ceiling.
2. **`contract_violation` / `requires_disposition` bypass the guard**
   (`review_fix.py:285-287`). A reviewer who marks an out-of-grant finding as a
   contract violation still mints an undischargeable obligation.

**Smallest change:** extend the `review_fix.py:505` predicate to the `file`
anchor, and decide whether the two bypass flags may override an out-of-grant
anchor. Reuse the existing `scope_screened` outcome — it already satisfies
"excluded from fix_keys", "visible in the cycle report" (it is in
`ledger.as_dict()`), and "not an open obligation". AC-CB306-2's demand for a
*new* "out-of-grant annotation" record type and separate journaling is
gold-plating over a working mechanism.

### C12 — CB3-06's AC-CB306-1 has no specimen

The journal-receipt discharge verb is a whole new mechanism: fix-stage return
shape, evidence-catalog resolution, controller-ownership check, a new
`discharged-by-receipt` outcome, plus an anti-self-discharge rule
(AC-CB306-3). Item 18's *only* recorded specimen
(`contract-burden-reduction.md:188-191`) is the out-of-grant anchor — the half
C11 covers. "An obligation already satisfied by journaled evidence"
(`:192-195`) is stated as a diagnosis with no run evidence behind it, and no
CB-2 block is attributed to it.

Recommend splitting AC-CB306-1 out and deferring it (the plan already has a
"Deferred out of this program" list, rule 8) unless a reviewer can produce a
journal where an obligation was undischargeable *because* its evidence was a
controller artifact. Landing both halves in one node also makes the red tail
ambiguous about which defect the gate proved.

### C13 — CB3-07 mandates re-landing an already-landed item

AC-CB307-1 requires items 6, 17, 18, 19, 20 to each carry "a struck-through
prior status and a landed status naming the closing node (CB3-01…CB3-06)".
Item 6's status at RED_BASE is already
`**Status:** landed (CB-01, commit 578ff4b5…)` (`contract-burden-reduction.md:60`)
— it is not open, so there is no open status to strike, and re-landing it under
CB3-01 would falsify the record (see C1: the item-6 action line is done).
Similarly, "no item this program claims remains marked open" is trivially true
for item 6 today.

If C1/C2 are accepted and CB3-01 is re-aimed at task/result provenance
resolution, that is a **new** worklist item (call it 21), not item 6, and
AC-CB307-1 should say so. If CB3-05 is deleted (C10), item 19's escalation half
must be recorded as *withdrawn* — the CB2-04/item-15 pattern — not as landed.

---

## Mechanism-level findings

### M1 — with `max_parallelism = 2`, nearly every admissible pair collides on a shared file

Owned-path intersections among nodes the DAG permits to run concurrently:

| Pair (DAG-legal) | Shared owned files |
|---|---|
| CB3-01 ∥ CB3-05 | `controller_kernel.py`, `tests/test_controller_kernel.py` |
| CB3-02 ∥ CB3-05 | `controller_live.py`, `tests/test_controller_live.py` |
| CB3-03 ∥ CB3-04 | `controller_live.py`, `tests/test_controller_live.py` |
| CB3-03 ∥ CB3-05 | `controller_kernel.py`, `controller_live.py`, both test modules |
| CB3-04 ∥ CB3-05 | `controller_live.py`, `tests/test_controller_live.py` |

Only CB3-06 (`review_fix.py`) is file-disjoint from every other node. The plan
states "Roots CB3-01, CB3-02, CB3-05, CB3-06 admit in any pair order" — two of
those three pairs collide. CB-2's adjudication accepted "DAG same-file ordering
is sound" because CB-2's DAG serialized every same-file pair; CB-3's does not.

Fix: add ordering edges (CB3-05 after CB3-01 and after CB3-02; CB3-04 after
CB3-03) or set `max_parallelism = 1`. If CB3-05 is deleted per C10, three of
the five collisions vanish and only CB3-03 ∥ CB3-04 needs an edge.

### M2 — the two grant *issuers* are already duplicates of each other

`feature_run.py:1528-1560` (`_dirty_baseline_receipt_ref`) and
`agent_mixture.py:308-340` (`_best_covering_receipt`) are near-verbatim clones
— same docstring paragraph, same tightest-covering selection, differing only in
`agent_mixture`'s deterministic ref tie-break and its `if not dirty` guard
placement. CB3-02's unification is therefore *more* necessary than the plan
argues (three copies, not two), and correspondingly more blocked by C6's
missing owned path. Worth stating explicitly in the node so the implementer
does not delete one clone and leave the other.

### M3 — `allow_dirty_baseline` is already dead as a bypass; the keep-list clause is misleading

Program rule 4 keeps "the executor's refusal of a dirty baseline **absent a
valid grant or restoration receipt**". At RED_BASE the only way past the
refusal is a covering, content-matching `dirty_baseline_grant`
(`claude_task_executor.py:167-168`, `:508-513`); `allow_dirty_baseline` is
consulted only in the constructor sandbox assertion
(`claude_task_executor.py:100-101`, `controller_live.py:198-199`,
`agent_mixture.py:144-145`) and in `_controller_dirty_baseline_grant`'s
eligibility test (`agent_mixture.py:299`). Reviewers should know that removing
the flag entirely from the executors weakens nothing — several nodes touch
these files and an implementer may reasonably want to.

### M4 — no node owns the runner, yet CB3-03's fix may require a runner change

Precondition 3 freezes `experiments/run_burden3_plan_graph.py` as unowned. But
per C7 the item-17 defect lives in how a runner authors
`RoleProfile.executor_factory` (`run_burden2_plan_graph.py:412-427`). If CB3-03
takes route (b) — journal-only in `agent_mixture` — the CB-3 runner must adopt
`build_role_profiles` to benefit, which is a runner change, which is an
operator instruction pin. Say this explicitly, or take route (a)
(`controller_scheduler.py`) so the fix is runner-independent.

---

## Self-refutations

### R1 — "CB3-02 is redundant with CB3-03, both mint grants" — rejected

They are disjoint. CB3-02 fixes *issuer/enforcer disagreement* (a grant is
minted and refused: `feature_run.py:1546-1560` vs
`claude_task_executor.py:508-513`). CB3-03 fixes *no grant minted at all* for
factories that never call `_controller_dirty_baseline_grant`. The CB-2 journals
show both independently:
`attempt-1-CB2-03` has `dirty_baseline_adoption_grant_supplied` **followed by**
the refusal (item 20); `attempt-3-CB2-08` has the refusal with **no** grant
event at all (item 17). Both nodes earn their place.

### R2 — "CB3-02 landing makes CB3-03's red unreachable (ordering hazard)" — rejected

I expected CB3-02's stricter shared verifier to suppress grant issuance and so
change CB3-03's observable. It does not: CB3-03's red runs against the frozen
RED_BASE tree (Preconditions 2), and on the joined candidate the CB3-03 test
asserts a grant *is* attached where none was — a strictly additive assertion
that a stricter verifier does not contradict, since CB3-03's own AC-CB303-2
requires refusal when coverage is absent. No hazard. (The genuine hazard is
M1's file collision, not semantic.)

### R3 — "CB3-04 is unnecessary because CB3-03's adoption grant already unblocks a dirty tree" — rejected

Adoption and restoration are different: adoption requires a receipt whose
`files` content matches disk **and** requires the residue to be adoptable work.
AC-CB304-3 correctly separates them (successful residue is adoption material;
failed residue is restoration material). And the CB2-08 journal proves a case
where no grant could be minted and the coordinator had no shell:
`"The coordinator has no shell access to clean the tree, and every writable
worker dispatch will keep failing at that harness precondition."` The node is
warranted — my objection is C8's precondition reachability, not the node's
existence.

### R4 — "CB3-06's AC-CB306-2 is unreachable, `scope_screened` already handles it" — rejected

This was my strongest DELETE candidate for CB3-06, and I killed it. The guard
at `review_fix.py:505` reads `required_paths` only; `file` is never tested
against `allowed_paths` anywhere in the module (verified by grepping every
`paths_outside_scope` call site in `harness_labs/`). A `file`-anchored,
`required_paths`-empty finding — the CB2-02 shape — reaches `outcome: "open"`
and recurs. The red is reachable. The finding survives only as SHRINK (C11),
not DELETE.

### R5 — "item 6's `command:` ref format means the coordinator just needs a prompt pin" — rejected

I considered arguing that C2's residual is an ergonomics problem fixable with
an instruction pin telling coordinators to cite `command:<command_id>`. But
`command:impl-cb205` in `attempt-2-CB2-05` shows a coordinator that already
guessed the `command:` prefix and still failed, because it prefixed a *task*
id. And `system-result:impl-cb205:5` is a ref the coordinator read out of the
projection. The vocabulary mismatch is structural — the kernel exposes ids it
will not resolve — so a mechanism fix (C2's rework direction) is correct, not a
pin.

### R6 — "CB3-05 is still needed because `operator_input.requested` sets status blocked but the coordinator loop spins" — rejected

I looked for evidence of a spin or a timeout after the block. There is none:
`attempt-3-CB2-08` goes `operator_input.requested` (rev 9) →
`backend_process_terminated` → `coordinator.session_ended outcome=blocked`
(rev 10) → `run_failed` within the same journal, with no intervening retry,
timeout, or error event. The teardown is orderly. CB3-05's DELETE verdict
holds.

### R7 — "CB3-07 is pure documentation and needs no scrutiny" — rejected

C13 stands: AC-CB307-1 encodes a factual claim about item 6's prior status that
is false at RED_BASE, and the sink is where a program's closure claims become
the next program's diagnosis input. CB-2's own history shows the cost — item 15
had to be *withdrawn* in the living doc after its node was deleted. If CB3-01
and CB3-05 change verdict, CB3-07's ACs must change with them.
