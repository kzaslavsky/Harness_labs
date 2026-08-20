// Comparison-table row construction, grouping, and sorting for the
// completed-PlanGraph viewer (plan DM-06). The snapshot *listing*
// (`GET /api/snapshots`) only carries headline metrics (tokens, cost, wall
// time, completeness, status, finished_at); every other comparison column
// is read from a fetched full snapshot document's `graph_metrics` once one
// is available. Until a document loads, those columns render as degraded
// ("unavailable") rather than fabricating a value -- matching the tri-state
// convention used across the rest of the app (plan:117-119).
import { duration, title, tokens, usd } from './format.js';

function normalizedMetric(state, value, reason = null) {
  return { state: state || 'unavailable', value: value === undefined ? null : value, reason: reason || null };
}

function fromGenericMetric(metric) {
  return metric ? normalizedMetric(metric.state, metric.value, metric.reason) : normalizedMetric('unavailable');
}

function fromTokenBlock(block) {
  return block ? normalizedMetric(block.state, block.total_tokens, block.reason) : normalizedMetric('unavailable');
}

function fromCostBlock(block) {
  return block ? normalizedMetric(block.state, block.usd, block.reason) : normalizedMetric('unavailable');
}

function fromDistribution(distribution) {
  return distribution ? normalizedMetric(distribution.state, distribution.mean, distribution.reason) : normalizedMetric('unavailable');
}

const COMPLETENESS_RANK = { minimal: 0, partial: 1, complete: 2 };

/** True when a normalized metric has nothing to sort or render. */
export function isMetricMissing(metric) {
  return !metric || metric.state === 'unavailable' || metric.value === null || metric.value === undefined;
}

/** True when a comparison-table metric must render as an em-dash with its
 * hover reason and sort last (plan:344): the value is genuinely absent.
 * `partial` (verified lower bound) and `estimated` values DO render and
 * sort by value — prefixed `≥` / `≈` exactly like the detail views
 * (plan:163-168) — because hiding them blanks nearly every cost cell in a
 * corpus where no recorded pricing exists and most token totals are
 * verified lower bounds; a labelled bound beats an em-dash. */
export function isMetricDegraded(metric) {
  return isMetricMissing(metric);
}

/** Rendering prefix for a normalized metric: `≥` for a verified lower
 * bound, `≈` for an estimate (the reason says when both apply). */
export function metricPrefix(metric) {
  if (!metric || isMetricMissing(metric)) return '';
  if (metric.state === 'partial') return '≥';
  if (metric.state === 'estimated') return '≈';
  return '';
}

/** Build one normalized comparison row from a `/api/snapshots` listing entry
 * plus (when loaded) the entry's full snapshot document. `doc` is `null`
 * when a fetch was attempted and failed, `undefined` when not yet fetched,
 * and an object once loaded; `snapshot_missing` entries never have a doc. */
export function buildComparisonRow(entry, doc) {
  const metrics = doc && doc.graph_metrics ? doc.graph_metrics : null;
  const docReason = entry.snapshot_missing
    ? entry.reason
    : doc === undefined
      ? 'the full snapshot document has not finished loading'
      : doc === null
        ? 'the full snapshot document could not be loaded'
        : null;
  const docMetric = (accessor) => (metrics ? accessor(metrics) : normalizedMetric('unavailable', null, docReason));
  const completenessValue = entry.completeness && COMPLETENESS_RANK[entry.completeness] !== undefined ? COMPLETENESS_RANK[entry.completeness] : null;
  return {
    runId: entry.run_id,
    logicalGraphId: entry.logical_graph_id || entry.run_id,
    displayName: entry.display_name || entry.run_id,
    snapshotMissing: !!entry.snapshot_missing,
    reason: entry.reason || null,
    finished_at: normalizedMetric(
      entry.finished_at ? 'available' : 'unavailable',
      entry.finished_at ? Date.parse(entry.finished_at) : null,
      entry.finished_at ? null : (entry.reason || 'finished_at was not recorded'),
    ),
    status: normalizedMetric(entry.status ? 'available' : 'unavailable', entry.status),
    total_tokens: fromTokenBlock(entry.tokens),
    cost: fromCostBlock(entry.cost),
    wall_ms: metrics ? fromGenericMetric(metrics.timing.wall_clock_ms) : fromGenericMetric(entry.wall_clock_ms),
    agent_busy_ms: docMetric((graphMetrics) => fromGenericMetric(graphMetrics.totals.agent_busy_ms)),
    parallelism: docMetric((graphMetrics) => fromGenericMetric(graphMetrics.totals.parallelism)),
    retries: docMetric((graphMetrics) => normalizedMetric('available', graphMetrics.retries.node_retries)),
    recoveries: docMetric((graphMetrics) => normalizedMetric('available', graphMetrics.recovery.dispositions.length)),
    blockers: docMetric((graphMetrics) => normalizedMetric('available', graphMetrics.blockers.count)),
    logical_nodes: docMetric((graphMetrics) => normalizedMetric('available', graphMetrics.counts.logical_nodes)),
    tries: docMetric((graphMetrics) => normalizedMetric('available', graphMetrics.counts.feature_run_tries)),
    tokens_per_feature_run: docMetric((graphMetrics) => fromDistribution(graphMetrics.per_feature_run.tokens)),
    cost_per_feature_run: docMetric((graphMetrics) => fromDistribution(graphMetrics.per_feature_run.cost_usd)),
    wall_per_feature_run: docMetric((graphMetrics) => fromDistribution(graphMetrics.per_feature_run.wall_ms)),
    cache_savings_usd: docMetric((graphMetrics) => fromGenericMetric(graphMetrics.cache.savings_usd)),
    completeness: {
      state: completenessValue === null ? 'unavailable' : 'available',
      value: completenessValue,
      reason: completenessValue === null ? 'completeness was not recorded' : null,
      display: entry.completeness || null,
    },
  };
}

// Every metric column the comparison table sorts and renders (plan:339-341):
// total tokens, est cost, wall time, agent-busy, parallelism, retries,
// recoveries, blockers, logical nodes, tries, tokens/cost/wall per
// FeatureRun, cache savings, completeness, status -- plus the default sort
// column, finished_at, which every row populates.
export const COMPARISON_COLUMNS = [
  { key: 'finished_at', label: 'Finished', display: (row) => new Date(row.finished_at.value).toLocaleString() },
  { key: 'status', label: 'Status', display: (row) => title(row.status.value) },
  { key: 'total_tokens', label: 'Total tokens', display: (row) => tokens(row.total_tokens.value) },
  { key: 'cost', label: 'Est. cost', display: (row) => usd(row.cost.value) },
  { key: 'wall_ms', label: 'Wall time', display: (row) => duration(row.wall_ms.value) },
  { key: 'agent_busy_ms', label: 'Agent-busy', display: (row) => duration(row.agent_busy_ms.value) },
  { key: 'parallelism', label: 'Parallelism', display: (row) => `${row.parallelism.value.toFixed(2)}×` },
  { key: 'retries', label: 'Retries', display: (row) => tokens(row.retries.value) },
  { key: 'recoveries', label: 'Recoveries', display: (row) => tokens(row.recoveries.value) },
  { key: 'blockers', label: 'Blockers', display: (row) => tokens(row.blockers.value) },
  { key: 'logical_nodes', label: 'Logical nodes', display: (row) => tokens(row.logical_nodes.value) },
  { key: 'tries', label: 'FeatureRun tries', display: (row) => tokens(row.tries.value) },
  { key: 'tokens_per_feature_run', label: 'Tokens / FeatureRun', display: (row) => tokens(row.tokens_per_feature_run.value) },
  { key: 'cost_per_feature_run', label: 'Cost / FeatureRun', display: (row) => usd(row.cost_per_feature_run.value) },
  { key: 'wall_per_feature_run', label: 'Wall / FeatureRun', display: (row) => duration(row.wall_per_feature_run.value) },
  { key: 'cache_savings_usd', label: 'Cache savings', display: (row) => usd(row.cache_savings_usd.value) },
  { key: 'completeness', label: 'Completeness', display: (row) => title(row.completeness.display) },
];

/** Stable sort by one normalized metric column; degraded values (missing,
 * partial, or estimated) sort last regardless of direction (plan:344 /
 * AC-DM06-2), ties break on display name so row order stays deterministic
 * across re-sorts. */
export function sortComparisonRows(rows, columnKey, direction) {
  return rows.slice().sort((left, right) => {
    const leftMetric = left[columnKey];
    const rightMetric = right[columnKey];
    const leftDegraded = isMetricDegraded(leftMetric);
    const rightDegraded = isMetricDegraded(rightMetric);
    if (leftDegraded && rightDegraded) return left.displayName.localeCompare(right.displayName);
    if (leftDegraded) return 1;
    if (rightDegraded) return -1;
    const comparison = typeof leftMetric.value === 'string' ? leftMetric.value.localeCompare(rightMetric.value) : leftMetric.value - rightMetric.value;
    return direction === 'asc' ? comparison : -comparison;
  });
}

/** Group comparison rows by logical graph (plan:339-341): each group's
 * representative is its most-recently-finished attempt (rows with no
 * `finished_at` sort last within the group, same rule as the table). */
export function groupComparisonRows(rows) {
  const groups = new Map();
  for (const row of rows) {
    const group = groups.get(row.logicalGraphId) || { key: row.logicalGraphId, rows: [] };
    group.rows.push(row);
    groups.set(row.logicalGraphId, group);
  }
  return [...groups.values()].map((group) => {
    const sorted = group.rows.slice().sort((left, right) => {
      const leftMissing = isMetricMissing(left.finished_at);
      const rightMissing = isMetricMissing(right.finished_at);
      if (leftMissing && rightMissing) return left.displayName.localeCompare(right.displayName);
      if (leftMissing) return 1;
      if (rightMissing) return -1;
      return right.finished_at.value - left.finished_at.value;
    });
    return { key: group.key, displayName: sorted[0].displayName, attemptCount: sorted.length, rows: sorted, representative: sorted[0] };
  });
}

/** The default-on "metrics-complete only" filter (plan:346-348): only rows
 * whose listing-reported `completeness` grade is exactly "complete" pass;
 * everything else (including every `snapshot_missing` stub) is hidden and
 * counted so the table states the exclusion instead of showing empty stats. */
export function filterMetricsComplete(rows) {
  const visible = rows.filter((row) => row.completeness.display === 'complete');
  return { visible, hiddenCount: rows.length - visible.length };
}
