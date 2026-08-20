# Completed PlanGraph viewer: backfill and viewing runbook

Status: active

This is the operator runbook for `plangraph-metrics-snapshot/1`, the
persisted, read-only metrics document that lets the dashboard show completed
PlanGraphs (including ones from a worktree that no longer exists) without
re-reading a live journal. It covers the offline backfill CLI, the snapshot
layout, the data-quality flags a viewer must render honestly, and the
post-merge operator step that reconstructs the historical corpus.

See also: [`../development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md`](../development/DASHBOARD_OBSERVABILITY_METRICS_PLAN.md)
(the frozen plan this runbook implements, node DM-07), the schema at
[`../../schemas/plangraph-metrics-snapshot.schema.json`](../../schemas/plangraph-metrics-snapshot.schema.json),
and [`logging-and-metrics.md`](logging-and-metrics.md) for the run-directory
contract the builder reads.

## What gets built, and by whom

- `scripts/run_plan_graph.py` writes a snapshot best-effort immediately
  after a graph attempt reaches a terminal state.
- `scripts/plan_graph_recover.py` writes one best-effort after a recovery
  coordinator finalization (graphs terminalized outside the runner would
  otherwise never get one).
- `scripts/build_plangraph_snapshot.py` is the offline CLI below: it
  reconstructs snapshots for graphs that finished before either writer
  existed, or whose best-effort write failed.

All three call the same read-only builder,
`harness_labs.observability.plangraph_snapshot.build_snapshot`, which
computes every number through `harness_labs.observability.graph_metrics`
(the shared rollup the live `/api/plan-graph-metrics/<id>` endpoint also
uses), so a snapshot and the live dashboard can never numerically diverge.
The builder only reads run directories; the one write path,
`write_snapshot`, is atomic and writes exclusively under
`<run-root>/.plan-graph-snapshots/`, never inside a run directory. A
snapshot failure is a warning and never alters run status or journals.

## Post-merge backfill (the actual historical reconstruction)

`logs/runs` is populated only in the primary checkout, not in a worktree, so
reconstructing the historical corpus is a step an operator runs once in the
primary checkout after this plan's PlanGraph nodes are merged. **The
"reconstruct all prior completed PlanGraphs" requirement is satisfied at
this step** — the `--dry-run` count report below is its verification, not a
promise to be fulfilled later.

Run these in order, from the primary checkout. `--repository .` is required
in the real-run command below: without it, `outcome.delta` and the
digest-checked criteria/section text degrade to `unavailable` for every
reconstructed snapshot (see `criteria_text_unavailable` below), and because
`write_snapshot` is an idempotent no-op on an existing target, a later plain
re-run cannot repair them — repairing an already-written batch requires
re-running with both `--repository .` and `--force`.

```sh
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --all-completed --dry-run
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --all-completed --repository .
python3 scripts/run_dashboard.py --assets-root dashboard/plan-graph/dist
```

1. **`--dry-run` count verification.** The first command reconstructs every
   snapshot in memory but writes nothing, then prints one JSON line to
   stdout:

   ```json
   {"run_root": "...", "dry_run": true, "reconstructed": <n>, "skipped": <n>, "failed": <n>,
    "reconstructed_graph_ids": [...], "skipped_details": [...], "failed_details": [...],
    "scanned_total": <n>, "diagnostics": [...]}
   ```

   Read `reconstructed` + `skipped` + `failed` against the number of
   terminal PlanGraph run directories under `logs/runs` (`succeeded`,
   `failed`, or `blocked`; interrupted graphs are `skipped` unless you pass
   `--include-interrupted`, and non-PlanGraph or unverifiable directories —
   launcher-style dirs with no `events.jsonl`, corrupt journals — never
   enter this count at all: they are excluded upstream by
   `build_run_catalog`). `scanned_total` is every non-dot-prefixed directory
   `build_run_catalog` found under `--run-root` (PlanGraph and FeatureRun
   alike, including the corrupt/excluded ones), and `diagnostics` is that
   catalog's own `diagnostics` list verbatim — the `corrupt_run` entries
   there (e.g. a launcher-style dir's missing `events.jsonl`) are exactly
   the directories that never entered `reconstructed`/`skipped`/`failed`,
   so `reconstructed` + `skipped` + `failed` plus the non-PlanGraph and
   corrupt/excluded entries counted in `scanned_total` should reconcile to
   `scanned_total` with nothing left unaccounted for. Every `skipped_details`
   / `failed_details` entry carries a `graph_id` and a human-readable
   `reason`; a nonzero `failed` count is the only thing that should stop you
   here — it means a directory that verified as a terminal PlanGraph still
   could not be projected (a real corruption or a bug, not an expected
   historical gap), and the CLI's exit code is `1` in that case.
2. **The real run.** The second command performs the identical sweep and
   writes each reconstructed document under
   `logs/runs/.plan-graph-snapshots/<graph_attempt_id>.json`, this time with
   `--repository .` so `outcome.delta` and the digest-checked criteria/
   section text are populated instead of `unavailable`. It is idempotent: an
   existing snapshot is left untouched unless you pass `--force`, so
   re-running it after a partial failure or after new graphs have completed
   only fills the gap; re-running it with `--force` is also how you repair a
   batch that was written before `--repository` was added to this command.
3. **Launch the dashboard.** The third command builds nothing further; it
   serves the catalog, the live rollup, and the snapshot listing from
   `logs/runs` (build the frontend first if `dashboard/plan-graph/dist`
   does not exist yet: `npm --prefix dashboard/plan-graph run build`).
   Reconstructed graphs are then visible immediately in the dashboard's
   completed-PlanGraph view (built by this plan's DM-05/DM-06 frontend
   nodes) through `GET /api/snapshots` and `GET /api/snapshots/<id>`,
   without any further backfill step.

Other useful invocations:

```sh
# One specific graph, e.g. to retry after a `failed` entry above
# (add --repository . for the git-derived delta and criteria/section text):
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --graph <graph_attempt_id> --repository .

# Also snapshot graphs whose terminal status is "interrupted" (a crashed or
# killed controller -- included only on request because its evidence is
# inherently degraded):
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --all-completed --include-interrupted

# Overwrite existing snapshots (e.g. after a graph_metrics rollup change):
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --all-completed --force

# Write elsewhere instead of the default <run-root>/.plan-graph-snapshots:
python3 scripts/build_plangraph_snapshot.py --run-root logs/runs --all-completed \
  --output-dir /path/to/snapshots
```

The CLI never touches a run directory: `--run-root` is read-only except for
the sibling `.plan-graph-snapshots/` directory it creates, exactly like the
existing `.plan-graph-budgets` / `.plan-graph-locks` infrastructure
directories (`build_run_catalog` skips all dot-prefixed entries, so this
directory is invisible to the catalog).

## Snapshot layout

```text
<run-root>/.plan-graph-snapshots/<graph_attempt_id>.json
```

One `plangraph-metrics-snapshot/1` document per graph attempt (the file
stem is `identity.graph_attempt_id`, falling back to `identity.run_id` when
the graph predates the lineage extension and has no distinct attempt id).
Each document carries: `identity` (logical/attempt/run ids, plan path and
digest, base commit, repository id); `display_name` and `status`; `timing`
(`started_at`, `finished_at`, `wall_clock_ms`); the full `graph_metrics`
rollup (attempt-scoped totals, plus a separately labelled
`lineage_totals` block for cross-attempt history); `feature_runs[]` (one row
per logical node, cumulative across tries); `outcome` (per-node criteria and
evidence, graph-level attempted/succeeded/blocked/failed counts, a
git-derived `delta`, and a templated `narrative` built only from fields
already present elsewhere in the document); `data_quality`; and
`provenance` (generator identity, source run root, `reconstructed`).

## Data-quality flags

Every snapshot follows the tri-state availability convention: a value that
cannot be verified is reported `unavailable` (or `partial`/`estimated` where
that is the metric's own contract) with a reason, never rendered or summed
as zero. `data_quality` names the flags a completed-graph list or comparison
table should surface directly rather than only the raw metric states:

- **`summary_missing`** — the graph's own `summary.json` does not exist.
  True for 25 of the 77 dirs in the reference primary-checkout sample
  (2026-07-31 → 2026-08-11), whose manifests predate the `summary_sha256`
  field; 3 of those 25 are also missing `events.jsonl` entirely and never
  reach the builder at all (see the launcher-style-dir case below), leaving
  22 real, verifiable graphs in this flag's actual population. When this is
  true and the run is otherwise verifiable, `timing.wall_clock_ms` is still
  populated where possible: it derives an estimate from the first and last
  verified journal event timestamps and reports it `partial` with a reason
  naming that derivation, rather than `unavailable`, whenever at least two
  verified events exist. (This snapshot-level fallback is separate from
  `graph_metrics.timing.wall_clock_ms`, the shared live/
  snapshot rollup value, whose own contract stays `summary.json`-only and
  reports `unavailable` in this case — the two fields can legitimately
  disagree in state.)
- **`token_records_missing`** — no FeatureRun in the graph reports any
  verified usage record (`usage_records == 0` across every child, per
  `provenance.usage_records` in the merged detail metrics). This is
  derived from record *count*, never from a totals sum being zero, so a
  FeatureRun that genuinely reports zero tokens (a real, rare shape) is not
  conflated with one that reports none at all; `graph_metrics.totals.tokens`
  is `unavailable` with `total_tokens: null` in this case, not `0`.
- **`cost_state`** — `available`, `estimated`, or `unavailable`, mirroring
  `graph_metrics.totals.cost.state`: `estimated` when any covered child's
  cost came from a published-rate estimate rather than a recorded dollar
  figure.
- **`busy_unavailable_reason`** — `null` when `graph_metrics.totals.
  agent_busy_ms` is `available`; otherwise the reason string from that
  metric (agent-busy time is `sum(child.busy_ms)`, `unavailable` if any
  covered child's is `None`).
- **`criteria_text_unavailable`** — `true` when `outcome.plan_sections` /
  `outcome.acceptance_criteria` are `null`: no repository was supplied to
  the builder, the checkpoint does not record a decomposition path and
  digest, the recorded file is missing/unsafe/oversize, or — critically —
  its SHA-256 no longer matches the recorded `plan_digest` (a fail-closed
  check: stale or tampered decomposition text is never served).
- **`reconstructed`** — `true` for every snapshot produced by this offline
  CLI (as opposed to the runner/recovery-coordinator's best-effort
  emission), and threaded into `provenance.reconstructed` too.
- **`reconstruction_notes[]`** — the human-readable reasons behind the
  flags above, e.g. `"graph summary.json is unavailable; wall clock is
  reported unavailable unless derivable from another verified source"` or
  `"no FeatureRun in this graph reports verified token usage"`. A viewer
  can show these directly as hover text.
- **`completeness`** — one derived grade for sorting/filtering a comparison
  table: `complete` when `summary_missing` is false and tokens, cost, and
  agent-busy are all in their available/estimated/partial state;
  `minimal` when none of those four signals are covered; `partial`
  otherwise. Degraded rows should render as an em-dash with a hover reason
  and sort last, with a default-on "metrics-complete only" filter — a large
  fraction of the pre-2026-08-05 corpus has no token records at all, so a
  viewer that silently showed empty stats for it would be misleading.

## Historical corpus shapes this hardens against

`tests/test_snapshot_backfill.py` builds a fixture corpus reproducing every
shape verified against the 77-directory, 2026-07-31 → 2026-08-11 sample
under `logs/runs` in the primary checkout, and asserts the builder and CLI
handle each one without failing the sweep:

- a terminal graph whose token usage lives only in its child FeatureRun
  directories (the graph's own `summary.json` never carries tokens itself);
- a terminal graph with no `summary.json` but a real, verifiable journal
  (22 of the 25 no-summary real dirs): wall time is derived from event
  timestamps, `data_quality.summary_missing` is `true`;
- a terminal graph with zero token records: `graph_metrics.totals.tokens`
  is `unavailable` with `total_tokens: null`, never `0`;
- a launcher-style directory with no `events.jsonl` at all (3 of 77 real
  dirs, from an unrelated ad-hoc tool, not this repository's audit format):
  `build_run_catalog` reports a `corrupt_run` diagnostic and the directory
  is simply absent from `catalog["plan_graphs"]`, so `--all-completed`
  never targets it and the sweep's `failed` count stays `0`;
- an interrupted checkpoint: skipped by default (`skipped`, not `failed`),
  reconstructed only with `--include-interrupted`.
