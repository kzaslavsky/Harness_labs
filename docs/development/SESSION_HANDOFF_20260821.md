# Session handoff — 2026-08-21

Status: active. `main` at `daa2232`, pushed and in sync with `origin/main`.
Nothing is running.

Supersedes [`SESSION_HANDOFF_20260819.md`](SESSION_HANDOFF_20260819.md). Every
item on that document's ranked list is either closed below or restated here
with its current evidence.

## What this document is

A handoff after a session that closed the three top-ranked items from the
20260819 backlog, and a re-ranked list of what is left. Written because the
tree moved under this work more than once — another session extracted the
campaign launcher, finished the convergence campaign, and merged both to
`main` while this session was building CC-08 — and the next stretch should
start from measured state rather than from either document's memory.

Everything below was verified against `daa2232` in-session unless explicitly
marked otherwise.

---

## Where `main` is

`daa2232`. Full suite: **1580 passed, 2 skipped, 1 xfailed.** The single xfail
is deliberate and must stay — see "the SEAM" in the 20260819 handoff, which
remains accurate.

Operational notes that still hold:

- **`origin/main` has other contributors, and they were active during this
  session.** The campaign-launcher extraction, the convergence campaign merge,
  and the delta-to-run pipeline work all arrived from elsewhere. Fetch before
  assuming anything about the tree.
- **Test and push as separate steps.** `pytest ... | tail && git push` gates
  the push on `tail`, not on the tests.
- **Timing-sensitive tests exist and flake under load.**
  `tests/test_dashboard_e2e.py` drives a real browser over CDP;
  `tests/test_relax_gate_timeout_classification.py` failed once in this
  session under a loaded machine and passed in isolation. Re-run in isolation
  before concluding a change broke either.

### Closed since 20260819

| was | what happened |
|---|---|
| **1. Controller-liveness commit** | Reviewed and merged with a fix. `98f92e0` + `3b8a743`, merged `eebfa03`. |
| **2. Run a real campaign** | Two ran. The convergence campaign completed (CC-01…CC-05, CC-07 sealed) and merged from another session; CC-08 ran here as a two-node graph. |
| **3. Port `dashboard_improve` steps 1–3** | Merged whole, `f2b85c6`. |

Detail on each, since the reasoning matters more than the outcome:

**The liveness lease was worth merging, and the operator's recorded
inclination to reject it as overengineering did not survive the code.** The
gap was real: `run_catalog.py` reads `liveness.json` and only the dashboard
fixture ever wrote one. The cheaper alternative genuinely could not work, for
a reason stronger than the original commit gave — `plan-graph-admission-
liveness.json` is written only by `plan_graph_audit`, so FeatureRuns have no
marker at all and teaching the catalog to read it would have changed nothing
for them. The `_CHILD_LIVENESS_NAMES` alias removal, flagged as a behaviour
change needing its own look, is inert: nothing writes
`plan-graph-liveness.json`, so both readers return `None` before and after.

One real defect was found and fixed before merging: the lease was released
only in `finalize`, and `run_feature_worktree` had no `try`/`finally` across
the ~450 lines between journal creation and finalize. Launchers run in a
`ThreadPoolExecutor`, so an escape left an abandoned run heartbeating under a
live pid and reading `live` — the wedged-controller answer the lease exists to
rule out. Confirmed working in production: the CC-08 graph's attempt and its
child both read `live` through the catalog, which before this would have read
`liveness_unavailable`.

**CC-08 shipped and is on `main`** — the whole ADR 0007 pipeline. See
[`SESSION_HANDOFF_CC08_ESCALATION_20260819.md`](SESSION_HANDOFF_CC08_ESCALATION_20260819.md),
revised in this session to match the tree it was built on.

**The dashboard port's numbers were re-measured, not trusted.** The port
plan's 82 new passing tests held exactly; its "contract errors unchanged" held
while its absolute drifted 29 → 32; its "three conflicting files, all
additive" did not — four, two of them landing squarely on the frozen-snapshot
health check the plan itself named as the hazard. `dashboard_improve` branched
before `f992748`, so its side of `dashboard_server.py` has no health check and
its `tests/test_dashboard_api.py` has zero references to it against `main`'s
four. That is now a conflict rather than a silent copy, which is the safer
failure. All four resolved as unions.

---

## Re-ranked backlog

### 1. Exercise escalation live — it is wired everywhere and has never fired

**The dominant standing risk, and the direct successor to "run a real
campaign".** CC-08 is complete, on `main`, and now enabled in two places: the
CC-08 runner, and `harness_labs/graphrun/campaign_launcher.py`, which another
session wired with an `escalation_judge` seat,
`ReviewFixPolicy(escalation_enabled=True)`, and `transfer_ownership`
authority. So the *next campaign launched through the shared launcher will
have escalation on*.

It has never run. `grep -rl plan_graph_escalation_judged logs/runs/` returns
nothing: no judge has ever been invoked, no finding has ever been routed, no
node has ever been unsealed. CC08-A and CC08-B both sealed without raising an
out-of-grant finding, so even the graph that built the feature did not
exercise it.

What is untested in anger: the seat's JSON extraction against a real model's
prose, the retry path, the refusal-to-block path, the reviewer-independence
refusal, one structural decision per unseal, the bounded fix-only loop's
"never construct a review stage", and the cascade through `repair_selection`.
Every one has unit fences; none has controller evidence.

Cheapest instrument first: a confirm-everything stub judge
(`ConfirmEverythingStubJudge`, already shipped) against a plan with a
deliberately cross-node finding proves routing, unsealing, budget spend and
cascade with no model spend, and separates "the plumbing works" from "the
judgment is any good".

### 2. `run_cc08_plan_graph.py` is an 823-line fork of shared library code

It was written in this session as a deliberate copy of the convergence runner,
justified because refactoring that file would have meant editing a script a
live campaign was executing from. **That justification is now stale.** The
campaign finished, and another session extracted the machinery into
`harness_labs/graphrun/campaign_launcher.py` (1032 lines) behind
`build_campaign_launch_config()`. `experiments/run_convergence_plan_graph.py`
is now a 48-line shim over it; `run_cc08_plan_graph.py` does not import it at
all.

The two have already diverged in a way that proves the cost: the shared module
folds one `ANTI_PLACEHOLDER_FLOOR` into **all four** worker seats, while the
CC-08 fork carries its own copy across three. Migrating CC-08 to the shim form
is mechanical and removes a whole class of future drift.

### 3. `open_obligations` is unconditionally empty

Verified still open at `daa2232`. `plan_graph.py:3226` reads
`node.get("finding_obligations", [])`, but `finding_obligations` is passed to
`PlanGraphAudit._transition` as a **top-level state field**, never onto the
node record — `plan_graph_audit.py:173` initialises it as a peer of the node
map, and `node_completed`/`node_failed` forward it there. So the field the
block artifact reads is never the field anything writes.

This matters more now than when it was filed: the block artifact is what an
operator reads after an escalation blocks, and CC-08 made blocks more likely.
Mechanism spot-checked, consequence not measured.

### 4. The `origin_node` bypass skips later guards

Verified still open at `review_fix.py:467`. The
`if record.get("origin_node") or record.get("inherited")` branch `continue`s
past **all** later branches, so a transferred or inherited finding also skips
the citation guard and the sub-threshold `note` demotion. A low-score
inherited finding stays `open` and can block forever.

Over-blocking direction, so it loses no findings — which is why the
adversarial agent reported it and left it alone. CC-08 raises its
significance: escalation write-backs set `origin_node` deliberately, so this
branch is now on a hot path it was not on when the defect was filed.

### 5. Stabilize the timing-sensitive tests

`tests/test_dashboard_e2e.py` (real browser over CDP, wall-clock deadlines) and
`tests/test_relax_gate_timeout_classification.py`. The second flaked once in
this session under load and passed in isolation; a fix for the underlying
red-phase timeout budget landed from another session (`706e555`), and
[`relax-gate-timeout-flake-20260819.md`](relax-gate-timeout-flake-20260819.md)
documents it. Whether that closes the class or only one instance is untested.

### 6. The Completed tab is live and empty

The dashboard merge shipped the snapshot viewer and its offline builder. No
snapshots exist, so the tab renders nothing — the state the port plan called
"worse than no tab".

**Operator decision recorded 2026-08-20: deferred.** Worth knowing before
deferring further: nothing needs building. `scripts/build_plangraph_snapshot.py`
and [`../observability/completed-plangraph-viewer.md`](../observability/completed-plangraph-viewer.md)
both shipped, the backfill is one command with a `--dry-run` first, and unlike
when the port plan was written there is now real content for it — the
convergence campaign, CC-08, and the dashboard metrics campaign have all
completed.

```
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --all-completed --repository .
```

Either run it or gate the tab; leaving it empty is the one option the port
plan argued against.

### 7. The gate-slot bypass, on the campaign that ran parallel lanes

`plan_graph_gate_slot_bypassed` records a node that succeeded with a
verification command whose mutual-exclusion slot was never entered. CC-08 hit
it once and it was structurally harmless there: CC08-B depends on CC08-A, so
nothing ran concurrently.

The convergence campaign hit it **8 times across 4 attempts**, on every one of
its six nodes, and that campaign ran `max_parallelism=5` with deliberately
parallel lanes. There the guarantee the harness intended to provide did not
apply where it was meant to. Whether it caused harm depends on whether the
lanes' verification commands can interfere — they are file-disjoint by design,
so possibly not. **Not established either way**; only that the guarantee was
absent.

### 8. Smaller items

- **Two ADRs numbered 0006.** `0006-parallel-plangraph-contract.md` and
  `0006-repository-bound-plan-approval.md` both exist; `docs/decisions/README.md`
  lists only the second. Renumbering breaks every citation, including §2 of the
  CC-08 handoff. Needs an operator to say which is canonical.
- **`origin_reviewer_id` is a node id, not a reviewer identity.** ADR 0007
  states reviewer-independence in session-level language; the code compares
  `run.id`, because `ReviewLedger._new_record` carries no reviewer field. The
  rule therefore reads "the judge must not be this node", which a graph-level
  seat cannot violate — correct, and unable to fire. Commented at both sites in
  `3f56354` so whoever adds a reviewer field meets the warning first. No action
  needed unless someone adds one.
- **Unchanged-strategy guard compares globally, not per class**, in
  `_recover_abnormal`. Reachable only when a continuation immediately follows
  an identical continuation.
- **Whitespace-padded anchors.** A non-required `" feature.txt"` is still
  screened. The adversarial test that appears to cover this passes for the
  wrong reason — its fixture is `requires_disposition`, so the exemption
  rescues it.
- **Severity for shared directory grants** stays `info` while same-file
  overlaps are `high`.
- **`900` hardcoded** in `controller_live_scenarios.py:392,420`, alongside a
  hardcoded Retinology path.
- **32 directory grant entries remain unroutable** after the routing fix,
  because `_transfer_targets_for` drops any grant held by two equidistant
  descendants. Decomposition shape, not a harness defect.
- **Two unmerged worktree-agent branches** remain from the 20260818 audit:
  `worktree-agent-af9ce5232915de3ee` (scope-block study) and
  `worktree-agent-aacc87babf81c7a02` (tech-debt-collector design, which
  declines it). Merge when convenient. `worktree-agent-aa29a4426ec30342a`
  (abandoned partial leak fix) must **not** be merged; it is superseded by
  `4c79877`.

### 9. Deferred pending escalation evidence

The 20260819 handoff deferred three things "pending campaign evidence". The
campaign ran; none of the three got its evidence, because the campaign
predated CC-08 and the CC-08 graph never escalated. They now wait on item 1.

- **The SEAM**, marked in `ReviewLedger.ingest`. Genuinely out-of-grant
  findings are still discharged, but counted. CC-08 gives them a destination
  for the first time, so the question "block, carry, or collect" is now
  answerable by measurement rather than argument — once escalation has
  actually fired.
- **Bidirectional `finding_transfer_targets`.** Partly overtaken: CC-08's
  `_owner_for_paths` already routes to a node *upstream* of the escalating one,
  which is what the original motivation asked for. What remains is the sealed-
  upstream case needing a repair successor.
- **Tech-debt collector: declined on evidence.** Argument unchanged, in the
  design document on `worktree-agent-aacc87babf81c7a02`.

---

## Context a fresh session will need

**The campaign launcher is now shared library code.**
`harness_labs/graphrun/campaign_launcher.py` carries the pinned agent mixture,
the worker instructions, the review-fix wiring, the escalation seat, and the
recovery limits, behind `build_campaign_launch_config()`. Per-campaign runners
should be thin shims over it — `run_convergence_plan_graph.py` is the model at
48 lines. Anything that edits worker instructions belongs there now, not in a
runner.

**Escalation is on by default in that module**, with the coordinator spec
doubling as the judge seat and `max_structural_decisions = 2`. A campaign
launched through it inherits a feature that has never fired. That is the whole
of item 1.

**The judge seat is platform-agnostic and must stay that way.**
`harness_labs/graphrun/escalation_judge.py` binds through
`build_coordinator_session` on a `provider:model@effort` spec and never touches
`ClaudeSemanticTaskExecutor`. Its `.identity` is a fixed seat name, never
derived from the spec, so a provider swap cannot silently change independence
semantics. All validation, retry and refusal live in the seat rather than the
provider, so every backend gets identical guarantees.

**A judge that cannot answer blocks; it never invents a verdict.**
`EscalationJudgeUnavailable` is caught by name in `_resolve_escalations`, so
third-party judges' exceptions still propagate. `CONFIRM` spends a structural
decision on no evidence and `REJECT` is permanent — `prior_escalation_verdict`
forces a repeat escalation of the same key to an operator block rather than
re-judging it — so only a block is undoable. ADR 0007 was amended on
2026-08-20 to state this.

**Working conventions that earned their keep, again:**

- **Every fix proves its tests by reverting the production change**, confirming
  the exact failing assertion, and restoring. This session it caught a
  subagent's revert that *passed* because a fallback covered it; the agent
  redid the revert properly rather than reporting a green.
- **Verify subagent claims before relaying them.** Also verify the tree before
  trusting a handoff: the CC-08 document's ground truth was correct when
  written and wrong by the time the work started, and it instructed branching
  off a commit that predated the very documents it marked Binding.
- **Measure a stale plan's numbers again rather than quoting them.** The
  dashboard port plan was right about two of three claims and wrong about the
  one that mattered.
- **Prefer merge over copy, and prefer a conflict over a clean overwrite.** The
  dashboard hazard was safe precisely because it conflicted.
