# Node-sizing criteria from PlanGraph campaign history

Date: 2026-08-18. Opus agent review of prior campaign run journals,
commissioned to ground the node-sizing criteria in
`mockup-delta-convergence-plan.md`. Full evidence citations below; the
plan's "Node sizing criteria for generated graphs" section is the
normative summary.

## Evidence corpus

- **flow-editor-uistreamline** (26 nodes, 87 attempts): runner +
  `NODE_NOTES` at
  `.claude/worktrees/handoff-review-f71fb8/experiments/run_flow_editor_streamline_plan_graph.py`
  (~1,768 lines; `_SHARED_CSS_DISCIPLINE` at :380); registration at
  `handoff-review-f71fb8/logs/registration/flow-editor-uistreamline.json`;
  run root `logs/runs/few-graph/` with 76 `escalation.json` files, budget
  ledger (288 gate invocations, 164 repair dispatches, 27 operator
  resets, 15 extensions), 2 join-conflict payloads, 3 operator join
  resolutions.
- **dashboard-observability-metrics** (7 nodes): registration + run root
  in `dashboard-observability-metrics-ace406`; 2 escalations total;
  completed.
- **Static overlap checker**: `_sibling_overlap_warnings()` exists at
  `harness_labs/plangraph/plan_approval.py:458-525` (landed `7b0e475`,
  mid-campaign) — advisory only; `scripts/approve_plan.py` discards the
  warnings. This corrects the memory note claiming no such check exists.
- **Not found**: any orbit or burden3 run history (runners only); wall
  clock per node (attempt counts used as the cost proxy); this worktree's
  `logs/` is empty — journals live in the sibling worktrees.

## Headline correlation

| campaign | nodes | HIGH sibling-overlap warnings (checker re-run on its own plan) | join-conflict escalations | total escalations | outcome |
|---|---|---|---|---|---|
| flow-editor-uistreamline | 26 | **17** (+102 info) | **36** | 76/87 attempts | 25/26 sealed; WP-25 blocked at attempt 87 |
| dashboard-observability-metrics | 7 | **0** | **0** | 2 | completed |

The flow-editor plan was statically detectable as defective at approval
time; the harness detected it; nothing blocked on it. All three conflict
clusters (WP-06: 7 attempts; WP-13: 8; WP-25: 21) trace to declared
sealed-sibling overlaps. The WP-10↔WP-20 conflict was rated only `info`
because they shared *directory* grants — a shared directory grant is an
unwarned version of the same defect. The escalation `reason` string
quotes only the last `Auto-merging` line, which misled diagnosis for
seven attempts on WP-06.

## Key negative results

- **Acceptance-criteria count does not predict cost.** One-criterion
  nodes span the whole range (WP-22: 1 launch, sealed first try; WP-21:
  18 launches, 16 blocked attempts). The 8–10-criteria nodes (WP-13,
  WP-14, WP-17) all sealed. No AC-count cap is proposed.
- **No functional/visual split.** WP-13 mixed both and sealed; WP-21 was
  purely visual and was the worst node. The real axis is
  own-criteria-vs-inherited-invariants.
- **No separate "one surface per node" rule** — every case it would flag
  is already flagged by path disjointness or ownership contradiction.

## Key positive findings

- **WP-21's criterion delegated pass/fail to a protocol document**
  ("passes the layout- and state-faithful fidelity protocol") — 16
  blocked attempts and ~460 lines of `NODE_NOTES` were spent discovering
  what it meant. WP-13's criteria each named an observable and sealed.
  (Caveat: WP-22/23 had prose-y criteria and sealed in one launch each —
  this is a variance reducer, not a mean-cost predictor.)
- **Ownership contradictions are the expensive shape**: WP-21/22/23/24's
  objectives assigned fixes to other nodes' files while holding write
  grants on those same files — 25 launches / 19 blocked attempts across
  the group, and the one named conflicting hunk in the WP-25 join is
  WP-21 writing a WP-14-owned CSS rule.
- **Prose discipline measurably failed**: `_SHARED_CSS_DISCIPLINE` was
  deployed proactively citing the overlap analysis, and WP-21↔WP-22
  still conflicted on `flow_editor.css`/`canvas.js`.
- **Shared-gate invariants outside a node's criteria burned whole review
  budgets**: WP-12 (attempts 21/22), WP-24 (39), WP-16 (34/35) — each
  blocked "not on your own EV criteria" but on repo-wide invariants their
  verification gate pinned; ≥6 attempts, two full 5-cycle budget
  exhaustions.
- **Sandbox-invisible criteria are unfulfillable**: WP-21 ATTEMPT-49
  spent 7 repair rounds "succeeding" against a test that xfailed in the
  worker sandbox while failing in the controller's real browser run;
  ATTEMPT-54 documents an exit condition requiring SHA-256s of PNGs the
  worker could not produce.
- **Fan-in concentrates blocks**: WP-25 (fan-in 5) was the blocked node
  in 24/87 attempts on 4 launches; WP-13 (7) blocked 12; WP-06 (4)
  blocked 8 — 44 of 76 escalations at three high-fan-in nodes.

## Criteria S1–S10 and enforcement

See the plan's "Node sizing criteria for generated graphs" section for
the normative statement; the evidence-quality grading:

| criterion | basis |
|---|---|
| S1 disjoint writable paths (file *and* directory granularity) | Measured — strongest result (17/36 vs 0/0) |
| S2 explicit file grants, no directories | Measured (directory grants defeated S1 in flow-editor; dashboard all-file, zero conflicts) |
| S3 serialize shared writers via `depends_on`, never prose | Measured (discipline string deployed and failed) |
| S4 no grant on a path the objective disclaims | Measured, n=4, high variance |
| S5 criteria name their own observable, no protocol-document deferral | Qualitative variance argument |
| S6 criteria observable from the node's own environment | Measured, n=1, documented root cause |
| S7 exit checks satisfiable within own `allowed_paths` | Measured, n=1 (WP-05 attempt-15, green tests, failed launch) |
| S8 verification gate ⊆ criteria (or invariants moved downstream) | Measured (≥6 attempts) but not cleanly decidable → warn |
| S9 fan-in ≤ 3 except the sink | Directionally measured; threshold 3 is judgment |
| S10 ~8 repair nodes per generation | Restates the plan's rule; supported, confounded |

Enforcement grading: S1/S2/S4/S6/S7 block `issue`; S3/S9 auto-fix
(insert edge / propose intermediate join) and report; S5 blocks quoting
the criterion; S8/S10 warn loudly in the approval packet, requiring
acknowledgment. No auto-splitting of nodes — the machine proposes a
split, the operator disposes. Overrides are per-criterion, per-node, with
a recorded reason (modeled on `RetryBudgetLedger.extend` reason strings);
no blanket bypass flag. An LLM approver may approve only a **clean**
check — never an overridden one.

## Caveats

The two campaigns differ in more than sizing (repo, worker mixture, gate
cost, live-browser dependency, mid-campaign harness fixes, environmental
failures); the contrast is a strong signal, not a controlled experiment.
Environmental escalations (~40 generic blocked reports) were attributed
to nothing; counts lean on the 36 explicitly-typed join conflicts and 4
typed budget exhaustions.
