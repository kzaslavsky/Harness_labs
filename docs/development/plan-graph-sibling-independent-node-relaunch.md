# Sibling-independent node relaunch in a PlanGraph attempt

Status: design + a narrow implemented slice (Phase 1). Phases 2–4 need a human
decision before any code.
Date: 2026-08-16
Scope: `harness_labs/plangraph/` attempt lifecycle, plus the operator driver
that consumes its escalation artifact.

## The observed problem

A PlanGraph attempt dispatches several independent frontier nodes in parallel.
Node B hit a terminal block (review-fix gave up). Node A, which has no
dependency edge to B and runs in a separate worktree, is still converging and
will run for a long time. B is already known to need a relaunch, but nothing
happens until A finishes: B idles for A's entire remaining runtime.

## Root cause: it is library-level, in three independent layers

This is **not** a driver-only bottleneck. The campaign driver does serialize,
but even a perfectly per-node-aware driver is refused by the library today.

### Layer 0 — the driver does serialize (but is not the binding constraint)

The driver this was diagnosed against is the Retinology Flow-Editor
campaign's autoresume loop, which is campaign-specific and deliberately not
merged here (patch P7 of `HARNESS_LABS_PATCH_AUDIT_20260818.md`; a
parameterized `scripts/plan_graph_autoresume.py` has since been written and is
the merged replacement). Its
shape is what matters: it blocks on a quiescence wait (no campaign-runner
process alive) and then picks a predecessor from the latest *finalized*
attempt, which requires `manifest.json`'s `status` to be `failed`/`blocked`.
Removing both would not help, because of layers 1–3.

Worth noting: per-node terminal status *is* already observable in real time.
`PlanGraphAudit.node_failed` (`plan_graph_audit.py:597-609`) writes a
`plan_node_failed` event plus a sha256-addressed evidence artifact the moment B
blocks, and the driver already parses exactly those events in
`failed_frontier()`. So early *detection* is free today; early *action* is not.

### Layer 1 — `resume` hard-requires a finalized predecessor

`PlanGraph.resume` (`plan_graph.py:1239+`) resolves its predecessor through
`PlanGraphAudit.open_repair_predecessor`, which rejects anything not finalized
(`plan_graph_audit.py:236-241`):

```python
if (manifest.get("status") not in {"failed", "blocked"}
        or state.get("terminal_graph_status") not in {"failed", "blocked"}
        ...):
    raise AuditError("predecessor is not a matching failed or blocked attempt")
```

A still-running attempt has neither a `manifest.json` nor a
`terminal_graph_status`. There is no supported way to allocate a repair
successor against it.

### Layer 2 — `repair_selection` would double-own the running sibling

`PlanGraphAudit.repair_selection` (`plan_graph_audit.py:344-388`) partitions the
predecessor's nodes into exactly two buckets: **invalidated** (the retry
frontier and its dependency closure) and **reusable** (`status == "succeeded"`
*and* backed by a controller-owned integration barrier). A node that is still
`running` is in neither: it is not reused, so
`PlanGraph._audit_for_run` (`plan_graph.py:1965+`) marks it `queued` in the
successor, and the successor's scheduler will dispatch it.

That is the real correctness core. A mid-flight successor would launch node A a
second time — two live worktrees, two budget reservations, two candidate
commits for one node — while the predecessor's copy is still running. **The
attempt container is currently what guarantees single ownership of a node.**

### Layer 3 — `resume` abandons the predecessor's live reservations

`PlanGraph.resume` calls `budget.reconcile_attempt(predecessor_attempt_id,
disposition="abandoned", ...)` (`plan_graph.py:1328-1334`) with no
`live_node_ids`, on the documented assumption that "a finalized predecessor
cannot retain a live reservation." Against a running predecessor this
terminalizes A's live reservation out from under it.

(`RetryBudgetLedger.reconcile_attempt` already accepts `live_node_ids`
— `plan_graph_budget.py:384-425` — so this specific layer is the cheapest of the
three to fix.)

### Was the serialization deliberate?

Yes, and it is documented. ADR 0006 (`docs/decisions/0006-parallel-plangraph-contract.md`)
states:

> The controller continues every already-reserved sibling after a lane failure,
> then drains and records every terminal result; it blocks dependent nodes and
> never launches a new dependent after the failure.

`_seal_outcome`'s docstring (`plan_graph.py:1544-1545`) repeats it: "the caller
owns graph finalization so the ready-set path can drain in-flight siblings
before transitioning." The reason is custody, not convenience: an orphaned lane
whose terminal result is never recorded is exactly the ambiguity ADR 0006's
recovery rules refuse to guess about.

**But note what the ADR does *not* say.** It forbids launching a new
*dependent* after a failure. It does not forbid launching a new *independent*
node. The code before this change was stricter than the contract: the
admission block in `_run_ready_set` was gated on
`if terminal is None and deferred_error is None:`, which stops *all* dispatch,
not just dispatch of dependents (see `git log -p` for this file). The
stronger rule appears only as a verification bullet in
`docs/development/plangraph-parallelization-implementation-plan.md:439` ("A
failure stops new dispatch while already-running siblings drain"), and no test
asserts it.

## What the join baseline actually requires

Contrary to the initial hypothesis, the join commit is **not** the blocker for
B's own successor.

- `_base_commit_for_run` (`plan_graph.py:1837+`) derives a node's base from its
  *declared dependencies'* sealed candidates only, or `plan.base_commit` when it
  has none. B's successor base is unaffected by A being unsealed, exactly as it
  is today.
- `_join_candidates` (`plan_graph.py:1861+`) merges only the commits it is
  handed; it has no notion of "all siblings."

The join problem lives at the *other* end: `_final_candidate`
(`plan_graph.py:1852+`) joins every sink node's candidate. With B's lifecycle in
attempt N+1 and A's in attempt N, **neither attempt holds the complete sealed
set**, so neither can compute the final candidate or run the functionality
tests. ADR 0006's "at most one graph-owned integration lease" has no owner. This
is the structural obstacle that makes Phases 3–4 a real redesign rather than
plumbing.

## Retry budget and admission interactions

- **Budget is already per-node, not per-attempt.** `_assert_capacity`
  (`plan_graph_budget.py:502-514`) reads `node["launches"]`, per-class counters,
  and per-finding-key counters. `graph_attempt_id` is only a *tag* on a
  reservation. Finer-grained resumption does not break the budget's accounting.
- **`reconcile_attempt` is the one attempt-keyed operation** and already has the
  `live_node_ids` escape hatch.
- **`max_parallelism` is per `PlanGraph` instance.** Two concurrent instances of
  the same logical graph would each enforce their own bound, so the real
  concurrency becomes the sum — violating the documented invariant "Active child
  count never exceeds the configured limit." A cross-attempt design needs a
  lineage-scoped slot ledger, not a per-instance integer.
- **Approval receipt.** `_revalidate_approval` is per-instance and re-validated
  on resume; nothing about it is attempt-exclusive, so it is not an obstacle.
- **Attempt ordinals** (`_next_repair_ordinal`, `plan_graph.py:1380+`) are a
  directory scan under a lineage flock. Ordinals are a per-*graph* concept
  today; making them per-node (Phase 4) invalidates that scheme and the
  `<logical>-attempt-N` directory naming the driver and dashboard parse.

## Options considered

### Option A — driver-level fix only
**Rejected: it does not work.** Layers 1–3 refuse it. Worth stating plainly
because it looks plausible from outside.

### Option B — partial resume (a successor admitted mid-flight)
Requires: snapshot-reading a live, CAS-advancing predecessor checkpoint;
a third node category in `repair_selection` (`deferred_to_predecessor`) plus an
"externally owned, do not dispatch" exclusion in the scheduler; `live_node_ids`
threading; a lineage-scoped parallelism ledger; and — the hard part — a new
owner for final integration that spans attempts. This is precisely the deep
change to checkpoint/attempt semantics that should not be done speculatively.

### Option C — rolling attempt model (attempt numbers become per-node)
Supersedes ADR 0006 outright, and breaks the attempt-directory naming that the
runner, the auto-resume driver, and the dashboard's run catalog all parse.
Largest blast radius of the three.

### Option D (recommended) — relax the dispatch stop, not the attempt boundary

Keep the attempt as the unit of finalization and of node ownership. Change only
the over-strict rule that a terminal outcome stops *all* admission, so the tail
of a blocked attempt keeps doing useful work on nodes that are ready anyway.

This does not relaunch B early. It does recover most of the wasted wall-clock,
because the freed slot goes to a node the graph must run regardless, and any
node that seals in that window is reused by the successor's `repair_selection`
(it gets integration-barrier custody) rather than re-run. On a 26-node campaign
at `max_parallelism=5`, a block early in a long attempt currently idles up to
four slots for the sibling's whole remaining runtime.

It is strictly ADR-0006-compliant on the letter of the contract (dependents of a
blocked node stay unlaunched — enforced for free by the unsealed-dependency
rule), it composes with the `carry_forward_attempt` work (which hooks only
`_launch_base_commit` and keys off `predecessor_attempt_id` + `retry_frontier`,
both untouched here), and it is opt-in and default-off.

## Implemented (Phase 1)

Additive, strictly opt-in, default behavior byte-identical.

1. `ReadySetScheduler.select(..., withheld=())` — a node in `withheld` is
   removed from selection without consuming a slot. Validated against unknown
   and sealed ids. Default empty sequence leaves selection unchanged. Dependents
   need no extra closure: they are already excluded because a withheld node
   never enters `sealed`.
2. `PlanGraph(..., continue_independent_after_block=False)`, also reachable via
   `HARNESS_LABS_PLAN_GRAPH_CONTINUE_AFTER_BLOCK=1`. When on, `_run_ready_set`
   keeps admitting ready nodes after a node's terminal outcome, recording that
   node in `withheld` so it is never relaunched inside its own attempt. The
   first terminal decision is still what the attempt finalizes with.
   An **admission-stage** failure (join construction or retry-budget exhaustion)
   still stops admission entirely even when the flag is on — that is a
   graph-level problem, not one node's product failure.
3. `_transition_to_blocked`'s `resume_directive_template.retry_frontier` names
   *every* node the attempt terminalized, primary blocker first, rather than
   only `result.failed_run_id` — **but only when the flag is on** (see the
   decision below). A successor that retries only the first node blocks again
   immediately on the rest, and the flag makes multi-blocked attempts common.
   `schemas/block-escalation.json` types this field as an open object, so no
   schema change was needed.

Tests in `tests/test_plan_graph_parallel_run.py`:
`test_block_stops_all_admission_by_default`,
`test_independent_node_is_admitted_after_a_block_when_opted_in`,
`test_escalation_retry_frontier_names_every_terminal_node`,
`test_escalation_retry_frontier_is_unchanged_by_default`, and a
`ReadySetWithheldTests` unit class.

### Decision: the widened frontier is gated on the flag

Open question 2 below asked whether the widened `retry_frontier` was wanted
independently of `continue_independent_after_block`. As first written it
applied on *every* blocked attempt regardless of the flag, which meant an
operator who opted into nothing would still see the content of
`escalation.json` change under them. `retry_frontier` is a published contract:
any autoresume driver reads it to decide what the successor attempt retries.
Widening it is therefore a change to that contract, and it is now gated on the
same flag that creates the condition motivating it. With the flag off the
field keeps its long-standing single-element form, byte for byte.

The residual is deliberate and worth stating plainly: a drained attempt can
already finish with more than one terminal node without the flag (the survivor
of a drain can fail too), and in that case the frontier still under-reports, as
it always has. That is a pre-existing gap in the artifact contract, and fixing
it is a decision about the contract rather than a side effect of shipping this
feature. `test_escalation_retry_frontier_is_unchanged_by_default` pins the
current behaviour so the gap is recorded rather than merely tolerated.

`scripts/plan_graph_autoresume.py` closes the gap on the consumer side without
touching the contract: it reconciles the template against the attempt's own
`plan_node_failed` events, filtered by each node's final status in the
escalation, and retries the union. The template still supplies the order, so
the primary blocker stays first; a node the template omitted is appended and
the discrepancy is logged and counted, so an operator sees that the published
frontier and the audit trail disagreed rather than only seeing the repair.

### Correctness risks of Phase 1

- **Longer drain.** More nodes may be in flight when the attempt finalizes, so
  the block-to-finalization latency can *increase* even as total useful work
  goes up. Mitigated by being opt-in.
- **Budget spend on nodes that later get invalidated.** A node admitted after
  the block still consumes a `graph_launches` reservation. If it seals it is
  reused; if it fails it joins the retry frontier. Neither is worse than running
  it in the successor, but it front-loads spend.
- **Escalation payload growth.** More terminal nodes means a larger `nodes`
  array; `_externalize_block_node_details` already handles oversized payloads.

## Phased plan — what needs a human decision

**Phase 1 (done).** Above. Opt-in; recommended default-off until one campaign
has run with it.

**Phase 2 (small, needs approval).** Make `resume` safe against a predecessor
with live reservations *even though it still refuses one*: thread
`live_node_ids` into the `reconcile_attempt` call so layer 3 stops being a
latent hazard for any future Phase 3. Independently useful, ~10 lines, no
behavior change today.

**Phase 3 (STOP — needs a human decision before any code).** Mid-flight partial
resume. Requires all of: a snapshot read of a live predecessor checkpoint; the
`deferred_to_predecessor` node category in `repair_selection` and a matching
scheduler exclusion; a lineage-scoped parallelism ledger replacing the
per-instance `max_parallelism`; and an answer to **who owns final integration
when no single attempt holds the complete sealed set**. That last question is a
design decision, not an implementation detail, and it amends ADR 0006. Do not
start Phase 3 without deciding it.

**Phase 4 (STOP).** Per-node attempt lifecycles (Option C). Only worth
considering if Phase 3's integration-ownership answer turns out to require it
anyway. Breaks the `<logical>-attempt-N` naming contract that the runner, the
auto-resume driver, and the dashboard run catalog all depend on.

## Open questions for the human

1. Ship Phase 1 default-off (current state), or default-on after one campaign?
2. ~~Is the widened `retry_frontier` (item 3) wanted independently of the
   flag?~~ **Decided: no** — it is gated on the flag, see "Decision: the
   widened frontier is gated on the flag" above. The pre-existing under-report
   on a multi-terminal drain remains open as a separate contract question.
3. For Phase 3: who owns the final join and the functionality tests when node
   lifecycles span attempts — the newest attempt, a dedicated integration
   attempt, or does `attempt` stop being the integration unit entirely?
4. Should `max_parallelism` become a lineage-scoped ledger regardless, given
   that two attempts of one logical graph can already be launched concurrently
   by an operator today with no bound enforced between them?
