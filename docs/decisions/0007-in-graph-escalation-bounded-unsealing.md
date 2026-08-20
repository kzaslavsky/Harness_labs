# 0007 — In-graph escalation of out-of-scope review findings with bounded unsealing

Status: accepted
Date: 2026-08-19
Owners: PlanGraph controller
Run: `cc-graph/convergence-campaign-harness` (attempts 1–3), logical graph
`convergence-campaign-harness`

## Context

A PlanGraph node's fixer may only write its registered `allowed_paths`. That
fence is load-bearing: it is what makes sibling lanes independent and what
makes a candidate's provenance readable. It also means a node whose reviewer
finds a real defect **in another node's code** has no legal move. Today the
loop simply re-finds the defect until its cycle budget runs out.

This is not hypothetical. On 2026-08-19, node `CC-05` (lifecycle proof;
`allowed_paths` = `tests/test_convergence_lifecycle.py`,
`tests/fixtures/convergence_lifecycle_launcher.py`) reported the finding
keyed
`tests/test_convergence_lifecycle.py:measure-step-bypassed-capture-stdout-seam`
— `severity: critical`, `contract_violation: true`, `requires_disposition:
true`, `required_paths` = `[tests/test_convergence_lifecycle.py,
scripts/ui_fidelity_capture.py, scripts/run_convergence_campaign.py]`. The
finding is correct: in sealed candidate `11cda5c`,
`ConvergenceCampaignDriver.measure` in `scripts/run_convergence_campaign.py`
computes `audit_result = json.loads(sanitized_stdout)`, while the shipped
capture CLI `scripts/ui_fidelity_capture.py` in sealed candidate `49ba00d`
writes its receipt to `<--out>/receipt.json` and prints nothing to stdout on
success. `measure` cannot work against the real capture CLI.

The existing machinery detected the scope problem and then had nowhere to put
it:

- `ReviewFixLoop.run` in `harness_labs/featurerun/review_fix.py` already
  computes `scope_expanding` from
  `paths_outside_scope(finding["required_paths"], self.allowed_paths)`, and
  the record shows `scope_expanding: true`.
- `ReviewLedger.ingest` does not screen it, because the scope-expansion guard
  deliberately exempts `contract_violation` and `requires_disposition`
  findings. Correct — a required finding must not be silently discharged.
- `ReviewLedger.transfer_scope_expanding` could not route it, because
  `PlanGraph._transfer_targets_for` builds its `{path: node_id}` map by
  breadth-first walk over **dependents only**. `CC-04` is a predecessor of
  `CC-05`, so it is absent from the map, no owner resolves, and the finding
  stays open.
- Even if it had resolved, `PlanGraph._advance_finding_obligations` raises
  `PlanGraphError` for a finding "transferred to a completed or current
  node". Routing to a sealed owner is presently *prohibited*.

The result was two burned graph attempts. Attempt 2 stopped with
`review_fix.stop_reason == "cycle_limit"` after 5 cycles and 3 addressed-then-
reopened fix attempts on that key; the `review_continuation` recovery class
then exhausted its own limit with `stop_cause: "repeated_strategy"`. Attempt 1
had already failed separately on a `DeliverableFloorViolation` with
`reason: "placeholder_token"`. Resolution was manual: the operator verified
the finding against the sealed candidates, resumed with retry frontier
`[CC-04, CC-05]`, and delivered guidance through an ad-hoc, untyped
operator-notes channel added in `fc16bd8`
(`experiments/run_convergence_plan_graph.py:_operator_note`, reading
`logs/plan-approval/operator-notes/<node>.md`, a gitignored path).

`docs/development/convergence-campaign-plan.md` anticipated exactly this at
[`driver-steps`](../development/convergence-campaign-plan.md) step 4: "a
mid-run surprise on an unowned path blocks fail-closed and routes to the
per-node operator-relief path or the next round's seed — authority follows an
observed finding." That deferral's trigger has now fired, and the observed
relief path was a human reading two sealed diffs by hand.

## Decision

PlanGraph gains one new authority: **an out-of-scope finding may be escalated
in-graph, adjudicated by an independent LLM judge, routed by deterministic
lookup, and — on confirmation — may reopen a sealed node with a ledger scope
bounded to that finding alone.** Five parts.

### (a) Recognition is early, deterministic, and pre-fix

Two entry points, both evaluated **before** another fix cycle is spent on the
key:

1. **At finding ingest.** A set check on data the contract already requires:
   `finding["required_paths"] ⊄ node.allowed_paths`. `ReviewLedger.ingest`
   already computes this predicate through `paths_outside_scope`; the
   decision is that a finding satisfying it, which
   `transfer_scope_expanding` does not claim for a downstream owner, takes
   outcome `escalated` in the cycle it is first seen, and never enters
   `fix_keys`.
2. **At fixer declaration.** The `review-fix-fix/1` stage result may return
   `unresolvable_finding_keys`, a subset of the `fix_finding_keys` it was
   handed, meaning "I cannot resolve this within my grants." Those keys take
   the same outcome. A key outside the fix list is a protocol error, under
   the same discipline `ReviewLedger.mark_fix_attempt` already applies to
   `addressed_finding_keys`.

Detection quality depends on reviewers and fixers emitting honest
`required_paths`. That is already a contract obligation, not a new one:
`_stage_output_contract("review")` lists `required_paths` among the required
finding fields, and the semantic envelope in
`harness_labs/core/controller_results.py` already carries `id`, `statement`,
`category`, `severity`, `requires_disposition`, `evidence_refs`, and
`source_finding_ids`. Escalation adds no new honesty requirement; it adds a
destination for honesty that previously had none.

### (b) The evidence packet reuses existing conventions

No new packet schema. The escalation packet is:

- the existing **review-ledger finding record** — the exact dict already
  produced by `ReviewLedger._new_record`, carried verbatim, identity
  (`key`, `source_finding_ids`, `evidence_refs`, `cycles_seen`,
  `fix_attempts`) intact; plus
- the existing **`plan-graph-block-escalation/1`** artifact
  (`schemas/block-escalation.json`), extended with one optional
  `escalations` array binding each escalated key to its origin node, its
  resolved owner, and its judgment.

All references are content-addressed (`artifact:sha256:…`), recorded through
`PlanGraphAudit.record_block_escalation`, which already owns the artifact
descriptor so a successor can prove its predecessor recorded the reference.
The single genuinely new versioned record is the judgment itself,
`plan-graph-escalation-judgment/1`.

### (c) Judgment is LLM; routing is not

An LLM judge — a coordinator seat or a fresh session — returns `CONFIRM` or
`REJECT` on the escalated finding, with a rationale and evidence references.
The judge **must be independent of the reviewer whose finding is being
judged**, and is never the escalating node's own reviewer. The controller
refuses an escalation whose configured judge identity matches the packet's
recorded reviewer identity.

`REJECT` is not a discard. The key returns to the escalating node through the
existing `fix_finding_keys` channel, with the judge's rationale written into
`outcome_reason`, so the next cycle argues against a stated position instead
of rediscovering the same claim.

**Routing is not the judge's job.** The owner of a required path is a pure
lookup over the registration's `allowed_paths` — the same longest-prefix rule
`review_fix._target_for_path` already implements — widened from the
dependents-only map to the whole plan. Zero owners, or two or more distinct
owners, is not an LLM question either: it escalates to the human operator
through the ordinary block path.

**Amendment, 2026-08-20 — a judge that cannot answer is a third refusal.**
This decision as first written offered the controller only `CONFIRM` and
`REJECT`, which is complete only while the judge always answers. A real seat
does not: a backend can fail, a reply can be unparseable, a schema check can
fail every retry. Neither verdict is safe to invent there. `CONFIRM` spends a
structural decision and unseals a node on no evidence; `REJECT` is permanent,
because a repeat escalation of the same finding key is forced to an operator
block rather than re-judged, so a fabricated `REJECT` poisons that key for the
whole lineage. Only a block is undoable.

So the judge seat may also refuse. The controller treats refusal exactly as it
treats zero-or-ambiguous owners: this attempt blocks for a human, with no
verdict journaled, no authority spent, and no key poisoned. Refusal is a
distinct, typed channel — not an exception escaping into the graph, and not a
verdict — so a third-party judge's own errors keep propagating unchanged.

The human is the backup for everything the layers below cannot resolve, and
this completes that ladder: the node fixes what it owns, the graph seat judges
what crosses node boundaries, and the operator sees only what neither could
settle — an unroutable owner, an already-rejected repeat, an exhausted
allowance, or a judge that could not answer.

### (d) On CONFIRM: inject, or unseal with bounded scope

- **Owner has not yet run.** The finding record is appended to that node's
  `finding_obligations` — the channel that already exists and already sets
  `inherited_ledger_frozen=True` in `PlanGraph._request_for_run` whenever
  `origin_node != run.id`. No new authority is spent.
- **Owner is sealed.** It is unsealed. The transferred finding key(s) are
  seeded into a review ledger that runs **only the fix and verify stages** of
  the existing review-fix machinery — the stages that are already bounded, by
  construction, to the exact `fix_finding_keys` handed to them. The
  open-ended review stage is skipped entirely, so no `ReviewLedger.ingest`
  call occurs on the reopened node and **no new finding can be opened and no
  other ledger item added**. The bound is structural, not prompted.

This is the record's core new authority: in-graph unsealing of a sealed node
for an out-of-scope blocker, bounded to the escalated issue only. Mechanically
the reopened node runs in a successor attempt whose retry frontier is
`[owner, escalating_node]` — the same shape the operator produced by hand —
but the frontier is now controller-authored from a confirmed judgment and
carried as durable state, not typed into a shell.

### (e) Bounds, authority, and audit

Reopening a sealed node is a structural decision. Each unseal spends exactly
one `transfer_ownership` action against the registration's
`automatic_recovery` authority, metered by `max_structural_decisions` in
`AutomaticRecoveryAuthority`. This reuses the existing accounting end to end:
`transfer_ownership` is already in `ACTION_TYPES` and `STRUCTURAL_ACTIONS`;
`RetryBudgetLedger.apply_recovery_decision` already validates it, already
requires a `receiving_node` distinct from `target`, and already appends
`obligation_transferred` with `reverification_required: true`. Exhaustion
raises `BudgetError("structural recovery allowance exhausted")` and the graph
blocks to the operator. A registration that does not list
`transfer_ownership` in `allowed_actions` cannot unseal at all.

No new event kind is added to the retry-budget ledger, whose `_fold` ends in
`else: raise ValueError` and would reject one. Escalation, judgment, and
unseal are journaled on the PlanGraph audit journal as
`plan_graph_finding_escalated`, `plan_graph_escalation_judged`, and
`plan_graph_node_unsealed`, in the manner of the budget ledger's operator
events: append-only, content-addressed, replayable.

**Stated consequence: unsealing cascades.** The reopened node produces a new
candidate commit, which invalidates the reuse receipts of every transitive
dependent — `PlanGraphAudit.repair_selection` computes exactly that closure
in `invalidated_node_ids`, and those nodes are excluded from
`reused_completed`. The unseal bounds the reopened node's **ledger** scope. It
does not bound downstream re-execution cost, and this record does not claim
otherwise.

## Alternatives

- **Let the escalating node's fixer widen its own grant.** Rejected: it
  dissolves the path fence that makes lanes independent and candidate
  provenance readable, and it lets any reviewer authorize arbitrary writes by
  asserting a required path.
- **Defer every out-of-scope finding to the next round's seed.** Rejected as
  the sole path: the observed case was a `critical`, `contract_violation`
  finding that made the escalating node's own gate unprovable. Deferring it
  ships a green node whose gate routes around the seam it exists to prove.
- **Let the LLM judge also decide the owner.** Rejected: ownership is a total
  function of the approved registration's `allowed_paths`. Asking a model to
  recompute it introduces disagreement with the admission-time grant map that
  nothing downstream can adjudicate.
- **Unseal and re-run the owner's full review-fix loop.** Rejected: the
  reopened node's first review pass is a fresh discovery pass, so an unseal
  granted for one blocker would license unbounded new work on already-sealed
  code and re-invalidate downstream repeatedly.
- **Keep the operator-notes channel.** Rejected as the durable answer: it is
  untyped free text under a gitignored path, invisible to the audit journal,
  unbound by any authority meter, and it requires a human to read two sealed
  diffs before a single word of it can be written. It remains available as an
  operator convenience; it is no longer the only relief path.
- **A new escalation-packet schema.** Rejected per the plan's contract-burden
  direction: the review-ledger finding record plus
  `plan-graph-block-escalation/1` already carry every field the packet needs.

## Evidence

- `logs/runs/cc-graph/convergence-campaign-harness-attempt-2-CC-05/artifacts/000182-final-result.json`
  — the decisive record: `review_fix.stop_reason == "cycle_limit"`,
  `review_fix.status == "blocked"`, and the two open findings including
  `measure-step-bypassed-capture-stdout-seam` with `scope_expanding: true`,
  `severity: "critical"`, and its three `required_paths`.
- `logs/runs/cc-graph/cc-graph-convergence-cc-2-CC-05/artifacts/000224-final-result.json`
  — the same failure mode recurring across 7 cycles and two
  `review_continuation` recoveries, ending `recovery proposal repeated an
  unchanged strategy`.
- `logs/runs/cc-graph/convergence-campaign-harness-attempt-1-CC-05/events.jsonl`
  — the independent attempt-1 loss:
  `DeliverableFloorViolation … 'reason': 'placeholder_token'`.
- Sealed candidates `11cda5c` (`scripts/run_convergence_campaign.py`,
  `audit_result = json.loads(sanitized_stdout)`) and `49ba00d`
  (`scripts/ui_fidelity_capture.py`, `receipt_path.write_text(...)` then
  `return EXIT_OK`) — the defect is real and lives outside CC-05's grant.
- `harness_labs/featurerun/review_fix.py` — `ReviewLedger.ingest`,
  `transfer_scope_expanding`, `mark_fix_attempt`, `_target_for_path`: every
  primitive escalation reuses.
- `harness_labs/plangraph/plan_graph.py` —
  `_transfer_targets_for` (dependents-only BFS),
  `_advance_finding_obligations` (rejects completed targets),
  `_request_for_run` (`inherited_ledger_frozen`).
- `harness_labs/plangraph/plan_graph_authority.py` and
  `plan_graph_budget.py` — `transfer_ownership` in `STRUCTURAL_ACTIONS`,
  `max_structural_decisions`, and the `obligation_transferred` fold.
- `harness_labs/plangraph/plan_graph_audit.py:repair_selection` — the
  invalidation closure that makes the cascade consequence concrete.
- `docs/development/convergence-campaign-plan.md` `[driver-steps]` — the
  operator-relief deferral whose trigger this record answers.
- `experiments/run_convergence_plan_graph.py:_operator_note` (commit
  `fc16bd8`) and `logs/plan-approval/operator-notes/CC-04.md` — the manual
  resolution this replaces.

## Consequences

**Required.** Reviewers and fixers must emit truthful `required_paths`; a
finding with an empty `required_paths` cannot escalate and is handled exactly
as today. A registration that wants in-graph unsealing must list
`transfer_ownership` in `automatic_recovery.allowed_actions` and budget
`max_structural_decisions` for it. Every escalation, judgment, and unseal must
appear in the audit journal.

**Prohibited.** An unsealed node may not open new findings or add ledger
items beyond its seeded keys. A judge may not be the escalating node's own
reviewer. A judge may not choose the owner. An escalation with zero or
multiple owners may not be auto-resolved.

**Easier.** The observed failure mode — a correct out-of-scope finding
burning two full graph attempts — terminates on the first cycle that sees it.
The operator-notes channel stops being load-bearing.

**Harder.** Graph attempts become more expensive in the confirmed case: the
cascade re-runs every transitive dependent of the reopened node. Registrations
must now reason about a structural-decision budget they previously left at
zero.

## Validation and reversal

Keep this authority while confirmed escalations are cheaper than the attempts
they replace and while every unseal is reconstructible from the journal. Two
signals would force revision: a `CONFIRM` rate near 100% (the judge is
rubber-stamping and the loop has become an unbounded scope-widening path), or
repeated cascades where the reopened node's bounded fix does not clear the
escalating node's finding (routing or the bound is wrong).

Reversal is a policy switch, not a migration: escalation is off by default and
a registration without `transfer_ownership` authority behaves exactly as
before this record. Superseding it requires a new versioned judgment protocol
and must preserve the existing finding-record identity and
`plan-graph-block-escalation/1` shape.
