import test from 'node:test';
import assert from 'node:assert/strict';
import { buildComparisonRow, filterMetricsComplete, groupComparisonRows, isMetricDegraded, isMetricMissing, sortComparisonRows } from './snapshots.js';

const genericMetric = (state, value, reason = null) => ({ state, value, reason });
const tokenBlock = (state, overrides = {}) => ({ state, reason: null, input_tokens: null, cached_input_tokens: null, output_tokens: null, total_tokens: null, ...overrides });
const costBlock = (state, usd = null, reason = null) => ({ state, usd, reason });
const distribution = (state, overrides = {}) => ({ state, reason: null, mean: null, median: null, max: null, sample_size: 0, population: 0, ...overrides });

function listingEntry(overrides = {}) {
  return {
    run_id: 'graph-1', logical_graph_id: 'logical-1', graph_attempt_id: 'graph-1', display_name: 'Graph One',
    status: 'succeeded', finished_at: '2026-08-10T00:00:00Z', wall_clock_ms: genericMetric('available', 60_000),
    tokens: tokenBlock('available', { input_tokens: 100, cached_input_tokens: 0, output_tokens: 50, total_tokens: 150 }),
    cost: costBlock('available', 0.5), completeness: 'complete', snapshot_missing: false, reason: null, source_root: '/audit',
    ...overrides,
  };
}

function graphMetricsDoc(overrides = {}) {
  return {
    protocol: 'harness-plan-graph-metrics/1', run_id: 'graph-1', status: 'succeeded',
    timing: { started_at: '2026-08-09T23:00:00Z', wall_clock_ms: genericMetric('available', 60_000) },
    totals: {
      tokens: tokenBlock('available', { input_tokens: 100, cached_input_tokens: 0, output_tokens: 50, total_tokens: 150 }),
      cost: costBlock('available', 0.5), calls: genericMetric('available', 4),
      agent_busy_ms: genericMetric('available', 45_000), parallelism: genericMetric('available', 1.5),
      peak_input_tokens: genericMetric('available', 80),
    },
    retries: { budget_ledger: { state: 'unavailable', reason: 'no ledger', graph_launches: null, gate_invocations: null, repair_dispatches: null, structural_decisions: null }, node_retries: 2, graph_attempts: 1 },
    recovery: { dispositions: [{ node_id: 'a', disposition: 'blocked', reason: null, forced: false, evidence_refs: [] }], attempt_lineage_count: 1, invalidations_count: 0 },
    blockers: { count: 1, nodes: [{ node_id: 'a', reason: 'blocked' }] },
    counts: { logical_nodes: 3, feature_run_tries: 4 },
    per_feature_run: {
      wall_ms: distribution('available', { mean: 20_000, median: 18_000, max: 30_000, sample_size: 3, population: 3 }),
      tokens: distribution('available', { mean: 50, median: 45, max: 90, sample_size: 3, population: 3 }),
      cost_usd: distribution('available', { mean: 0.16, median: 0.15, max: 0.3, sample_size: 3, population: 3 }),
    },
    nodes: [],
    scheduling: { critical_path_ms: genericMetric('available', 50_000) },
    cache: { savings_usd: genericMetric('available', 0.02) },
    lineage_totals: { tokens: tokenBlock('unavailable'), cost: costBlock('unavailable'), calls: genericMetric('unavailable', null), agent_busy_ms: genericMetric('unavailable', null), peak_input_tokens: genericMetric('unavailable', null), reason: 'no cross-attempt lineage' },
    ...overrides,
  };
}

test('buildComparisonRow reads listing-only fields without a full document', () => {
  const row = buildComparisonRow(listingEntry(), undefined);
  assert.equal(row.total_tokens.value, 150);
  assert.equal(row.cost.value, 0.5);
  assert.equal(row.wall_ms.value, 60_000);
  assert.equal(row.completeness.display, 'complete');
  assert.ok(isMetricMissing(row.agent_busy_ms), 'document-only metrics stay unavailable until the full document loads');
  assert.match(row.agent_busy_ms.reason, /not finished loading/);
});

test('buildComparisonRow fills every column once the full document is loaded', () => {
  const row = buildComparisonRow(listingEntry(), { graph_metrics: graphMetricsDoc() });
  assert.equal(row.agent_busy_ms.value, 45_000);
  assert.equal(row.parallelism.value, 1.5);
  assert.equal(row.retries.value, 2);
  assert.equal(row.recoveries.value, 1);
  assert.equal(row.blockers.value, 1);
  assert.equal(row.logical_nodes.value, 3);
  assert.equal(row.tries.value, 4);
  assert.equal(row.tokens_per_feature_run.value, 50);
  assert.equal(row.cost_per_feature_run.value, 0.16);
  assert.equal(row.wall_per_feature_run.value, 20_000);
  assert.equal(row.cache_savings_usd.value, 0.02);
});

test('buildComparisonRow degrades every metric column for a snapshot_missing stub without fabricating zeros', () => {
  const row = buildComparisonRow(listingEntry({
    run_id: 'graph-2', finished_at: null, wall_clock_ms: null, tokens: null, cost: null, completeness: null,
    snapshot_missing: true, reason: 'no metrics snapshot has been written for this terminal graph attempt',
  }), null);
  for (const key of ['finished_at', 'total_tokens', 'cost', 'wall_ms', 'agent_busy_ms', 'parallelism', 'retries', 'recoveries', 'blockers', 'logical_nodes', 'tries', 'completeness']) {
    assert.ok(isMetricMissing(row[key]), `${key} must degrade to missing, never a fabricated zero`);
  }
  assert.equal(row.reason, 'no metrics snapshot has been written for this terminal graph attempt');
});

test('sortComparisonRows places missing values last in both ascending and descending order', () => {
  const complete = buildComparisonRow(listingEntry({ run_id: 'complete', tokens: tokenBlock('available', { total_tokens: 200 }) }), { graph_metrics: graphMetricsDoc() });
  const missing = buildComparisonRow(listingEntry({ run_id: 'missing', tokens: null }), { graph_metrics: graphMetricsDoc() });
  missing.total_tokens = { state: 'unavailable', value: null, reason: 'no verified usage' };
  const rows = [missing, complete];
  assert.deepEqual(sortComparisonRows(rows, 'total_tokens', 'desc').map((row) => row.runId), ['complete', 'missing']);
  assert.deepEqual(sortComparisonRows(rows, 'total_tokens', 'asc').map((row) => row.runId), ['complete', 'missing']);
});

test('sortComparisonRows orders available values by direction', () => {
  const low = buildComparisonRow(listingEntry({ run_id: 'low', tokens: tokenBlock('available', { total_tokens: 10 }) }), undefined);
  const high = buildComparisonRow(listingEntry({ run_id: 'high', tokens: tokenBlock('available', { total_tokens: 90 }) }), undefined);
  assert.deepEqual(sortComparisonRows([low, high], 'total_tokens', 'asc').map((row) => row.runId), ['low', 'high']);
  assert.deepEqual(sortComparisonRows([low, high], 'total_tokens', 'desc').map((row) => row.runId), ['high', 'low']);
});

test('groupComparisonRows groups by logical graph id and picks the most recently finished attempt as the representative', () => {
  const older = buildComparisonRow(listingEntry({ run_id: 'attempt-1', logical_graph_id: 'logical-1', finished_at: '2026-08-09T00:00:00Z' }), undefined);
  const newer = buildComparisonRow(listingEntry({ run_id: 'attempt-2', logical_graph_id: 'logical-1', finished_at: '2026-08-10T00:00:00Z' }), undefined);
  const other = buildComparisonRow(listingEntry({ run_id: 'attempt-3', logical_graph_id: 'logical-2', finished_at: '2026-08-08T00:00:00Z' }), undefined);
  const groups = groupComparisonRows([older, newer, other]);
  assert.equal(groups.length, 2);
  const groupOne = groups.find((group) => group.key === 'logical-1');
  assert.equal(groupOne.attemptCount, 2);
  assert.equal(groupOne.representative.runId, 'attempt-2');
});

test('isMetricDegraded treats partial and estimated values as degraded even though they carry a real value', () => {
  assert.equal(isMetricDegraded({ state: 'available', value: 10, reason: null }), false);
  assert.equal(isMetricDegraded({ state: 'partial', value: 10, reason: 'lower bound: 2 of 3 report' }), true);
  assert.equal(isMetricDegraded({ state: 'estimated', value: 10, reason: 'derived from unit-cost heuristic' }), true);
  assert.equal(isMetricDegraded({ state: 'unavailable', value: null, reason: null }), true);
  assert.equal(isMetricMissing({ state: 'partial', value: 10, reason: null }), false, 'isMetricMissing must stay narrower than isMetricDegraded');
});

test('sortComparisonRows places partial and estimated values last, behind every fully available value', () => {
  const available = buildComparisonRow(listingEntry({ run_id: 'available', tokens: tokenBlock('available', { total_tokens: 10 }) }), undefined);
  const partial = buildComparisonRow(listingEntry({ run_id: 'partial', tokens: tokenBlock('available', { total_tokens: 999 }) }), undefined);
  partial.total_tokens = { state: 'partial', value: 999, reason: 'lower bound: 1 of 2 report' };
  const rows = [partial, available];
  assert.deepEqual(sortComparisonRows(rows, 'total_tokens', 'desc').map((row) => row.runId), ['available', 'partial'], 'a partial value must not outrank an available one even though its raw number is larger');
  assert.deepEqual(sortComparisonRows(rows, 'total_tokens', 'asc').map((row) => row.runId), ['available', 'partial']);
});

test('filterMetricsComplete hides every row whose completeness grade is not exactly "complete" and counts them', () => {
  const complete = buildComparisonRow(listingEntry({ run_id: 'complete', completeness: 'complete' }), undefined);
  const partial = buildComparisonRow(listingEntry({ run_id: 'partial', completeness: 'partial' }), undefined);
  const missingStub = buildComparisonRow(listingEntry({ run_id: 'missing', completeness: null, snapshot_missing: true, reason: 'no snapshot' }), null);
  const { visible, hiddenCount } = filterMetricsComplete([complete, partial, missingStub]);
  assert.deepEqual(visible.map((row) => row.runId), ['complete']);
  assert.equal(hiddenCount, 2);
});
