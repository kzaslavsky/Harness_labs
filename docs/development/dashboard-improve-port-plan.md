# Porting `dashboard_improve` to main: survey and decision document

Status: survey complete, no code ported. Decision document only.

Date: 2026-08-19. Read-only survey of branch `dashboard_improve`
(head `a1c5069`) against `main` at `4c79877`. Merge base is
`b5a39b1`. Every claim below is either a command output reproduced
here or a file:line citation. Where I inferred rather than verified, I
say so.

## Headline result

**A whole-branch merge into current `main` is far less painful than the
framing suggests, and it does not regress the frozen-snapshot health
check.** I performed the merge in a scratch worktree branch. Three files
conflicted; all three conflicts are additive (an import list and a
constants block), and resolving them by taking both sides yields a tree
where the full Python suite and the SPA's own Node test suite pass, with
no new repository-contract errors.

| Measurement | `main` (`4c79877`) | Trial merge (`scratch/trial-merge-dashboard`) |
| --- | --- | --- |
| `python3 -m pytest tests/ -q` | 648 passed, 2 skipped, 1 xfailed | 730 passed, 2 skipped, 1 xfailed |
| `npm --prefix dashboard/plan-graph test` | (only `api.test.js` exists) | 29 pass, 0 fail |
| `python3 scripts/check_repository_contracts.py` | 29 errors | 29 errors, byte-identical list |

The interesting question is therefore not *can this be merged* but *which
parts are worth carrying*, because roughly a fifth of the diff is
campaign apparatus and machine-shaped configuration that should not land,
and the SPA it delivers has no place for any of the three pieces of
machinery `main` has gained since the merge base.

## Evidence and commands

All commands were run from a dedicated worktree,
`/Users/kirillzaslavsky/Documents/harness_labs/.claude/worktrees/agent-a96ea2bc9690d7dda`,
on branch `port-plan/dashboard-improve` (branched from `main` at
`4c79877`). Nothing was pushed and nothing outside this worktree was
written.

### Merge friction

```
$ git merge-tree --write-tree --name-only main dashboard_improve
722b679c18db485e13e63f9fa537a01d7a7232e7
docs/development/INDEX.md
harness_labs/observability/dashboard_server.py
tests/test_dashboard_api.py
CONFLICT (content): Merge conflict in docs/development/INDEX.md
CONFLICT (content): Merge conflict in harness_labs/observability/dashboard_server.py
CONFLICT (content): Merge conflict in tests/test_dashboard_api.py
```

Three files, no more. The reason is that `main` barely moved in the code
this branch touches:

```
$ git diff --stat b5a39b1 main -- harness_labs/observability/ scripts/run_dashboard.py \
    scripts/run_plan_graph.py scripts/plan_graph_recover.py dashboard/ schemas/ \
    tests/test_dashboard_api.py tests/test_dashboard_e2e.py tests/test_run_catalog.py \
    .claude/launch.json docs/development/INDEX.md docs/observability/
 docs/development/INDEX.md                      |   2 +
 harness_labs/observability/dashboard_server.py |  70 ++++++++++++-
 schemas/review-ledger.schema.json              |  50 +++++++++-
 tests/test_dashboard_api.py                    | 133 ++++++++++++++++++++++-
```

**Negative result worth stating plainly: `run_catalog.py` has not changed
on `main` since the merge base at all, and neither have the plangraph
modules that this branch touches** (it touches none of them beyond
`scripts/run_plan_graph.py` and `scripts/plan_graph_recover.py`, both of
which are also unchanged on `main`). The task framing warned that
`run_catalog.py` and the plangraph modules had moved; for the files this
branch edits, they have not. The only real overlap is
`dashboard_server.py`, +70 lines from the frozen-snapshot health check.

The three conflicts in full:

- `harness_labs/observability/dashboard_server.py:31-41` — `main` adds
  `STALE_SNAPSHOT_REFRESH_MULTIPLIER` / `MIN_STALE_SNAPSHOT_SECONDS`
  immediately after `MAX_RESPONSE_BYTES`; the branch adds
  `MAX_SNAPSHOT_FILES` and friends in the same place. Both sides keep.
- `tests/test_dashboard_api.py:18-26` — the same collision in a
  single import list. Both sides keep, alphabetised.
- `docs/development/INDEX.md:48-58` — `main` adds the patch-audit link
  where the branch adds two observability links. Both sides keep.

I resolved all three by union and committed the result to a throwaway
branch to run the measurements below, then deleted it — it carried the
units listed under "What not to port" and must not be merged. The merge
is exactly reproducible: `git merge dashboard_improve` from `main`, then
take both sides of each of the three conflicts.

### The health check is preserved by an ordinary merge

This was the stated worry, and it is unfounded, but only because of a
lucky fact rather than a design decision. **The branch never touches
`health()` at all.** On the branch:

```
$ git show dashboard_improve:harness_labs/observability/dashboard_server.py | grep -n "def health"
134:    def health(self) -> bytes:
```

and that method is the pre-merge-base three-line version returning only
`ok` / `unavailable`. There is no `health_report`, no failure counter, no
staleness net. Because the branch left the region alone, git's three-way
merge keeps `main`'s version wholesale. I verified this on the merged
tree:

```
$ grep -n "def health_report\|_consecutive_refresh_failures\|stale_after_seconds" \
    harness_labs/observability/dashboard_server.py
102:        self._consecutive_refresh_failures = 0
124:                self._consecutive_refresh_failures += 1
128:                self._consecutive_refresh_failures = 0
146:    def stale_after_seconds(self) -> float:
154:    def health_report(self) -> dict[str, Any]:
```

The increment and reset survive inside the branch's rewritten
`refresh()`. `main`'s five `DashboardHealthTests` pass on the merged
tree.

The risk is therefore not in a *merge*; it is in a **cherry-pick or
manual re-application of the branch's `dashboard_server.py`**. That file
is +536 lines on the branch and reads as a coherent rewrite; anyone who
ports it by copying the branch's version wholesale silently reverts
`f992748` and reintroduces exactly the failure mode that hid the
`/api/catalog` wedge for ~10.5 hours. **If the port is done at all, it
must be done as a merge or as hunk-level application, never as a file
copy.** This is the single highest-consequence finding in this document.

### Repository contracts

```
$ python3 scripts/check_repository_contracts.py   # on main (4c79877)
... 29 errors
$ python3 scripts/check_repository_contracts.py   # on the trial merge
... 29 errors
$ diff contracts_main.txt contracts_merged.txt
(no output)
```

Verdict on the "24 excluded errors":

- **They are pre-existing, and they are now 29, not 24.** The count grew
  on `main` for reasons unrelated to this branch — the errors are stale
  README/INDEX link targets (`harness_labs/attempts.py`,
  `text_executor.py`, `composition.py`, `agent_sessions.py`,
  `controller_kernel.py`, `controller_coordinator.py`) and missing
  `Status:` lines in a dozen-odd design and plan-review documents,
  several of which post-date the merge base
  (`HARNESS_LABS_PATCH_AUDIT_20260818.md`,
  `flow-editor-convergence-application.md`,
  `plangraph-node-sizing-review.md`).
- **The branch adds none.** The diff of the two error lists is empty.
  The branch's two new documents both carry `Status:` lines and both use
  resolvable relative links, which is exactly the rule the campaign
  imposed on itself. From
  `dashboard_improve:docs/development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md:87-91`:
  "it fails at base with 24 pre-existing errors … New docs created by
  this plan must still satisfy its rules." That self-imposed rule was
  honoured.
- **It should not gate the port.** Gating a port on errors the port does
  not cause, in files the port does not touch, would be a pure tax. It
  should gate nothing; it is separate cleanup. One caveat: the phrase
  "tracked separately" in the branch plan has no visible tracker — the
  string "24 pre-existing" appears nowhere on `main`, so the "separate
  tracking" is a claim, not an artefact.

## Verified unit inventory

The starting inventory in the request was accurate as far as it went and
wrong in one respect (it omits several things and mis-frames the
snapshot emission as living only in a script). Corrected, the 42 files
group into eight units. Sizes are `git diff --stat b5a39b1
dashboard_improve`.

### U1 — Shared graph-metrics rollup (`graph_metrics.py`)

Files: `harness_labs/observability/graph_metrics.py` (new, 962),
`tests/test_graph_metrics.py` (new, 679).

The load-bearing piece. It owns one implementation of "cumulative metrics
across a logical PlanGraph node's tries", moved out of `dashboard_server`
so the live API and the offline rollup cannot disagree. Public surface:
`attempt_ancestors`, `node_history_run_ids`,
`apply_cumulative_node_metrics`, `merge_detail_metrics`,
`read_budget_ledger`, `compute_graph_metrics`. It carries a tri-state
availability convention throughout — `available` / `partial` /
`unavailable`, plus `estimated` for cost — so a degraded aggregate never
renders missing data as zero.

- **Value:** high. This is where the three post-join correctness fixes
  live (`e4fa73a` dropped phantom tries; `0c6b8a6` stopped em-dashing
  every partial metric; `10aa873` fixed lineage keyed on
  `(plan_digest, node_id)` which had been "inflating node rows 1.0-6.6x,
  fabricating rows for nodes that never ran"). Those fixes are the
  branch's real earned value; a re-implementation would re-earn them the
  hard way.
- **Risk:** low. Pure projection over already-verified records, no I/O
  except a bounded JSONL ledger read (`MAX_LEDGER_BYTES` 4 MiB,
  `MAX_LEDGER_LINES` 20,000).
- **Merge friction:** none. New file.
- **Tests:** 679 lines, self-contained, pass on the merged tree.
- **Dependencies:** imports `_ESTIMATED_MODEL_PRICES` from `run_catalog`
  — a private cross-module symbol
  (`graph_metrics.py:29`). Noted below under "unmentioned findings".
  Otherwise standalone.

### U2 — Server endpoints for live graph metrics

Files: part of `harness_labs/observability/dashboard_server.py` (+536
total across U2/U3), part of `tests/test_dashboard_api.py` (+445).

Adds `GET /api/plan-graph-metrics/<id>` serving a
`harness-plan-graph-metrics/1` document, plus the `_Snapshot.graph_metrics`
cache field. Deliberately never serves elapsed time for a live graph;
the client derives it.

- **Value:** high — this is what makes U1 visible.
- **Risk:** low-moderate. It rewrites `refresh()` and `_build_snapshot`,
  which is where the health-check interaction lives. Verified preserved
  above, but this is the hunk to review by eye.
- **Merge friction:** the one real conflict, and it is trivial.
- **Tests:** carried, pass.

### U3 — Completed-snapshot emission and serving

Files: `harness_labs/observability/plangraph_snapshot.py` (new, 538),
`schemas/plangraph-metrics-snapshot.schema.json` (new, 1688),
`scripts/build_plangraph_snapshot.py` (new, 91),
`scripts/run_plan_graph.py` (+58, partly U4),
`scripts/plan_graph_recover.py` (+8), the `/api/snapshots` and
`/api/snapshots/<id>` half of `dashboard_server.py`,
`tests/test_plangraph_snapshot.py` (new, 840),
`tests/test_snapshot_backfill.py` (new, 357),
`docs/observability/completed-plangraph-viewer.md` (new, 237).

A graph writes a schema-validated metrics snapshot on completion
(`emit_best_effort_snapshot`, called from `run_plan_graph.py` on both the
success and failure paths and from `plan_graph_recover.py`), plus an
offline backfill CLI for graphs that finished before this existed.

- **Value:** high but conditional — it only pays off if someone actually
  runs the backfill and keeps the viewer. Snapshots are the only way to
  see a graph after its run root is gone.
- **Risk:** moderate. It is the largest new on-disk contract in the
  branch (a 1,688-line schema), it shells out to `git` for the delta
  (`_git_delta`, `_parse_shortstat`), and emission is wired into the
  PlanGraph entrypoints. It is written best-effort — `SnapshotSkipped`
  and a broad catch mean a failure never fails a run — which is right,
  but it also means a broken emitter is silent.
- **Merge friction:** none beyond the shared `dashboard_server.py` file.
- **Tests:** 1,197 lines across two files, pass.
- **Dependencies:** hard dependency on U1. `dashboard_server.py` also
  reaches back into `plangraph_snapshot`'s **private** helpers —
  `plangraph_snapshot._read_budget_ledger` at `dashboard_server.py:638`
  and `_collect_node_details` mirrored at :558. U3 cannot be dropped once
  U2 lands unless those call sites are rewritten.

### U4 — Run-root self-registration and no-arg dashboard

Files: `scripts/run_plan_graph.py` `_register_run_root` (+~48),
`scripts/run_dashboard.py` (+50), the `load_audit_root_registry` path in
`dashboard_server.py`, tests in `test_dashboard_api.py:761-825` and
`test_plangraph_snapshot.py:513-782`.

Every `run_plan_graph.py` invocation atomically appends its run root to
`~/.harness_labs/dashboard-audit-roots.json` (env override
`HARNESS_DASHBOARD_AUDIT_ROOT_REGISTRY`), pruning dead entries and
capping at `MAX_AUDIT_ROOTS` (16). `run_dashboard.py` then falls back to
that registry when given neither `--audit-root` nor
`--audit-root-registry`.

- **Value:** high for the worktree-per-run workflow this repository
  actually uses — it is the difference between `python3
  scripts/run_dashboard.py` working and needing to know every worktree
  path. This is the unit an operator will notice first.
- **Risk:** **the highest of any unit, and it is a category of risk the
  rest of the branch does not have.** It is the only code here that
  writes outside the repository, into the user's home directory, as an
  unconditional side effect of running a graph. There is no opt-out flag
  — only redirection via the env var. The write is careful (temp file +
  `os.replace`, symlink-refusing, `OSError` swallowed to a stderr
  warning), but the *policy* — a run mutating user-global state without
  being asked — deserves an explicit decision rather than arriving as a
  side effect of a dashboard port.
- **Merge friction:** none.
- **Tests:** carried, and correctly parameterised through the env var so
  they never touch a real `$HOME`.
- **Dependencies:** `run_plan_graph.py` imports `MAX_AUDIT_ROOTS` from
  `dashboard_server`, i.e. a PlanGraph entrypoint now imports the
  observability HTTP module. Minor, but a new direction of dependency.

### U5 — Catalog display names, objectives, block escalation

Files: `harness_labs/observability/run_catalog.py` (+295),
`schemas/run-catalog-snapshot.schema.json` (+9),
`tests/test_run_catalog.py` (+266),
`tests/test_run_catalog_contracts.py` (new, 40).

Human-readable `display_name` for graphs and feature runs (title-cased
plan stem, attempt ordinal, disambiguated on collision), an `objective`
field capped and truncated to a first sentence, a `_predecessor_link_run_id`
recovery path, and a `block_escalation` projection
(`escalated` / `blocker_evidence_ref` / `stable_path`).

- **Value:** moderate. Cosmetic in large part, but "which of these
  seventeen `pg-…-a3f` graphs is the one I care about" is a real daily
  cost. The `block_escalation` field is genuinely new information.
- **Risk:** low. Additive schema fields, all optional.
- **Merge friction:** none today. **But this is the unit with future
  friction**: it is +295 lines concentrated in `_project_run`, and the
  in-flight `liveness.json` lease fix lives in the same file, near
  `_liveness` at the tail. I checked and the branch does **not** touch
  liveness — the only two `liveness` lines in the whole `run_catalog.py`
  diff are context on rewritten `dict` literals at the `_nodes` and
  corrupt-run sites. So the overlap is textual adjacency, not semantic
  conflict. Whichever lands second will rebase cleanly or nearly so; I
  am not proposing any liveness work here.
- **Tests:** carried, pass. `test_run_catalog_contracts.py` is worth
  landing on its own merits — six tests asserting the catalog schemas are
  closed and well-formed and that unavailable evidence is explicit rather
  than zero.

### U6 — SPA: shared formatting and live-view additions

Files: `dashboard/plan-graph/src/format.js` (new, 74),
`components/GraphTotals.jsx` (new, 64),
`components/NodeMetricsTable.jsx` (new, 68),
`components/InFlightStrip.jsx` (new, 31),
`src/App.jsx` (+93), `src/api.js` (partial, the
`validateGraphMetrics` / `fetchPlanGraphMetrics` / `elapsedMs` /
`liveGraphs` block), `src/styles.css` (+16),
`src/api.test.js` (+111).

The Live/Completed toggle, the PlanGraph totals panel, the per-node
metrics table with "this attempt" and "cumulative" columns held visually
apart, and the in-flight strip with client-derived elapsed time.

- **Value:** high; this is the user-facing payoff for U1 and U2.
- **Risk:** low, with one operational trap — see the `dist/` note below.
- **Tests:** `node --test src/api.test.js src/snapshots.test.js`, 29
  passing on the merged tree. `node_modules/` and
  `package-lock.json` are committed, so this runs offline.
- **Dependencies:** requires U2's endpoint.

### U7 — SPA: completed-snapshot viewer and comparison table

Files: `src/CompletedView.jsx` (new, 180), `src/snapshots.js` (new, 181),
`components/SnapshotBrowser.jsx` (new, 45),
`components/ComparisonTable.jsx` (new, 114),
`src/snapshots.test.js` (new, 140), the snapshot half of `api.js`.

Browse and Compare tabs over `/api/snapshots`: a left rail with two-line
outcome narratives, and a grouped, sortable 17-column cross-graph table
with a "metrics-complete only" filter.

- **Value:** moderate, and entirely contingent on U3 plus someone running
  the backfill. Without snapshots on disk this tab is empty.
- **Risk:** low in isolation; it re-uses U6's validators.
- **Dependencies:** U3 and U6 both. `CompletedView` passes
  `snapshot.graph_metrics` straight into `GraphTotals` and validates it
  with the *live* validator, so U7 cannot land without U6.

### U8 — Documentation

Files: `docs/development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md` (609),
`docs/development/dashboard-observability-metrics-decomposition.json`
(221), `docs/observability/completed-plangraph-viewer.md` (237),
`docs/observability/logging-and-metrics.md` (+15),
`docs/development/INDEX.md` (+6).

The plan and decomposition are campaign history; the viewer runbook is
live operator documentation and belongs with U3.

## What is already superseded, and what would regress `main`

This is the section to read before touching anything.

**Superseded — do not port:**

1. **The response-cap raise.** `MAX_RESPONSE_BYTES` 1 MiB → 8 MiB is
   already on `main` as `ceb68d0` (cherry-pick of the branch tip
   `a1c5069`, merged in `159136d` as patch P9), comment and all. The
   merge is a no-op here.

2. **Nothing else on the branch is superseded.** I checked specifically
   for a competing health-check implementation and found none: the
   branch has no `health_report`, no failure counter, no staleness
   threshold. The framing's worry that "the branch may contain an older
   or conflicting take on the same problem" is **not borne out** —
   the branch simply never addressed the frozen-snapshot problem. What
   it *does* contain is the incident's other half: the comment above
   `MAX_RESPONSE_BYTES` naming the failure, which `main` already has.

**Would regress `main` if ported naively:**

1. **A file-level copy of `dashboard_server.py` reverts `f992748`.** The
   branch's version of that file contains the old three-line `health()`.
   Copy it and `/api/health` goes back to answering `ok` while every
   refresh fails — the exact ~10.5-hour blindness the health check was
   built to end, and it would be reintroduced by a change whose stated
   purpose is *better observability*. A merge is safe; a copy is not.

2. **A file-level copy of `tests/test_dashboard_api.py` deletes
   `DashboardHealthTests`.** Same failure, harder to notice, because the
   suite would still be green — it would simply have stopped asking. The
   import-list conflict at line 18 is the tripwire; if a porter resolves
   it by taking the branch side, the five health tests go with it.

3. **`.claude/launch.json` would be silently repointed.** The branch
   renames `cb-dashboard` → `plan-graph-dashboard`, drops
   `--audit-root logs/runs/cb-graph`, and moves port 8321 → 8000. That
   is correct *given* U4, and wrong without it: a no-arg dashboard with
   no registry present errors out. Port it only in lockstep with U4, or
   not at all.

No other regression surface exists. `main`'s work since the merge base —
per-class recovery budgets, the scope screen, refinement advisories — is
in `harness_labs/featurerun/` and `harness_labs/plangraph/`, which this
branch does not touch.

## What the port does *not* buy: the three new machineries

`main` has gained substantial machinery whose evidence a dashboard should
ideally surface. **The branch has no place for any of it, and two of the
three are structurally locked out.** I grepped the entire branch for
`recovery_class|stop_cause|scope_screen|screened_count|findings_discharged|advisor|unclaimed`;
the single hit is a pre-existing enum value in
`schemas/review-ledger.schema.json`, unrelated to any dashboard
projection.

1. **Per-class recovery budgets** (`3c99707`, `e6f6853`). Lives on `main`
   in `harness_labs/featurerun/feature_run.py`: `_recovery_class()` at
   :512, `_RecoveryState.class_limit` / `class_used` at :543/:548,
   `continuation_recovery_limit` at :753, and per-decision
   `recovery_class` / `stop_cause` records at :1684-1778. The branch's
   `retries.budget_ledger` block is a **different** ledger — the
   PlanGraph `retry-budget-ledger/1` read by `graph_metrics.read_budget_ledger`
   — with four counters (`graph_launches`, `gate_invocations`,
   `repair_dispatches`, `structural_decisions`) and no per-class notion.
   Worse, the client validator is closed: `api.js`'s `validLedgerBlock`
   uses `hasOnly` over exactly those four keys, so adding a field
   server-side without editing the SPA makes the document fail
   validation and the panel throw. Surfacing this is new work at every
   layer.

2. **Scope-screening counters** (`b6fd71e`, `4c79877`). On `main`:
   per-finding `outcome: "scope_screened"` + `scope_screen_class` in
   `harness_labs/featurerun/review_fix.py:461-479`, aggregated by
   `ReviewLedger.scope_screening()` at :553-583, plus
   `findings_discharged` in `feature_run.py:1359`. The branch has one
   accidental partial path and no aggregate path. Accidental: the raw
   findings list is forwarded verbatim by
   `run_catalog.py:635` **on the branch** and rendered by `App.jsx`'s
   `ReadableList` as up to eight unlabelled scalar rows, so a screened
   finding's fields would in fact appear — unexplained. Aggregate:
   `run_catalog.py:715-727` on the branch
   reads `state["review_fix"]` but projects only `criteria_total`,
   `criteria_satisfied`, `findings_total`, `open_findings`,
   `review_cycles`, `verification_repairs`. `screened_count` and
   `findings_discharged` are dropped. Adding `screened_count` to
   `quality` is genuinely one line — `validMetrics` only requires
   `quality` to be an object — but a graph-level counter or a Compare
   column is new work end to end.

3. **Refinement advisories** (`00b4e79`, `67be3f9`, `a8ed261`). On
   `main`: `UNCLAIMED_GRANT_WARNING` at
   `harness_labs/plangraph/plan_approval.py:48`, emitted by
   `_unclaimed_grant_warnings()` at :543-602, carried as
   `PlanRefinement.advisories`. The branch's observability layer has
   **no notion of admission warnings or advisories at all** — no field
   in the catalog, the metrics document, or the snapshot schema — and
   `validGraph` / `validGraphExecution` are `hasOnly`-closed, so even
   smuggling one through would break the client.

**What this means for the port's value.** The branch is an excellent
answer to *how much did this graph cost and how does it compare to its
siblings*. It is not, and was never designed to be, a window onto the
control machinery `main` has since grown. Porting it does not get you a
head start on surfacing recovery classes or scope screening; if anything
the closed `hasOnly` validators mean each future field costs an edit in
four places (projection, schema, validator, component). That is a real
tax, and it argues for landing the units in an order that puts the
projection layer (U1, U5) in first, where extension is cheap, and
treating the SPA as the part that will need rework anyway.

## Recommended sequence

Each step is independently landable and independently valuable.

**Step 1 — U5, catalog display names and block escalation.** Zero
conflicts, additive schema, +266/+40 lines of tests. Delivers on its own:
readable graph and run names in the existing dashboard, and a
`block_escalation` field the existing UI already tolerates. This is the
"something landable first without the rest" — it depends on nothing else
in the branch and touches no file `main` has moved. It also lands
`tests/test_run_catalog_contracts.py`, which is a net gain regardless of
whether anything else follows.

**Step 2 — U1, the shared rollup.** A new module and its test file.
Nothing imports it yet, so this cannot break anything; it is a
"land the library, wire it later" step. Its value on its own is the
three correctness fixes being in the tree and under test rather than
stranded on a branch. Before landing, replace the
`_ESTIMATED_MODEL_PRICES` private import with a promoted public name in
`run_catalog`.

**Step 3 — U2 + U6, live metrics end to end.** The endpoint plus the
totals panel, node table, and in-flight strip. This is where an operator
first sees new numbers. **Resolve the `dashboard_server.py` and
`test_dashboard_api.py` conflicts by union, and verify
`DashboardHealthTests` still exists and passes before committing.**
Rebuild the SPA bundle (below).

**Step 4 — U4, self-registration and the no-arg dashboard.** Held to its
own step precisely because it is a policy decision, not a code decision:
do we accept that every graph run writes to `~/.harness_labs/`? If yes,
land it together with the `.claude/launch.json` change, which is only
correct in its presence. If no, land `run_dashboard.py`'s registry
fallback *without* `run_plan_graph.py`'s writer, and register roots by
hand — the fallback is useful on its own.

**Step 5 — U3 + U7, snapshots and the completed viewer.** The largest,
newest on-disk contract and the tab that reads it. Worth doing only if
someone commits to running the backfill:
`python3 scripts/build_plangraph_snapshot.py --run-root logs/runs
--all-completed --repository .`. The runbook warns that omitting
`--repository .` permanently degrades `outcome.delta` and criteria text
on every snapshot written, repairable only with `--repository . --force`
— so this step has a get-it-right-the-first-time operator action
attached, which is a good reason to keep it last and deliberate.

**At every step that touches `dashboard/plan-graph/src/`: rebuild
`dist/`.** The prebuilt bundle is committed and is what
`--assets-root dashboard/plan-graph/dist` actually serves. I verified the
branch's `dist/` is a faithful build of the branch's `src/` — after
`npm --prefix dashboard/plan-graph run build` on the merged tree,
`git status dashboard/plan-graph/dist` is clean, i.e. the rebuild is
byte-identical. Porting `src/` without rebuilding leaves the new views
present in source and invisible in the browser.

## What not to port

- **`experiments/run_dashboard_metrics_plan_graph.py`** (623 lines). The
  campaign runner that produced the branch. Campaign apparatus; it
  belongs in the campaign's history, not on `main`. `main` already
  carries several such runners, which is arguably a mistake it need not
  repeat.
- **`docs/development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md`** (609)
  and **`dashboard-observability-metrics-decomposition.json`** (221).
  Campaign planning artefacts, superseded by their own delivery. If any
  of it is kept, keep the "review resolutions" section, which records
  real design decisions. `docs/observability/completed-plangraph-viewer.md`
  is different — that is a live runbook and should land with U3.
- **The `MAX_RESPONSE_BYTES` hunk.** Already on `main`. Verbatim no-op.
- **The branch's `health()`.** Explicitly, loudly, not.
- **`.claude/launch.json`** unless U4 lands with it. The rename plus port
  change plus dropped `--audit-root` is a coherent configuration only in
  U4's presence, and this file is local developer configuration that
  different people reasonably want pointed at different things.

## Things nobody mentioned that I think matter

1. **`dist/` is committed and rebuild-identical.** Nobody flagged that
   the served UI is a checked-in artefact. It means (a) a src-only port
   ships an invisible feature, and (b) the branch author rebuilt on every
   src-touching commit — `git log -- dashboard/plan-graph/dist` and
   `-- src` return the same three most recent commits — so the bundle is
   trustworthy. The good news is that `node_modules/` and
   `package-lock.json` are committed too, so `npm run build` works
   offline with no install.

2. **Two private cross-module imports.** `graph_metrics.py:29` imports
   `run_catalog._ESTIMATED_MODEL_PRICES`; `dashboard_server.py:638`
   calls `plangraph_snapshot._read_budget_ledger` and :558 admits to
   mirroring `plangraph_snapshot._collect_node_details`. These are the
   seams that will hurt later, and they are cheap to fix *before*
   landing (promote three names) and expensive after.

3. **`run_plan_graph.py` now imports `dashboard_server`.** A PlanGraph
   entrypoint taking a dependency on the observability HTTP module, for
   one constant (`MAX_AUDIT_ROOTS`). Small, but it is a new direction of
   coupling and it will read as accidental to whoever finds it next.

4. **The self-registration write is the only user-global side effect in
   the branch, and it has no opt-out.** Flagged above under U4, repeated
   here because it is the item most likely to be waved through as "just
   dashboard plumbing".

5. **The delivery-side documentation for this branch does not exist.**
   I checked `.claude/worktrees/handoff-review-f71fb8/docs/development/`
   — 59 documents, none about the dashboard observability campaign. The
   plan, decomposition, and runbook exist **only** on `dashboard_improve`,
   and that worktree's `INDEX.md` does not link them. The only delivery
   record is the branch's own commit messages, which are unusually candid
   (see U1) but are not a handoff. There is also no session journal.
   Anyone porting this is working from the commits, and the commits are
   good enough — but the "described from the delivery side" framing
   overstates what is written down.

6. **The branch's own quality bar was met.** The three post-join
   correctness fixes (`e4fa73a`, `0c6b8a6`, `10aa873`) are the campaign
   catching its own arithmetic errors after the join and fixing them with
   stated verification. `10aa873` in particular found node rows inflated
   "1.0-6.6x" and one attempt over-counted "by 58.9%". That is a branch
   that was actually driven, not merely written — which raises my
   confidence in porting it rather than reimplementing it.

## Overall recommendation

**Port it, in part, now — and specifically steps 1 through 3.**

The cost is far lower than the 19-commit / +10,004-line framing suggests:
three trivial conflicts, 82 new passing tests, no new contract errors,
and the frozen-snapshot health check survives an ordinary merge intact.
The value in U1 is real and hard to re-earn, because it is three
arithmetic bugs already found and fixed. U5 is free. U6 is the payoff.

Hold step 4 pending an explicit decision about the home-directory write,
and hold step 5 until someone will actually run the backfill — an empty
Completed tab is worse than no Completed tab.

Do not treat this port as progress toward surfacing `main`'s newer
machinery. It is orthogonal, and the closed `hasOnly` validators it
introduces make that later work slightly more expensive, not less. That
is an acceptable price for what U1 through U6 deliver, but it should be
paid knowingly.
