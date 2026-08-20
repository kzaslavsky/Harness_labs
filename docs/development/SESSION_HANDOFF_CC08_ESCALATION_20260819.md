# Session handoff — CC-08 in-graph escalation and bounded unsealing

Date: 2026-08-19
Repository: `harness_labs`
Base commit: `489b307` on branch `claude/cc08-escalation`
Deliverable: implement ADR 0007 per the CC-08 plan.

**Revised 2026-08-19, after the base moved.** This document was first written
against `1462892`, which by the time the work started was twelve commits behind
`main` — four of them on the three files CC-08 edits. The base is now
`489b307`, which merges `main` into `99930b4`. What changed for you is in
§1.0; §3's line numbers have been re-verified against the new
base.

This document is self-contained. It assumes no access to the session that
produced it. Read it top to bottom before touching a file.

---

## 0. What you are building, in one paragraph

A PlanGraph node's fixer may write only its registered `allowed_paths`. A
reviewer finding whose repair needs *another* node's paths currently has
nowhere to go, so the review-fix loop re-finds it until the cycle budget is
exhausted. CC-08 gives such a finding a destination: recognize it
deterministically (a set check on `required_paths`, or the fixer saying "I
can't"), package it from records that already exist, have an **independent**
LLM judge confirm or reject it, route it by **pure lookup** over the plan's
`allowed_paths`, and on confirmation either inject it into a node that
hasn't run yet or **unseal a sealed node with its ledger bounded to that one
finding** — running only the fix and verify stages, never a fresh review.
Each unseal spends one structural-decision allowance and is journaled.

---

## 1. Repo ground rules — non-negotiable

### 1.0 Your base, and what moved

The worktree is already created:
`/Users/kirillzaslavsky/Documents/harness_labs/.claude/worktrees/cc08-escalation`,
branch `claude/cc08-escalation`, at `489b307`. It is verified: 898 passed, 2
skipped, 1 xfailed; `tests/test_import_boundaries.py` passes; `python3
scripts/run_plan_graph.py run --help` exits 0; and
`git status --porcelain --untracked-files=all` prints nothing, so §1.4's
precondition holds. Do not re-create it off `1462892` — ADR 0007 and the CC-08
plan, both marked Binding in §2, do not exist at that commit.

Three things arrived with `main` that bear on this work:

- **`b6fd71e` (scope-screen false-positive repair) added `fixable_in_grant` at
  `review_fix.py:426`:** `bool(required_paths) and not
  paths_outside_scope(required_paths, allowed_paths)`. That is the exact
  complement of the predicate CC-08 escalates on, already computed, for a
  different purpose — and it clears `anchor_out_of_grant` when true. **Trap 5
  is now three-way, not two-way.** Read that block before writing CC-08-1, and
  put `escalate_out_of_grant` beside that computation rather than recomputing
  the same set check.
- **`597b160` rewrote `_transfer_targets_for`**, the function trap 2 forbids
  widening. Its downstream-only BFS still holds, but the tests that pin it have
  moved; re-grep rather than trusting the line numbers the campaign plan cites.
- **`98f92e0` / `3b8a743` reindented `run_feature_worktree`** inside a new
  `try`/`finally` that releases the controller liveness lease on every exit.
  This is where CC-08-2 wires `bounded_fix_only`; the construction is now one
  indent level deeper than §3.2 described.

### 1.1 Worktree-only workflow

**Never edit the primary checkout at `/Users/kirillzaslavsky/Documents/harness_labs`.**
All work happens in a dedicated git worktree; `main` receives merges only.
Yours already exists (§1.0). If you need another, branch it off
`489b307`, never off `1462892`:

```
git -C /Users/kirillzaslavsky/Documents/harness_labs worktree add \
    /Users/kirillzaslavsky/Documents/harness_labs/.claude/worktrees/<name> \
    -b claude/<name> 489b307
```

Work exclusively inside that directory. `git worktree list` will show many
sibling worktrees; leave every one of them alone.

### 1.2 Full-module-path imports

Import by full module path — `from harness_labs.featurerun.review_fix import
ReviewFixLoop`, not `from harness_labs import ReviewFixLoop`.
`harness_labs/__init__.py` is deliberately out of scope for this work; do not
add re-exports there. (It does eagerly re-export plangraph symbols, which is
why the boundary checker works on *static import closures* rather than
`sys.modules` — see the docstring at the top of
`tests/test_import_boundaries.py`.)

### 1.3 `core` must not import `plangraph`

Nothing under `harness_labs/core/` may import `harness_labs.plangraph.*`, and
`harness_labs/featurerun/feature_run.py`'s static import closure must remain
free of plangraph-layer modules. This is enforced by:

```
python3 -m pytest tests/test_import_boundaries.py -q
```

which contains `test_feature_run_closure_free_of_plangraph` and
`test_development_policy_closure_free_of_featurerun`. Practical consequence
for CC-08: `review_fix.py` (featurerun layer) may only *emit data*; all
routing, judging, and unsealing logic lives in
`harness_labs/plangraph/plan_graph.py`.

### 1.4 Pristine base, untracked included

Before registering or running a PlanGraph, the base worktree must be clean
**including untracked files**:

```
git status --porcelain --untracked-files=all   # must print nothing
```

This is the plan's stated precondition
(`docs/development/convergence-campaign-plan.md`, section `[driver-steps]`
step 5: "pristine base worktree (untracked included)"), and it matches
`harness_labs/core/git_transaction.py:changed_paths`, whose docstring is
"Return every tracked or untracked path changed relative to HEAD" — untracked
files count as workspace changes everywhere in this codebase. Scratch files
left in the tree will be seen as a node's output.

Note `.gitignore:18` ignores `logs/plan-approval/`, so operator notes and
approval files there do not dirty the base.

### 1.5 Live campaign

A live PlanGraph campaign (`cc-graph/convergence-campaign-harness`) runs
against this repository. Do not modify anything under `logs/runs/`,
`logs/plan-approval/`, or `.plan-graph-*` directories. Read them freely.

It runs out of the **`convergence-harness-impl-fe83e1` worktree**, on branch
`claude/convergence-harness-impl-fe83e1` at `99930b4`. Do not write anything
into that worktree, including this document — its base must stay pristine per
§1.4, and a scratch edit there is seen as a node's output. That branch
also cannot be rebased while the campaign holds it, which is why the merge was
taken onto a new branch instead.

---

## 2. Authoritative documents

| Document | Role |
|---|---|
| `docs/decisions/0007-in-graph-escalation-bounded-unsealing.md` | The decision. Binding. |
| `docs/development/in-graph-escalation-unsealing-plan.md` | The plan: design, AC-CC08-1…17, runtime contracts, build order, risks. Binding. |
| `docs/development/convergence-campaign-plan.md` | Context; section `[driver-steps]` step 4 states the "operator-relief path" deferral this work answers. |
| `docs/development/convergence-campaign-decomposition.json` | The acceptance-criterion style CC-08's criteria imitate. |
| `docs/decisions/0005-ledger-backed-review-fix.md` | Why finding identity is controller-owned and why the first review is the only discovery pass. |
| `docs/decisions/0006-parallel-plangraph-contract.md` | Custody rules; child stdout never establishes success. |
| `docs/decisions/README.md`, `TEMPLATE.md` | ADR conventions. **Follow-up not yet done:** ADR 0007 is not listed in `README.md`'s "Accepted decisions" list, and `docs/development/INDEX.md` does not link the plan. Both need a one-line addition. |

---

## 3. Ground truth: the code you are changing

Re-verified against the working tree at `489b307`. If any of this has drifted
further, **report the drift, do not invent around it.**

**Line numbers below were re-verified against `489b307`.** Every symbol
survived the merge; most moved. The largest shifts are in `review_fix.py`,
where `main` added 271 lines ahead of the material you need.

### 3.1 `harness_labs/featurerun/review_fix.py` (1305 lines)

| Symbol | Line | What it does today |
|---|---|---|
| `ReviewFixPolicy` | 88 | Frozen dataclass; every guard is an explicit boolean switch (`scope_expansion_guard_enabled`, `regression_review_enabled`, `cycle_limit_enabled`, …) plus numeric limits. Add `escalation_enabled: bool = False` here. |
| `ReviewFixResult` | 129 | `status, reason, cycles, risk_tier, ledger_ref, open_finding_keys, technical_debt_keys, transferred_findings, open_findings, stop_reason`; `as_dict()` at 98. Add `escalated_findings`. |
| `ReviewLedger` | 171 | Owns finding identity. `allowed_paths` is a constructor arg (178). |
| `ReviewLedger.seed_transferred` | 227 | Reopens inherited obligations, preserving `key`. Raises on empty or duplicate key. |
| `ReviewLedger.freeze_discovery` | 298 | Sets `discovery_frozen`; `ingest` then marks any new finding `deferred`. |
| `ReviewLedger.transfer_scope_expanding` | 301 | Routes eligible findings to a **pre-bound downstream** owner via `_target_for_path`. Skips when `not downstream_paths or None in owners or len(owners) != 1`. |
| `ReviewLedger.ingest` | 350 | Assigns outcomes. **Lines 415-500** are the disposition loop, rewritten by `b6fd71e` and `597b160`: `anchor_out_of_grant` from the single `file` (425), then the new `fixable_in_grant` (426), which clears it when every `required_paths` entry is inside the grant; `scope_screened` at 461 and 474; `note` demotion at 488 and 496. **Read 415-500 before writing CC-08-1** — this is the block trap 5 now has three terms in. |
| `ReviewLedger.mark_fix_attempt` | 510 | Raises `ReviewFixError("fixer claimed findings outside its fix list: …")` — copy this discipline for `unresolvable_finding_keys`. |
| `ReviewLedger._new_record` | 619 | The finding record. Fields include `key, file, subject, statement, category, severity, score, fix_cost, protects, requires_disposition, contract_violation, scope_expanding, outcome, outcome_reason, cycles_seen, occurrences, source_finding_ids, evidence_refs, fix_attempts, reopened_count, origin_node, transferred_to, transfer_eligible, required_paths, anchor_out_of_grant`. Add `escalation_reason: ""`. |
| `ReviewFixLoop.__init__` | 707 | Kwargs include `inherited_findings`, `retained_transfers`, `finding_transfer_targets`, `origin_node_id`, `inherited_ledger_frozen`, `resumed_ledger`, `resume_from_cycle`, `additional_cycles`. Add `bounded_fix_only`, `seeded_fix_keys`. |
| `ReviewFixLoop.run` | 795 | The cycle loop. **Line 830** is the unconditional `review` stage. **Lines 833-844** compute `scope_expanding` via `paths_outside_scope(finding.get("required_paths", ()), self.allowed_paths)`. **845** `ingest`, **846** `transfer_scope_expanding`, **851** `fix_keys` recomputation. Insert `escalate_out_of_grant` between 846 and 851. |
| `ReviewFixLoop._execute` | 1007 | Builds the `review-fix-context/1` context. **Line 1027** is `"fix_finding_keys": list(fix_keys or ())` — the fixer's exact bound. |
| `_target_for_path` | 1218 | Longest-prefix owner lookup over `{grant_path: node_id}`; returns `None` on no match or a tie between distinct owners. Reuse this rule for CC-08 routing. |
| `_detail_keys` | 1234 | Validates a `list[str]` detail; raises `ReviewFixError(f"{name} must be a list of finding keys")`. |
| `_stage_output_contract` | 1275 | For `fix`, `{"details_schema": "review-fix-fix/1", "required_details": {"addressed_finding_keys": "list[string]"}}`. Add `unresolvable_finding_keys` as **optional**. |

Protocol constants: `REVIEW_LEDGER_PROTOCOL = "review-ledger/1"` (20),
`REVIEW_FIX_RESULT_PROTOCOL = "review-fix-result/1"` (21). Stage detail
schemas are `review-fix-review/1`, `review-fix-fix/1`, `review-fix-verify/1`.

### 3.2 `harness_labs/featurerun/feature_run.py` (3196 lines)

- **1065**: the production `ReviewFixLoop(...)` construction. Wire
  `bounded_fix_only` / `seeded_fix_keys` here. It now sits one indent level
  deeper than it did: `98f92e0`/`3b8a743` wrapped everything from the journal's
  creation to the return in a `try`/`finally` that releases the controller
  liveness lease. Keep your edit inside that block.
- **1109-1140**: the recovery/continuation loop (`_recover_abnormal` at 961,
  980 and 1028, then a fresh loop at 1109 with `resumed_ledger` (1134),
  `resume_from_cycle` (1135), `additional_cycles` (1138)).
- **154, 156, 210-222**: `finding_transfer_targets` and
  `inherited_ledger_frozen` validation on the PlanGraph-context dataclass.
- **740-742**: run options `review_fix_policy`,
  `review_finding_transfer_targets`, `review_inherited_ledger_frozen`.
- Imports `DirtyBaselineGrantVerification, verify_dirty_baseline_grant` from
  `harness_labs.core.controller_live` (line 22);
  `_attach_dirty_baseline_grant` (1896) sets `executor.dirty_baseline_grant`
  only when the executor exposes that attribute.

### 3.3 `harness_labs/plangraph/plan_graph.py` (3132 lines)

| Symbol | Line | Note |
|---|---|---|
| `FeatureRunRequest` | 502 | Frozen dataclass: `protocol, run, base_commit, plan, plan_base_commit, plan_sha256, plan_graph_id, plan_node_id, feature_run_id, run_dir, finding_obligations, finding_transfer_targets, inherited_ledger_frozen, verification_gate_slot`. Add `bounded_fix_only: bool = False`. |
| `FeatureRunOutcome` | 526 | `status, candidate_commit, evidence, plan_graph_id, plan_node_id, feature_run_id, run_dir`. |
| `_SealDecision` | 537 | `kind ∈ {"sealed","blocked","failed"}`, `result, reason, evidence, evidence_ref, finding_obligations`. |
| `PlanGraphResult` | 549 | `status, candidate_commit, completed, failed_run_id, functionality_failure, deviation_records`. |
| `RepairResumeDirective` | 571 | `logical_graph_id, predecessor_attempt_id, retry_frontier, blocker_evidence_ref`. |
| `PlanGraph.resume` | 1248 | Derives `graph_run_id`, `logical_graph_id`, `predecessor_attempt_id`, `resume_directive`, `reused_completed`, `predecessor_checkpoint` itself — passing any of those as a kwarg is rejected (1266-1277). |
| `PlanGraph.run` | 1425 | Entry point. |
| `_request_for_run` | 2153 | Builds the request; sets `finding_transfer_targets=self._transfer_targets_for(run)` (2176) and `inherited_ledger_frozen=any(origin_node != run.id ...)`. Derive `bounded_fix_only` here from the obligation records. |
| `_load_finding_obligations` | 2190 | Reads `audit.state["finding_obligations"]` — this is how a successor attempt inherits the packet with no new directive field. |
| `_advance_finding_obligations` | 2250 | **2288** raises `PlanGraphError(f"finding {key} was transferred to a completed or current node")`. Relax **only** for escalation-routed records. |
| `_open_findings` | 2378 | Reads `outcome.evidence["review_fix"]["open_findings"]`; degrades to `()` on malformed input. Mirror it for `escalated_findings`. |
| `_transferred_findings` | 2396 | Reads `evidence["transferred_findings"]`, falling back to `evidence["review_fix"]["transferred_findings"]`; raises on malformed input. |
| `_transfer_targets_for` | 2415 | **The dependents-only BFS**, rewritten by `597b160` so a directory grant routes to the node that owns it. Its downstream-only semantics still hold and are still pinned by tests, but those tests have moved — re-grep rather than trusting the line numbers the campaign plan cites. `_owner_for_paths` must remain a separate, full-plan lookup; do not widen this one. |
| `_transition_to_blocked` | 2520 | Builds the `plan-graph-block-escalation/1` dict at **2633**; `retry_frontier` at 2622-2630; `record_block_escalation` at 2666; writes `escalation.json` (2655) and invokes `self.on_block_argv`. Add the `escalations` array here. |

`PlanGraphRegistration` carries `plan_lineage_id` (398) and
`automatic_recovery` (399); `RetryBudgetLedger` is constructed at 1237 with
`(self.run_root, registration.plan_lineage_id)`.

### 3.4 `harness_labs/plangraph/plan_graph_authority.py` (136 lines)

```
AUTHORITY_PROTOCOL = "plan-graph-automatic-recovery/1"
DECISION_PROTOCOL  = "plan-graph-recovery-decision/1"
ACTION_TYPES       = {"resume", "extend_budget", "transfer_ownership",
                      "ratify_gate_change", "revise_acceptance",
                      "revise_functionality", "accept_contract_deviation"}
STRUCTURAL_ACTIONS = {"transfer_ownership", "ratify_gate_change"} | REVISION_ACTIONS
```

`AutomaticRecoveryAuthority` (41) = `allowed_actions, max_extra_node_launches,
max_structural_decisions, protocol`. `from_mapping` requires the mapping's
key set to be **exactly** `{"protocol", "allowed_actions",
"max_extra_node_launches", "max_structural_decisions"}`.

`validate_recovery_decision` (71) requires the decision's key set to be
exactly `{"protocol", "action", "target", "expected_prior_digest",
"payload"}`, and for `transfer_ownership` requires
`set(payload) == {"receiving_node"}` with `receiving_node != target`
(92-100).

### 3.5 `harness_labs/plangraph/plan_graph_budget.py` (831 lines)

- `RetryBudgetLedger.protocol = "retry-budget-ledger/1"` (95); ledger path is
  `run_root/.plan-graph-budgets/<lineage_id>.jsonl` (101).
- `apply_recovery_decision` (180): validates against the registered
  authority, checks `expected_prior_digest`, refuses when
  `automatic_recovery_structural_decisions >= max_structural_decisions` with
  `BudgetError("structural recovery allowance exhausted")` (194-196), then
  for `transfer_ownership` appends `recovery_decision` followed by
  `{"event": "obligation_transferred", "source_node": target,
  "receiving_node": …, "reverification_required": True}` (212-221).
- `_fold` (599) is a closed `elif` chain ending **line 780** in
  `else: raise ValueError`. **Adding a new event kind makes the entire
  lineage unreplayable.** Do not add one. The `obligation_transferred`
  branch is at 722-729 and requires both nodes registered and
  `source != receiving`.
- `BudgetConfig.structural_decision_limit` (35) is a *per-node
  classification* limit — a different meter. CC-08 uses
  `AutomaticRecoveryAuthority.max_structural_decisions`.

### 3.6 `harness_labs/plangraph/plan_graph_audit.py`

- `repair_selection` (344): validates the blocker evidence ref, computes
  `invalidated_node_ids` as the frontier's transitive-dependent closure
  (357-367), then `reused_completed` only for nodes with a matching
  integration barrier or an inherited digest-verified reuse receipt
  (368-399). Returns `{retry_frontier, invalidated_node_ids,
  reused_completed, predecessor_checkpoint}`.
- `node_completed` (556) / `node_failed` (606) both accept
  `finding_obligations=` and persist it into the checkpoint via
  `_transition` (1538).
- `record_block_escalation` (1304) owns the artifact descriptor; returns the
  `artifact:sha256:…` ref.
- Journal event pattern to imitate:
  `self.journal.append("plan_graph_child_seal_adopted", status="succeeded",
  payload={...}, actor=_ACTOR)` (1158), optionally with `artifacts=(…,)`
  (1169).

### 3.7 `harness_labs/core/controller_results.py` (309 lines)

The `semantic-task-result/1` envelope. `validate_semantic_result` (108)
enforces per finding: non-empty `id` (unique), `statement`, `category`,
`severity ∈ {critical, major, minor, info}`, boolean
`requires_disposition`, `evidence_refs` as a string list, and
`source_finding_ids` as a string list. `enforce_deliverable_floor` (76)
raises `DeliverableFloorViolation(field, reason)` with `reason ∈
{not_a_string, sub_minimal_length, placeholder_token, repeated_token}` —
this is what killed graph attempt 1.

### 3.8 `harness_labs/core/git_transaction.py`

`paths_outside_scope(actual_paths, allowed_paths)` (77) — the set check
escalation keys off. `changed_paths` (33) counts untracked.
`workspace_snapshot` (91).

### 3.9 Schemas and CLI

- `schemas/block-escalation.json` — `plan-graph-block-escalation/1`. Has a
  `required` list, **no** `additionalProperties: false`, so an optional
  `escalations` property is backward compatible.
- New: `schemas/plan-graph-escalation-judgment.json` —
  `plan-graph-escalation-judgment/1`.
- `scripts/run_plan_graph.py` `run` subparser flags, verified verbatim:
  `--repository`, `--registration` | `--approval-receipt`,
  `--decomposition`, `--graph-attempt-id`, `--launcher` |
  `--launcher-command`, `--launcher-cwd`, `--launcher-timeout`,
  `--run-root`, `--lineage-id`, `--resume`, `--logical-graph-id`,
  `--predecessor-attempt-id`, `--retry-frontier` (**`action="append"` — repeat
  the flag, do not comma-join**), `--blocker-evidence-ref`, `--on-block-argv`.
  Confirm with `python3 scripts/run_plan_graph.py run --help`.

---

## 4. Protocols you will touch

| Protocol | Status |
|---|---|
| `semantic-task-result/1` | existing, unchanged |
| `review-ledger/1` | existing; gains the `escalated` outcome and `escalation_reason` field |
| `review-fix-result/1` | existing; gains `escalated_findings` |
| `review-fix-context/1` | existing, unchanged (`fix_finding_keys` already bounds the fixer) |
| `review-fix-fix/1` | existing; gains optional `unresolvable_finding_keys` |
| `plan-graph-block-escalation/1` | existing; gains optional `escalations` |
| `plan-graph-recovery-decision/1` | existing, unchanged — reuse `transfer_ownership` |
| `plan-graph-automatic-recovery/1` | existing, unchanged — `max_structural_decisions` is the meter |
| `retry-budget-ledger/1` | existing, **must not gain a new event kind** |
| `plan-graph-escalation-judgment/1` | **new** — the only new protocol |

---

## 5. Evidence pointers (the incident that justifies this)

- `logs/runs/cc-graph/convergence-campaign-harness-attempt-2-CC-05/artifacts/000182-final-result.json`
  — `review_fix.status == "blocked"`, `stop_reason == "cycle_limit"`,
  `cycles == 5`. Open finding
  `tests/test_convergence_lifecycle.py:measure-step-bypassed-capture-stdout-seam`:
  `severity "critical"`, `contract_violation true`,
  `requires_disposition true`, `scope_expanding true`,
  `anchor_out_of_grant false`, `required_paths ==
  ["tests/test_convergence_lifecycle.py", "scripts/ui_fidelity_capture.py",
  "scripts/run_convergence_campaign.py"]`, `fix_attempts` addressed in
  cycles 1, 2, 4 and reopened each time. `recovery_decisions[1]` shows
  `stop_cause: "repeated_strategy"`.
- `logs/runs/cc-graph/cc-graph-convergence-cc-2-CC-05/artifacts/000224-final-result.json`
  — same mode, 7 cycles, `review_continuation recovery limit of 2 exhausted`.
- `logs/runs/cc-graph/convergence-campaign-harness-attempt-1-CC-05/events.jsonl`
  — `DeliverableFloorViolation … 'field': 'summary', 'reason':
  'placeholder_token'` (three occurrences); the independent attempt-1 loss.
- Sealed candidates (files exist **only** in these commits, not at HEAD):
  - `11cda5c` "PlanGraph node CC-04" → `scripts/run_convergence_campaign.py`
    (1845 lines). `ConvergenceCampaignDriver` at line 1170, `measure` at
    1301; the defect is `audit_result = json.loads(sanitized_stdout)` around
    line 1341. Read it with
    `git show 11cda5c:scripts/run_convergence_campaign.py`.
  - `49ba00d` "PlanGraph node CC-03" → `scripts/ui_fidelity_capture.py`
    (1104 lines); around 1096-1100 it does
    `receipt["exit_code"] = EXIT_OK; receipt_path.write_text(...); return
    EXIT_OK` — no stdout. Read with
    `git show 49ba00d:scripts/ui_fidelity_capture.py`.
  - `3af8ba6` "PlanGraph node CC-01" → `harness_labs/core/convergence_contract.py`
    (25 lines) and `harness_labs/plangraph/convergence_ledger.py` (896).
  - `5593409` "PlanGraph node CC-02" →
    `harness_labs/plangraph/convergence_campaign.py` (556).
- The manual resolution CC-08 replaces: commit `fc16bd8` added
  `_operator_note(node_id)` at `experiments/run_convergence_plan_graph.py:143`,
  reading `logs/plan-approval/operator-notes/<node_id>.md` (files `CC-04.md`,
  `CC-05.md` exist; the directory is gitignored). It is folded into the fix
  and implement instruction strings at lines 287 and 366.
- The review-fix executor factory used by that campaign:
  `experiments/run_convergence_plan_graph.py:_review_fix_factory` (310) —
  `model=IMPLEMENTER_MODEL if stage == "fix" else REVIEWER_MODEL`. This is
  where a judge seat would be configured for a live run.

---

## 6. Do this, in order

1. **Set up.** Your worktree already exists at `489b307` (§1.0) and is
   verified green. Confirm `git status --porcelain --untracked-files=all` is
   empty before you start, and again before any graph run.
2. **Re-verify §3 before writing code.** Its line numbers were correct at
   `489b307`; grep each symbol anyway. If one has moved again, trust the tree
   and note the drift in your summary.
3. **CC08-A** — the featurerun layer: escalation primitives *and* the bounded
   fix-only loop, including the `FeatureRunRequest.bounded_fix_only` wiring.
   Criteria AC-CC08-1 through 5 and AC-CC08-16. Gate:
   `python3 -m pytest tests/test_review_fix.py tests/test_feature_run.py tests/test_import_boundaries.py -q`.
   Build it internally in the plan's CC-08-1 → CC-08-2 order; that ordering is
   still load-bearing, it is simply no longer a node boundary.
4. **CC08-B** — the plangraph layer: routing, judgment, packet, schemas and
   journal events, then unseal, authority spend and the cascade assertion.
   AC-CC08-6 through 15, and AC-CC08-17. Depends on CC08-A. Gate:
   `python3 -m pytest tests/test_plan_graph.py tests/test_plan_graph_budget.py -q`.
   Internal order: the plan's CC-08-3 → CC-08-4.

5. **Finalize.** `python3 -m pytest tests/ -q`. Then add ADR 0007 to
   `docs/decisions/README.md`'s accepted list and the plan to
   `docs/development/INDEX.md` (see §2).

The decomposition is
[`cc08-escalation-decomposition.json`](cc08-escalation-decomposition.json).
**It registers two nodes, not the four in the plan's `[cc08-build-order]`.**
That section asserts "Path grants are disjoint, so S1/S2 hold", and they are
not: CC-08-1 and CC-08-2 both write `review_fix.py` and
`tests/test_review_fix.py`, and CC-08-3 and CC-08-4 both write `plan_graph.py`
and `tests/test_plan_graph.py`. Only the concurrent pair is disjoint. A
reviewer on CC-08-2 finding a defect in the `review_fix.py` code CC-08-1
sealed is exactly the escalation case CC-08 is being built to handle — and
CC-08 does not exist yet to handle it. Splitting on the layer boundary instead
makes the grants disjoint at file granularity (S1, S2), leaves fan-in at 1
(S9), and follows the seam `tests/test_import_boundaries.py` already enforces.
The four-node version bought one step of parallelism between two nodes that
each overlapped a sibling anyway.

Existing test files (verified present): `tests/test_review_fix.py`,
`tests/test_feature_run.py`, `tests/test_import_boundaries.py`,
`tests/test_plan_graph.py`, `tests/test_plan_graph_budget.py`,
`tests/test_plan_graph_authority.py`. There is **no**
`tests/test_plan_graph_recovery_authority.py`.

---

## 7. Traps, stated explicitly

1. **Do not add a `retry-budget-ledger/1` event kind.** `_fold` ends at
   `plan_graph_budget.py:780` with `else: raise ValueError`; a new kind makes
   every past and future read of that lineage raise. Reuse
   `transfer_ownership` → `recovery_decision` + `obligation_transferred`, and
   put escalation-specific events on the PlanGraph audit journal.
2. **Do not widen `_transfer_targets_for`.** Its dependents-only BFS is
   pinned by existing tests (`tests/test_plan_graph.py:147,179,237` are cited
   by the campaign plan as the in-graph routing pins). Add a separate
   `_owner_for_paths`.
3. **`transfer_ownership` vs `transfer_scope_expanding`** — a recovery action
   metered by `max_structural_decisions` versus a `ReviewLedger` method that
   spends nothing. Both appear in this work; they are not the same thing.
4. **`max_structural_decisions` vs `BudgetConfig.structural_decision_limit`**
   — the registration authority meter versus a per-node classification limit.
   CC-08 spends the former.
5. **`scope_expanding` vs `anchor_out_of_grant`** — the first derives from
   `required_paths` (what CC-08 keys off), the second from the single `file`
   anchor. In the motivating record they disagreed: `true` and `false`.
6. **Escalate after transfer, not before.** `transfer_scope_expanding` must
   keep first claim on a finding with a legitimate downstream owner
   (AC-CC08-3).
7. **`--retry-frontier` is `action="append"`.** Two nodes means the flag
   twice, not one comma-separated value.
8. **The judge never routes, and never judges its own reviewer's finding.**
   Both are hard refusals, not prompt guidance (AC-CC08-6, AC-CC08-7).
9. **Bounded means no `ingest` call.** Do not implement the bound by telling
   the reviewer to behave; implement it by not running a review stage at all.
10. **Cascade is real and must not be hidden.** Reopening a sealed node
    invalidates every transitive dependent's reuse
    (`repair_selection:357-367`). Say so in the escalation artifact.
11. **`DeliverableFloorViolation`.** Every `summary` and deliverable field
    you or a sub-agent emits must clear the floor: ≥ 4 characters, not a
    placeholder token (`test`, `todo`, `tbd`, `n/a`, `na`, `placeholder`,
    `wip`, `fixme`, `xxx`, `lorem ipsum`), not one token repeated. A summary
    of `"test"` fails. This cost a whole graph attempt.
12. **Feature off by default.** `escalation_enabled=False`,
    `escalation_judge=None`. AC-CC08-1 requires byte-identical behaviour in
    that configuration.

---

## 8. Definition of done

- All 17 criteria in `docs/development/in-graph-escalation-unsealing-plan.md`
  `[cc08-criteria]` are covered by named tests.
- `python3 -m pytest tests/ -q` passes with no new skips.
- `python3 -m pytest tests/test_import_boundaries.py -q` passes.
- `python3 scripts/run_plan_graph.py run --help` still exits 0 and lists
  `--retry-frontier` and `--on-block-argv`.
- The retry-budget lineage used by the new tests folds without error, proving
  no new event kind was added.
- No file outside the two nodes' declared `allowed_paths`, plus the two
  index/README one-liners in §6 step 5, has changed.
