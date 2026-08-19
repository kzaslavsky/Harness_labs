# CC-08 — In-graph escalation and bounded unsealing

Status: proposed
Date: 2026-08-19
Decision: [`../decisions/0007-in-graph-escalation-bounded-unsealing.md`](../decisions/0007-in-graph-escalation-bounded-unsealing.md)
Base: `1462892` (branch `claude/convergence-harness-impl-fe83e1`)

This plan implements ADR 0007. It is written to be registerable as a
`plan-graph-plan/1` decomposition: every section carries an anchor a node
may cite in `plan_sections`, and every acceptance criterion below is a
single concrete, testable statement in the style of
[`convergence-campaign-decomposition.json`](convergence-campaign-decomposition.json).

Scope note for every node: consumers import by full module path;
`harness_labs/__init__.py` is deliberately out of scope. Nothing added
under `harness_labs/core/` may import `harness_labs.plangraph`.

## Problem and current behaviour [cc08-problem]

A node's fixer may write only its registered `allowed_paths`. A reviewer
finding whose repair requires another node's paths therefore has no legal
destination, and the loop re-finds it until the cycle budget is gone. The
motivating incident, its artifacts, and the verified defect in sealed
candidates `11cda5c` / `49ba00d` are recorded in ADR 0007 and are not
repeated here.

What the working tree does today, verified:

- `harness_labs/featurerun/review_fix.py:616-628` (`ReviewFixLoop.run`)
  sets `scope_expanding` from
  `paths_outside_scope(finding["required_paths"], self.allowed_paths)`.
- `ReviewLedger.ingest` (`review_fix.py:300-344`) screens a
  scope-expanding finding to `scope_screened` **only** when it is neither
  `contract_violation` nor `requires_disposition`. A required
  out-of-scope finding stays `open` and enters `fix_keys`.
- `ReviewLedger.transfer_scope_expanding` (`review_fix.py:196-234`)
  routes such a finding to a pre-bound owner via
  `_target_for_path(path, targets)`; `targets` comes from
  `PlanGraph._transfer_targets_for` (`plan_graph.py:2388-2415`), which
  walks **dependents only**. A predecessor node is never a candidate
  owner, so `owners` is empty and the finding is skipped.
- `PlanGraph._advance_finding_obligations` (`plan_graph.py:2283-2286`)
  raises `PlanGraphError` when a transfer target is `in completed` or is
  the current run. Routing to a sealed owner is presently prohibited.
- `ReviewFixLoop.run` always begins each cycle with a `review` stage
  (`review_fix.py:614`), so there is no way to run fix+verify only.

CC-08 changes exactly these five facts and nothing else.

## Design [cc08-design]

### Recognition [cc08-recognition]

Two deterministic entry points, both before another fix cycle is spent:

1. **Ingest-time set check.** After `transfer_scope_expanding` runs (so a
   legitimate downstream owner keeps priority), any finding still
   `outcome == "open"` whose `required_paths` is non-empty and for which
   `paths_outside_scope(required_paths, allowed_paths)` is non-empty
   becomes `outcome = "escalated"`, `escalation_reason =
   "required_paths_outside_grant"`. Escalated keys are excluded from
   `fix_keys`.
2. **Fixer declaration.** The `review-fix-fix/1` stage result gains an
   optional `unresolvable_finding_keys` list. Keys in it become
   `outcome = "escalated"`, `escalation_reason =
   "fixer_declared_unresolvable"`. A key not present in the stage's
   `fix_finding_keys` raises `ReviewFixError`, matching the discipline
   `ReviewLedger.mark_fix_attempt` already applies to
   `addressed_finding_keys`.

### Packet [cc08-packet]

No new packet schema. The packet is the verbatim review-ledger finding
record (as produced by `ReviewLedger._new_record`, identity fields
intact) carried out through `ReviewFixResult.escalated_findings`, plus
one optional `escalations` array added to the existing
`schemas/block-escalation.json` (`plan-graph-block-escalation/1`). That
schema declares `required` but not `additionalProperties: false`, so the
addition is backward compatible and existing escalations still validate.
Every reference in the packet is content-addressed
(`artifact:sha256:…`), recorded through
`PlanGraphAudit.record_block_escalation`.

The one new versioned record is the judgment,
`plan-graph-escalation-judgment/1`, in
`schemas/plan-graph-escalation-judgment.json`.

### Judgment [cc08-judgment]

An `EscalationJudge` callable receives the packet and returns a judgment
with `verdict ∈ {"confirm", "reject"}`, a rationale, and evidence
references. It is a coordinator seat or a fresh session and MUST be
independent of the reviewer whose finding is being judged; the controller
refuses when the configured judge identity equals the packet's recorded
`origin_reviewer_id`. The judge is optional: `escalation_judge=None`
means the feature is off and behaviour is byte-identical to today.

`reject` returns the key to the escalating node's own obligations with
`outcome = "open"` and the rationale in `outcome_reason`, delivered
through the existing `finding_obligations` →
`ReviewFixLoop(inherited_findings=…)` → `fix_finding_keys` channel.

### Routing [cc08-routing]

Routing is a pure lookup, never a model call. `PlanGraph._owner_for_paths`
builds `{path: node_id}` from **every** `PlanRun.allowed_paths` in the
plan and resolves with the existing longest-prefix rule in
`review_fix._target_for_path`. Zero owners, or two or more distinct
owners, blocks to the human operator with
`decision_request.requested_action == "assign_finding_owner"`.

### Disposition on confirm [cc08-disposition]

- **Owner has not yet run.** The record is appended to
  `finding_obligations[owner]`. `PlanGraph._request_for_run` already sets
  `inherited_ledger_frozen=True` when `origin_node != run.id`. No
  authority is spent.
- **Owner is sealed — unseal.** The record is appended to
  `finding_obligations[owner]` with `bounded_fix_only: true`, one
  `transfer_ownership` recovery decision is spent, and the graph blocks
  with an escalation whose `resume_directive_template.retry_frontier` is
  `[owner, escalating_node]` in stable plan order. The successor attempt
  reads `finding_obligations` from the predecessor checkpoint (already
  the case, `PlanGraph._load_finding_obligations`) and launches the owner
  with `FeatureRunRequest.bounded_fix_only=True`.

`ReviewFixLoop(bounded_fix_only=True, seeded_fix_keys=(…))` seeds the
ledger via `seed_transferred`, calls `freeze_discovery()`, and runs
exactly one `fix` then one `verify` stage over the seeded keys. It never
constructs a `review` stage attempt and never calls `ReviewLedger.ingest`,
so opening a new finding is structurally impossible rather than merely
discouraged.

### Bounds, authority, audit [cc08-authority]

Each unseal spends exactly one `transfer_ownership` action through
`RetryBudgetLedger.apply_recovery_decision`, `target = escalating node`,
`payload = {"receiving_node": owner}`. That action is already in
`ACTION_TYPES` and `STRUCTURAL_ACTIONS`
(`harness_labs/plangraph/plan_graph_authority.py:17-22`), is already
metered by `AutomaticRecoveryAuthority.max_structural_decisions`, and
already appends `obligation_transferred` with `reverification_required:
true`. Exhaustion raises
`BudgetError("structural recovery allowance exhausted")`.

**No new event kind is added to the retry-budget ledger.** Its `_fold`
ends in `else: raise ValueError`
(`plan_graph_budget.py:780`), so an unknown kind would make the whole
lineage unreplayable. Escalation events go on the PlanGraph audit journal
instead, via `self.journal.append(...)` in the manner of
`PlanGraphAudit._adopt_seal`:

- `plan_graph_finding_escalated`
- `plan_graph_escalation_judged`
- `plan_graph_node_unsealed`

### Stated cascade consequence [cc08-cascade]

`PlanGraphAudit.repair_selection` (`plan_graph_audit.py:344-400`)
computes `invalidated_node_ids` as the frontier plus its transitive
dependents and excludes them from `reused_completed`. Reopening a sealed
node therefore re-runs every dependent. The unseal bounds the reopened
node's **ledger** scope; it does not bound downstream re-execution.

## Acceptance criteria [cc08-criteria]

| ID | Criterion |
|---|---|
| AC-CC08-1 | With `ReviewFixPolicy(escalation_enabled=False)` (the default), `ReviewFixLoop.run()` over the existing `tests/test_review_fix.py` fixtures produces a `ReviewFixResult.as_dict()` identical to the pre-change output except for one added key `"escalated_findings": []`; no ledger record gains an `escalation_reason` value other than `""`. |
| AC-CC08-2 | With escalation enabled, a first-cycle review finding carrying `requires_disposition: true` and a `required_paths` entry outside `allowed_paths`, for which `finding_transfer_targets` resolves no owner, ends that cycle with `outcome == "escalated"` and `escalation_reason == "required_paths_outside_grant"`; the recording executor factory is never invoked with `stage == "fix"` for that key. |
| AC-CC08-3 | When `finding_transfer_targets` does resolve a unique downstream owner for the same finding, `transfer_scope_expanding` still claims it: `outcome == "transferred"`, `transferred_to == <owner>`, and `escalation_reason == ""`. Transfer takes precedence over escalation. |
| AC-CC08-4 | A `review-fix-fix/1` result whose `unresolvable_finding_keys` is a subset of its `fix_finding_keys` marks exactly those keys `escalated` with `escalation_reason == "fixer_declared_unresolvable"`; a key outside `fix_finding_keys` raises `ReviewFixError` whose message names the offending key. |
| AC-CC08-5 | `ReviewFixLoop(bounded_fix_only=True, seeded_fix_keys=("k",), inherited_findings=(<record for k>,))` invokes its executor factory exactly twice, with `stage == "fix"` then `stage == "verify"`, both carrying `fix_finding_keys == ["k"]`; no attempt id matches `*/review-fix/c*/review`; the resulting ledger's `findings` mapping has exactly one key; `ReviewFixResult.cycles == 1`. |
| AC-CC08-6 | `PlanGraph._owner_for_paths(("scripts/run_convergence_campaign.py",))` returns the node whose `allowed_paths` covers it by longest prefix, including when that node is a *predecessor* of the escalating node; it returns `None` for a path no node claims and for a path two distinct nodes claim at equal prefix length. A judge stub that raises on call proves routing consults no model. |
| AC-CC08-7 | A `PlanGraph` configured with an `escalation_judge` whose identity equals the packet's `origin_reviewer_id` raises `PlanGraphError` naming reviewer independence before the judge is invoked. |
| AC-CC08-8 | A `reject` judgment writes the key back to `finding_obligations[<escalating node>]` with `outcome == "open"` and the judge rationale contained in `outcome_reason`; `RetryBudgetLedger.deviation_records()` is unchanged before and after, proving no structural decision was spent. |
| AC-CC08-9 | A `confirm` judgment whose owner has status `queued` appends the record to `finding_obligations[<owner>]`; that node's next `FeatureRunRequest` has `inherited_ledger_frozen is True` and `bounded_fix_only is False`; `deviation_records()` is unchanged. |
| AC-CC08-10 | A `confirm` judgment whose owner is sealed appends exactly one `recovery_decision` event with `action == "transfer_ownership"`, `target == <escalating node>`, `payload == {"receiving_node": <owner>}`, followed by one `obligation_transferred` event; the folded `automatic_recovery_structural_decisions` increases by exactly one. A second unseal against `max_structural_decisions == 1` raises `BudgetError("structural recovery allowance exhausted")` and the graph blocks with `decision_request.required is True`. |
| AC-CC08-11 | The obligation record written for an unsealed owner carries `bounded_fix_only: true`; `PlanGraph._request_for_run` sets `FeatureRunRequest.bounded_fix_only=True` for that node and `False` for every other node in the same attempt. |
| AC-CC08-12 | The emitted `escalation.json` validates against `schemas/block-escalation.json`; its `escalations[0]` contains `finding_key`, `origin_node`, `owner_node`, `required_paths`, and a `judgment_ref` matching `artifact:sha256:[0-9a-f]{64}`; its `resume_directive_template.retry_frontier` equals `[<owner>, <escalating node>]` in plan-declaration order. |
| AC-CC08-13 | The graph journal contains `plan_graph_finding_escalated`, `plan_graph_escalation_judged`, and `plan_graph_node_unsealed` in that order, each payload carrying the same `finding_key`; re-opening the lineage's `RetryBudgetLedger` and folding it raises nothing, proving no new budget event kind was introduced. |
| AC-CC08-14 | An escalated finding whose `required_paths` resolve to zero owners, or to two distinct owners, produces a block whose `decision_request.requested_action == "assign_finding_owner"` and whose `candidate_actions` lists the candidate owner ids (empty list for the zero-owner case); `deviation_records()` is unchanged and no judge is invoked. |
| AC-CC08-15 | After resuming with `retry_frontier == [<owner>, <escalating node>]`, `PlanGraphAudit.repair_selection` returns every transitive dependent of `<owner>` in `invalidated_node_ids` and omits each of them from `reused_completed`. |
| AC-CC08-16 | `python3 -m pytest tests/test_import_boundaries.py -q` passes: no module added or modified under `harness_labs/core/` appears in the plangraph layer's import closure, and `feature_run`'s static import closure remains free of plangraph-layer modules. |
| AC-CC08-17 | `python3 -m pytest tests/ -q` passes at the finalize gate with no test skipped for reasons introduced by this change. |

## Runtime contracts [cc08-runtime-contracts]

| Contract | Type | Write site | Read site | Verification |
|---|---|---|---|---|
| `escalation_enabled` | policy field | `harness_labs/featurerun/review_fix.py` `ReviewFixPolicy` | `ReviewLedger.ingest` / `ReviewFixLoop.run` | `ReviewFixPolicy(escalation_enabled=True)` constructs; `asdict(policy)` in the persisted ledger contains the key (AC-CC08-1) |
| `escalated` (ledger outcome) | enum value | `ReviewLedger.escalate_out_of_grant` | `ReviewLedger.open_all` must exclude it; `ReviewFixResult.escalated_findings` | AC-CC08-2, AC-CC08-4 |
| `escalation_reason` | record field | `ReviewLedger._new_record` default `""` | packet construction in `plan_graph.py` | AC-CC08-2, AC-CC08-4 |
| `unresolvable_finding_keys` | stage detail | fix-stage executor result `details` | `ReviewFixLoop.run` via `_detail_keys` | `_stage_output_contract("fix")` lists it as optional; AC-CC08-4 |
| `escalated_findings` | result field | `ReviewFixResult.as_dict()` | `PlanGraph._escalated_findings(outcome)` reading `outcome.evidence["review_fix"]["escalated_findings"]` | AC-CC08-2 plus a PlanGraph-side reader test |
| `bounded_fix_only` | request field + obligation field | `PlanGraph._request_for_run`; obligation record | FeatureRun launcher payload → `ReviewFixLoop(bounded_fix_only=…)` | AC-CC08-5, AC-CC08-11 |
| `plan-graph-escalation-judgment/1` | protocol | judge callable return value | `PlanGraph` judgment validator | schema file exists and the validator rejects a wrong `protocol` string |
| `escalations` | escalation-artifact field | `PlanGraph._transition_to_blocked` escalation dict | operator / `run_plan_graph.py run --resume` | AC-CC08-12 |
| `transfer_ownership` | recovery action (**existing**) | `PlanGraph` unseal path calling `RetryBudgetLedger.apply_recovery_decision` | `plan_graph_budget._fold` `recovery_decision` / `obligation_transferred` branches | AC-CC08-10; registration must list it in `automatic_recovery.allowed_actions` |
| `--retry-frontier` | CLI flag (**existing**) | operator, or `escalation.json` `resume_directive_template` | `scripts/run_plan_graph.py` `run` subparser | `python3 scripts/run_plan_graph.py run --help` lists `--retry-frontier`; it is `action="append"`, so the frontier is repeated flags, not one comma list |
| `--on-block-argv` | CLI flag (**existing**) | campaign driver / operator | `scripts/run_plan_graph.py` `run` subparser | `run --help` lists `--on-block-argv` |

Near-neighbour disambiguation, called out deliberately:

- **`transfer_ownership` vs `transfer_scope_expanding`.** The first is a
  `plan-graph-recovery-decision/1` action metered by
  `max_structural_decisions`. The second is a `ReviewLedger` method that
  moves a finding to a pre-bound downstream owner and spends no
  authority. CC-08 uses `transfer_scope_expanding` for the
  already-working downstream case and `transfer_ownership` only for the
  unseal.
- **`scope_expanding` vs `anchor_out_of_grant`.** `scope_expanding` is
  derived from `required_paths`; `anchor_out_of_grant` from the single
  `file` anchor. Escalation keys off `required_paths`, i.e.
  `scope_expanding`. In the motivating record `anchor_out_of_grant` was
  `false` while `scope_expanding` was `true`.
- **`escalation_enabled` vs `scope_expansion_guard_enabled`.** The
  existing guard screens *non-required* scope-expanding findings to
  `scope_screened`. CC-08's switch is separate and governs only the
  escalation route.
- **`max_structural_decisions` vs `structural_decision_limit`.** The
  first is the registration's `AutomaticRecoveryAuthority` field, and is
  the meter CC-08 spends against. The second is a `BudgetConfig`
  per-node classification limit and is *not* involved.

## Build order [cc08-build-order]

Registerable as four nodes. Path grants are disjoint, so S1/S2 hold.

### CC-08-1 Review-fix escalation primitives [cc08-node-1]

Add `ReviewFixPolicy.escalation_enabled`; the `escalated` outcome and
`escalation_reason` record field; `ReviewLedger.escalate_out_of_grant`;
`unresolvable_finding_keys` in `_stage_output_contract("fix")` and its
handling in `ReviewFixLoop.run`; `ReviewFixResult.escalated_findings`
and its `as_dict()` key. Ordering inside `run()` is load-bearing:
`ingest` → `transfer_scope_expanding` → `escalate_out_of_grant` →
`fix_keys` recomputation.
Files: `harness_labs/featurerun/review_fix.py`,
`tests/test_review_fix.py` (modify).
Criteria: AC-CC08-1, AC-CC08-2, AC-CC08-3, AC-CC08-4.
Verification: `python3 -m pytest tests/test_review_fix.py -q`.
Depends on: nothing.

### CC-08-2 Bounded fix-only loop [cc08-node-2]

Add `bounded_fix_only` and `seeded_fix_keys` to `ReviewFixLoop`; a
`run()` branch that seeds, freezes discovery, and runs one fix and one
verify stage with no review stage and no `ingest` call; the
`FeatureRunRequest.bounded_fix_only` field and its wiring into
`run_feature_worktree`'s `ReviewFixLoop` construction
(`feature_run.py:1050-1065`).
Files: `harness_labs/featurerun/review_fix.py`,
`harness_labs/featurerun/feature_run.py`, `tests/test_review_fix.py`
(modify), `tests/test_feature_run.py` (modify).
Criteria: AC-CC08-5, AC-CC08-16.
Verification:
`python3 -m pytest tests/test_review_fix.py tests/test_feature_run.py tests/test_import_boundaries.py -q`.
Depends on: CC-08-1.

### CC-08-3 Routing, judgment, and packet [cc08-node-3]

Add `PlanGraph._escalated_findings`, `PlanGraph._owner_for_paths`, the
`EscalationJudge` protocol and `escalation_judge` constructor kwarg, the
judgment validator and reviewer-independence refusal, the reject
write-back path, the `escalations` array in the block-escalation payload,
the `schemas/plan-graph-escalation-judgment.json` schema, the optional
`escalations` property in `schemas/block-escalation.json`, and the three
journal events on `PlanGraphAudit`.
Files: `harness_labs/plangraph/plan_graph.py`,
`harness_labs/plangraph/plan_graph_audit.py`,
`schemas/plan-graph-escalation-judgment.json`,
`schemas/block-escalation.json` (modify), `tests/test_plan_graph.py`
(modify).
Criteria: AC-CC08-6, AC-CC08-7, AC-CC08-8, AC-CC08-12, AC-CC08-13,
AC-CC08-14.
Verification: `python3 -m pytest tests/test_plan_graph.py -q`.
Depends on: CC-08-1.

### CC-08-4 Unseal, authority, and cascade [cc08-node-4]

Relax `_advance_finding_obligations`'s completed-target refusal for
escalation-routed records only; append the `bounded_fix_only` obligation
field; spend one `transfer_ownership` decision via
`RetryBudgetLedger.apply_recovery_decision`; author the
`resume_directive_template.retry_frontier`; propagate `bounded_fix_only`
through `_request_for_run`; assert the cascade through
`repair_selection`.
Files: `harness_labs/plangraph/plan_graph.py`,
`tests/test_plan_graph.py` (modify),
`tests/test_plan_graph_budget.py` (modify — the budget-event assertions;
note the authority *validator* suite is `tests/test_plan_graph_authority.py`,
and there is no `tests/test_plan_graph_recovery_authority.py`).
Criteria: AC-CC08-9, AC-CC08-10, AC-CC08-11, AC-CC08-15, AC-CC08-17.
Verification:
`python3 -m pytest tests/test_plan_graph.py tests/test_plan_graph_budget.py -q`.
Depends on: CC-08-2, CC-08-3.

Dependencies are data dependencies: CC-08-2 extends the loop CC-08-1
defines; CC-08-3 reads the `escalated_findings` CC-08-1 emits; CC-08-4
consumes both the bounded loop and the routing/judgment surface.

## Tests [cc08-tests]

`tests/test_review_fix.py` (modify): default-off byte-identity; ingest
escalation vs transfer precedence; fixer declaration including the
out-of-list error; bounded fix-only stage sequence and ledger size.

`tests/test_plan_graph.py` (modify): owner lookup over the full plan
including predecessors, zero-owner and ambiguous-owner refusals with a
raising judge stub; reviewer-independence refusal; reject write-back;
confirm-into-queued-owner; confirm-into-sealed-owner unseal with the
exact budget events; escalation artifact shape and retry frontier;
journal event order; cascade through `repair_selection`.

`tests/test_import_boundaries.py`: unchanged, must still pass.

The finalize gate runs the full suite (`python3 -m pytest tests/ -q`).

## Rollout and defaults [cc08-rollout]

`escalation_enabled` defaults to `False` and `escalation_judge` defaults
to `None`. A registration without `transfer_ownership` in
`automatic_recovery.allowed_actions` cannot unseal; it degrades to
today's behaviour (the finding stays open, the node blocks) rather than
failing differently. First live use is the convergence campaign, whose
registration should carry
`allowed_actions ⊇ {"resume", "transfer_ownership"}` and
`max_structural_decisions: 1`.

## Risks [cc08-risks]

| Risk | Mitigation |
|---|---|
| A dishonest or over-broad `required_paths` becomes a scope-widening lever | Escalation never widens the escalating node's grant; it can only route to an *existing* registered owner. An unroutable claim blocks to the operator (AC-CC08-14). |
| Judge rubber-stamps every escalation | `max_structural_decisions` caps unseals per lineage regardless of judge behaviour; ADR 0007 names a near-100% confirm rate as a reversal signal. |
| Cascade cost exceeds the attempt it saved | Stated, not hidden: AC-CC08-15 makes the invalidation closure an asserted property so the cost is visible in the escalation artifact before resume. |
| A new budget event kind silently breaks lineage replay | No new kind is added; AC-CC08-13 asserts the lineage still folds. |
| `bounded_fix_only` leaks into ordinary runs | AC-CC08-11 asserts it is `False` for every node other than the unsealed one in the same attempt. |
| Ordering regression: escalation steals a finding a downstream owner should get | AC-CC08-3 pins transfer precedence over escalation. |

## Deferred, with triggers [cc08-deferred]

| Deferred | Trigger |
|---|---|
| Multi-owner escalation (one finding split across two owners) | A confirmed escalation whose `required_paths` genuinely span two nodes' grants |
| In-attempt hot unseal without a successor attempt | Reuse receipts become attempt-independent |
| Retiring `_operator_note` from `experiments/run_convergence_plan_graph.py` | One campaign completes with zero operator notes written |
| Judge-panel (two independent judges, disagreement escalates) | First confirmed escalation later shown to be wrong |
| Escalation surfacing in the live dashboard | An operator needs the current escalation state and has no answer but `cat` |
