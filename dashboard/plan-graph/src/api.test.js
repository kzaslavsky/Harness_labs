import test from 'node:test';
import assert from 'node:assert/strict';
import { defaultGraphAttempt, displayState, elapsedMs, graphProjection, liveGraphs, planGraphGroups, selectedRunFor, shouldPoll, stateLabel, validateCatalog, validateGraphMetrics, validateRunDetail, validateSnapshotDocument, validateSnapshotsListing } from './api.js';
import { distributionSummary, metricValue, money } from './format.js';

const availability = { state: 'available', reason: null };
const liveness = (state) => ({ state, reason: null });
const feature = { run_id: 'run-1', kind: 'feature_run', status: 'running', liveness: liveness('live'), evidence: availability, correlation: null };
const metrics = { protocol: 'harness-run-detail-metrics/1', totals: {}, quality: {}, provenance: {}, by_phase: [], by_agent: [], by_agent_type: [], by_model: [], by_effort: [], by_backend: [], stages: [] };
const graph = (runId, createdAt, status = 'running', nodes = []) => ({
  run_id: runId, created_at: createdAt, plan_path: 'docs/plan.md', plan_digest: 'a'.repeat(64), plan_graph_digest: 'b'.repeat(64),
  logical_graph_id: 'logical-graph-1', graph_attempt_id: runId, predecessor_attempt_id: null, retention_constraints: { state: 'unavailable', reason: 'not recorded' },
  status, liveness: liveness(status === 'running' ? 'live' : 'terminal'), evidence: availability, nodes,
});
const node = (nodeId, dependsOn = [], runId = null) => ({ node_id: nodeId, status: 'queued', feature_run_id: runId, depends_on: dependsOn, liveness: liveness('not_applicable'), evidence: availability });

test('runtime validation accepts a catalog with a correlated graph node', () => {
  const catalog = { protocol: 'harness-run-catalog-snapshot/1', revision: 'rev', generated_at: '2026-08-09T00:00:00Z', source_root: '/audit', source_roots: ['/audit', '/other/audit'], availability, diagnostics: [], feature_runs: [feature], ungrouped_feature_runs: [], plan_graphs: [graph('graph-1', '2026-08-09T00:00:00Z', 'running', [{ ...node('node-1', [], 'run-1'), status: 'running', liveness: liveness('live') }])] };
  assert.equal(validateCatalog(catalog), catalog);
  const projection = graphProjection(catalog, catalog.plan_graphs[0]);
  assert.deepEqual(projection.nodes.map((item) => item.data.runId), ['run-1']);
  assert.equal(projection.nodes[0].data.title, 'node-1');
});

test('runtime validation accepts recorded execution state and rejects incomplete state', () => {
  const execution = {
    logical_graph: { base_commit: 'a'.repeat(40), plan_digest: 'a'.repeat(64), plan_graph_digest: 'b'.repeat(64) },
    attempts: [{ node_id: 'lane', logical_attempt: 1, allocation_id: 'alloc-lane', checkpoint_revision: 1, parent_candidate_commit: 'a'.repeat(40), expected_staging_head: 'a'.repeat(40), status: 'reserved', candidate_commit: null }],
    concurrency: { active_nodes: ['lane'], active_count: 1, max_parallelism: { state: 'unavailable', reason: 'not recorded' } },
    integration: { staging_head: 'a'.repeat(40), lease: { state: 'available', reason: null }, lease_record: { node_id: 'lane', lease_id: 'lease-lane', expected_staging_head: 'a'.repeat(40) }, barriers: [{ barrier_id: 'lane:integration:alloc-lane', node_id: 'lane', attempt_id: 'graph:attempt:alloc-lane', allocation_id: 'alloc-lane', logical_attempt: 1, checkpoint_revision: 1, lease_id: 'lease-lane', action: 'lease_acquired', input_commit: 'a'.repeat(40), expected_staging_head: 'a'.repeat(40), integrated_commit: null, evidence_refs: [] }] },
    recovery: { active_allocations: [], authority: { state: 'unavailable', reason: 'not recorded' }, dispositions: [], attempt_lineage: [{ attempt_id: 'graph:attempt:alloc-lane', node_id: 'lane', logical_attempt: 1, allocation_id: 'alloc-lane', input_commit: 'a'.repeat(40), predecessor_attempt_id: null }], retry_state: { invalidations: [], reuse: [] } },
  };
  const catalog = { protocol: 'harness-run-catalog-snapshot/1', revision: 'rev', generated_at: '2026-08-09T00:00:00Z', source_root: '/audit', availability, diagnostics: [], feature_runs: [], ungrouped_feature_runs: [], plan_graphs: [{ ...graph('graph-1', '2026-08-09T00:00:00Z'), execution }] };
  assert.equal(validateCatalog(catalog), catalog);
  delete execution.recovery.authority;
  assert.throws(() => validateCatalog(catalog));
  execution.recovery.authority = { state: 'unavailable', reason: 'not recorded' };
  execution.integration.barriers[0].unexpected = true;
  assert.throws(() => validateCatalog(catalog));
});

test('a reused node validates and projects the origin run for inspection', () => {
  const originRun = { ...feature, run_id: 'graph-root-CB-01', kind: 'legacy_feature_run', status: 'succeeded', liveness: liveness('terminal') };
  const reusedCorrelation = { state: 'reused', origin_attempt_id: 'graph-root', origin_feature_run_id: 'graph-root-CB-01', reused_from_attempt: 'graph-attempt-1', reason: 'node was reused from attempt graph-root; metrics come from origin run graph-root-CB-01' };
  const reusedNode = { ...node('CB-01', [], 'graph-attempt-2-CB-01'), status: 'succeeded', reused_from_attempt: 'graph-attempt-1', candidate_commit: 'f'.repeat(40), correlation: reusedCorrelation, evidence: { state: 'partial', reason: reusedCorrelation.reason } };
  const catalog = { protocol: 'harness-run-catalog-snapshot/1', revision: 'rev', generated_at: '2026-08-09T00:00:00Z', source_root: '/audit', availability, diagnostics: [], feature_runs: [originRun], ungrouped_feature_runs: [], plan_graphs: [graph('graph-attempt-2', '2026-08-09T02:00:00Z', 'running', [reusedNode])] };
  assert.equal(validateCatalog(catalog), catalog);
  const projection = graphProjection(catalog, catalog.plan_graphs[0]);
  assert.equal(projection.nodes[0].data.runId, 'graph-root-CB-01');
  assert.equal(projection.nodes[0].data.plannedRunId, 'graph-attempt-2-CB-01');
  assert.equal(projection.nodes[0].data.reused.origin_attempt_id, 'graph-root');
  assert.equal(projection.nodes[0].data.record.run_id, 'graph-root-CB-01');
  // A malformed reuse correlation is rejected rather than displayed.
  reusedNode.correlation = { state: 'reused', origin_attempt_id: 'graph-root' };
  assert.throws(() => validateCatalog(catalog));
  // An unresolved reuse stays visibly unresolved: no origin run is invented.
  reusedNode.correlation = null;
  assert.equal(validateCatalog(catalog), catalog);
  assert.equal(graphProjection(catalog, catalog.plan_graphs[0]).nodes[0].data.runId, null);
});

test('attempts are grouped by logical graph identity and newest live attempt is selected', () => {
  const older = graph('attempt-old', '2026-08-09T00:00:00Z', 'failed', [node('root')]);
  const live = graph('attempt-live', '2026-08-09T00:02:00Z', 'running', [node('root')]);
  const newestTerminal = { ...graph('attempt-terminal', '2026-08-09T00:03:00Z', 'failed', [node('root')]), plan_graph_digest: 'c'.repeat(64) };
  const groups = planGraphGroups({ plan_graphs: [older, live, newestTerminal] });
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].attempts.map((item) => item.run_id), ['attempt-terminal', 'attempt-live', 'attempt-old']);
  assert.equal(defaultGraphAttempt(groups[0]).run_id, 'attempt-live');
});

test('singleton self-identities are combined as retries by plan, base, and topology', () => {
  const first = { ...graph('attempt-1', '2026-08-09T00:00:00Z'), logical_graph_id: 'attempt-1', graph_attempt_id: 'attempt-1' };
  const second = { ...graph('attempt-2', '2026-08-09T00:01:00Z'), logical_graph_id: 'attempt-2', graph_attempt_id: 'attempt-2' };
  const groups = planGraphGroups({ plan_graphs: [first, second] });
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].attempts.map((item) => item.run_id), ['attempt-2', 'attempt-1']);
});

test('same-plan graphs with different verified topology remain distinct', () => {
  const first = { ...graph('attempt-1', '2026-08-09T00:00:00Z'), logical_graph_id: 'attempt-1', graph_attempt_id: 'attempt-1' };
  const second = { ...graph('attempt-2', '2026-08-09T00:01:00Z', 'running', [node('other')]), logical_graph_id: 'attempt-2', graph_attempt_id: 'attempt-2' };
  assert.equal(planGraphGroups({ plan_graphs: [first, second] }).length, 2);
});

test('dependency projection creates layered nodes and audited edges', () => {
  const selected = graph('graph-1', '2026-08-09T00:00:00Z', 'running', [node('root', [], 'planned-root'), node('left', ['root']), node('right', ['root']), node('join', ['left', 'right'])]);
  const projection = graphProjection({ feature_runs: [] }, selected);
  assert.equal(projection.edges.length, 4);
  assert.equal(projection.nodes[0].data.runId, null);
  assert.equal(projection.nodes[0].data.plannedRunId, 'planned-root');
  const positions = Object.fromEntries(projection.nodes.map((item) => [item.data.nodeId, item.position]));
  assert.ok(positions.left.x > positions.root.x);
  assert.equal(positions.left.x, positions.right.x);
  assert.ok(positions.join.x > positions.left.x);
});

test('states distinguish queued, blocked, stale, terminal, and unavailable evidence', () => {
  assert.equal(displayState({ ...feature, status: 'queued' }), 'queued');
  assert.equal(displayState({ ...feature, status: 'blocked' }), 'blocked');
  assert.equal(displayState({ ...feature, status: 'running', liveness: liveness('liveness_unavailable') }), 'running');
  assert.equal(displayState({ ...feature, status: 'running', liveness: liveness('remote_unverified') }), 'running');
  assert.equal(displayState({ ...feature, liveness: liveness('stale') }), 'stale');
  assert.equal(displayState({ ...feature, status: 'succeeded', liveness: liveness('terminal') }), 'succeeded');
  assert.equal(stateLabel({ ...feature, evidence: { state: 'unavailable', reason: 'missing' } }), 'Evidence unavailable');
});

test('selection is stable across a refreshed catalog and becomes unavailable only when removed', () => {
  const first = { feature_runs: [feature] };
  const refreshed = { feature_runs: [{ ...feature, status: 'blocked' }] };
  assert.equal(selectedRunFor(first, 'run-1').run_id, 'run-1');
  assert.equal(selectedRunFor(refreshed, 'run-1').status, 'blocked');
  assert.equal(selectedRunFor(refreshed, 'removed-run'), null);
});

test('runtime validation rejects a fabricated or incomplete catalog', () => {
  assert.throws(() => validateCatalog({ protocol: 'harness-run-catalog-snapshot/1' }));
});

test('FeatureRun detail validation accepts the production availability projection', () => {
  const unavailable = { state: 'unavailable', reason: 'not recorded' };
  const detail = {
    lifecycle: [], criteria: [], tasks: [], findings: [], decisions: [], evidence_metadata: [], git_custody: [],
    usage: null, metrics, timing: {}, availability: {
      lifecycle: availability, criteria: availability, tasks: unavailable, findings: unavailable,
      evidence_metadata: unavailable, git_custody: availability, usage: unavailable,
    },
  };
  assert.deepEqual(validateRunDetail(detail), detail);
  delete detail.availability.usage;
  assert.throws(() => validateRunDetail(detail));
});

test('liveGraphs lists only non-terminal graphs, newest first', () => {
  const catalog = { plan_graphs: [graph('done', '2026-08-09T00:00:00Z', 'succeeded'), graph('run-a', '2026-08-09T00:01:00Z', 'running'), graph('run-b', '2026-08-09T00:02:00Z', 'queued')] };
  assert.deepEqual(liveGraphs(catalog).map((item) => item.run_id), ['run-b', 'run-a']);
  assert.deepEqual(liveGraphs(null), []);
});

test('elapsedMs derives elapsed time from started_at against the current clock', () => {
  assert.equal(elapsedMs('2026-08-09T00:00:00Z', Date.parse('2026-08-09T00:00:05Z')), 5000);
  assert.equal(elapsedMs(null), null);
  assert.equal(elapsedMs('not-a-date'), null);
});

test('tri-state metric formatting renders available, partial, and unavailable values distinctly', () => {
  assert.equal(metricValue({ state: 'available', value: 42 }), '42');
  assert.equal(metricValue({ state: 'partial', value: 42 }), '≥42');
  assert.equal(metricValue({ state: 'unavailable', value: null }), 'Unavailable');
  assert.equal(metricValue(null), 'Unavailable');
});

test('distribution summaries mark only the max as a verified lower bound when partial', () => {
  const partial = { state: 'partial', mean: 10, median: 10, max: 20, reason: 'lower bound' };
  assert.equal(distributionSummary(partial), 'mean 10 · median 10 · max ≥20');
  const available = { state: 'available', mean: 10, median: 10, max: 20 };
  assert.equal(distributionSummary(available), 'mean 10 · median 10 · max 20');
  assert.equal(distributionSummary({ state: 'unavailable' }), 'Unavailable');
});

test('cost formatting distinguishes recorded dollars from estimates', () => {
  assert.equal(money({ state: 'available', usd: 1.5 }), '$1.5000');
  assert.equal(money({ state: 'estimated', usd: 1.5 }), '≈$1.5000');
  assert.equal(money({ state: 'unavailable', usd: null }), 'Unavailable');
});

const genericMetric = (state, value, reason = null) => ({ state, value, reason });
const tokenBlock = (state, overrides = {}) => ({ state, reason: null, input_tokens: null, cached_input_tokens: null, output_tokens: null, total_tokens: null, ...overrides });
const costBlock = (state, usd = null, reason = null) => ({ state, usd, reason });
const ledgerBlock = (state, overrides = {}) => ({ state, reason: null, graph_launches: null, gate_invocations: null, repair_dispatches: null, structural_decisions: null, ...overrides });
const distribution = (state, overrides = {}) => ({ state, reason: null, mean: null, median: null, max: null, sample_size: 0, population: 0, ...overrides });
const graphMetricsDoc = (overrides = {}) => ({
  protocol: 'harness-plan-graph-metrics/1', run_id: 'graph-1', status: 'running',
  timing: { started_at: '2026-08-09T00:00:00Z', wall_clock_ms: genericMetric('available', 1000) },
  totals: {
    tokens: tokenBlock('partial', { input_tokens: 10, cached_input_tokens: 0, output_tokens: 5, total_tokens: 15, reason: 'lower bound: 1 of 2 FeatureRun(s) report verified token usage' }),
    cost: costBlock('estimated', 0.05, 'one or more FeatureRun cost records are estimated'),
    calls: genericMetric('available', 3), agent_busy_ms: genericMetric('available', 900),
    parallelism: genericMetric('available', 1.2), peak_input_tokens: genericMetric('available', 10),
  },
  retries: { budget_ledger: ledgerBlock('available', { graph_launches: 1, gate_invocations: 0, repair_dispatches: 0, structural_decisions: 0 }), node_retries: 0, graph_attempts: 1 },
  recovery: { dispositions: [], attempt_lineage_count: 1, invalidations_count: 0 },
  blockers: { count: 0, nodes: [] },
  counts: { logical_nodes: 2, feature_run_tries: 2 },
  per_feature_run: { wall_ms: distribution('available', { mean: 900, median: 900, max: 900, sample_size: 2, population: 2 }), tokens: distribution('partial', { mean: 15, median: 15, max: 15, sample_size: 1, population: 2, reason: 'lower bound: 1 of 2 FeatureRun(s) report this metric' }), cost_usd: distribution('unavailable') },
  nodes: [],
  scheduling: { critical_path_ms: genericMetric('available', 900) },
  cache: { savings_usd: genericMetric('unavailable', null) },
  lineage_totals: { tokens: tokenBlock('unavailable'), cost: costBlock('unavailable'), calls: genericMetric('unavailable', null), agent_busy_ms: genericMetric('unavailable', null), peak_input_tokens: genericMetric('unavailable', null), reason: 'no cross-attempt lineage' },
  ...overrides,
});

test('PlanGraph metrics validation accepts a full tri-state document and rejects an incomplete one', () => {
  const doc = graphMetricsDoc();
  assert.equal(validateGraphMetrics(doc), doc);
  assert.equal(doc.totals.tokens.state, 'partial');
  assert.equal(doc.totals.cost.state, 'estimated');
  const errorDoc = { protocol: 'harness-plan-graph-metrics/1', run_id: 'graph-2', status: 'blocked', error: { state: 'unavailable', reason: 'catalog write in progress' } };
  assert.equal(validateGraphMetrics(errorDoc), errorDoc);
  const broken = graphMetricsDoc();
  delete broken.counts;
  assert.throws(() => validateGraphMetrics(broken));
});

test('snapshots listing validation accepts populated and snapshot_missing entries and rejects a malformed one', () => {
  const listing = {
    protocol: 'harness-dashboard-snapshots-listing/1',
    bounds: { max_snapshot_files_per_root: 512, max_file_bytes: 4194304 },
    snapshots: [
      { run_id: 'graph-1', logical_graph_id: 'graph-1', graph_attempt_id: 'graph-1', display_name: 'Graph One', status: 'succeeded', finished_at: '2026-08-09T00:00:00Z', wall_clock_ms: genericMetric('available', 1000), tokens: tokenBlock('available', { total_tokens: 150 }), cost: costBlock('available', 0.5), completeness: 'complete', snapshot_missing: false, reason: null, source_root: '/audit' },
      { run_id: 'graph-2', logical_graph_id: 'graph-2', graph_attempt_id: 'graph-2', display_name: 'Graph Two', status: 'blocked', finished_at: null, wall_clock_ms: null, tokens: null, cost: null, completeness: null, snapshot_missing: true, reason: 'no metrics snapshot has been written for this terminal graph attempt', source_root: null },
    ],
    diagnostics: [],
  };
  assert.equal(validateSnapshotsListing(listing), listing);
  const broken = { ...listing, snapshots: [{ ...listing.snapshots[0], completeness: 'invalid-grade' }] };
  assert.throws(() => validateSnapshotsListing(broken));
});

test('snapshot document validation accepts a full plangraph-metrics-snapshot/1 document and rejects an incomplete one', () => {
  const document = {
    protocol: 'plangraph-metrics-snapshot/1',
    identity: { logical_graph_id: 'graph-1', graph_attempt_id: 'graph-1', run_id: 'graph-1', plan_path: 'docs/plan.md', plan_digest: 'a'.repeat(64), base_commit: 'a'.repeat(40), repository_id: null },
    display_name: 'Graph One', status: 'succeeded',
    timing: { started_at: '2026-08-09T00:00:00Z', finished_at: '2026-08-09T01:00:00Z', wall_clock_ms: genericMetric('available', 3_600_000) },
    graph_metrics: graphMetricsDoc(),
    feature_runs: [{ node_id: 'done', objective: 'Ship the feature', display_name: 'Ship the feature', status: 'succeeded', feature_run_id: 'graph-1-done', tries: 1, detail: { state: 'available', reason: null }, metrics: null }],
    outcome: {
      nodes: [{ node_id: 'done', objective: 'Ship the feature', status: 'succeeded', criteria_satisfied: 1, criteria_total: 1, criteria_state: 'available', evidence_reason: null }],
      nodes_total: 1, nodes_attempted: 1, nodes_succeeded: 1, nodes_blocked: 0, nodes_failed: 0,
      delta: { state: 'available', reason: null, base_commit: 'a'.repeat(40), final_integrated_commit: 'b'.repeat(40), files_changed: 2, insertions: 10, deletions: 1, nodes: [{ node_id: 'done', candidate_commit: 'b'.repeat(40) }] },
      plan_sections: null, acceptance_criteria: null, narrative: 'Graph One succeeded: 1 of 1 nodes succeeded.',
    },
    data_quality: { summary_missing: false, token_records_missing: false, cost_state: 'available', busy_unavailable_reason: null, criteria_text_unavailable: true, reconstructed: false, reconstruction_notes: [], completeness: 'complete' },
    provenance: { generated_at: '2026-08-09T01:00:01Z', generator: 'harness_labs.observability.plangraph_snapshot/1', run_root: '/audit', reconstructed: false },
  };
  assert.equal(validateSnapshotDocument(document), document);
  const broken = { ...document, outcome: { ...document.outcome, narrative: '' } };
  assert.throws(() => validateSnapshotDocument(broken));
});

test('FeatureRun detail validation normalizes keyed controller families', () => {
  const detail = {
    lifecycle: [], criteria: { 'AC-1': { id: 'AC-1', status: 'satisfied' } },
    tasks: { task: { id: 'task', status: 'succeeded' } }, findings: {}, decisions: {},
    evidence_metadata: [], git_custody: [], usage: null, metrics, timing: {}, availability: {
      lifecycle: availability, criteria: availability, tasks: availability, findings: availability,
      evidence_metadata: availability, git_custody: availability, usage: availability,
    },
  };
  const normalized = validateRunDetail(detail);
  assert.deepEqual(normalized.criteria, [{ id: 'AC-1', status: 'satisfied' }]);
  assert.deepEqual(normalized.tasks, [{ id: 'task', status: 'succeeded' }]);
});

test('polling always performs the first fetch, and only skips repeats while hidden', () => {
  // Headless and embedded viewers report visibilityState 'hidden'
  // permanently; gating the initial fetch on visibility left the dashboard
  // on "Loading PlanGraph metrics…" / "No logical nodes" forever.
  assert.equal(shouldPoll('hidden', false), true);
  assert.equal(shouldPoll('visible', false), true);
  assert.equal(shouldPoll('visible', true), true);
  assert.equal(shouldPoll('hidden', true), false);
});
