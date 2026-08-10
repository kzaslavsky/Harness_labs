import test from 'node:test';
import assert from 'node:assert/strict';
import { defaultGraphAttempt, displayState, graphProjection, planGraphGroups, selectedRunFor, stateLabel, validateCatalog, validateRunDetail } from './api.js';

const availability = { state: 'available', reason: null };
const liveness = (state) => ({ state, reason: null });
const feature = { run_id: 'run-1', kind: 'feature_run', status: 'running', liveness: liveness('live'), evidence: availability, correlation: null };
const metrics = { protocol: 'harness-run-detail-metrics/1', totals: {}, quality: {}, provenance: {}, by_phase: [], by_agent: [], by_agent_type: [], by_model: [], by_effort: [], by_backend: [] };
const graph = (runId, createdAt, status = 'running', nodes = []) => ({
  run_id: runId, created_at: createdAt, plan_path: 'docs/plan.md', plan_digest: 'a'.repeat(64), plan_graph_digest: 'b'.repeat(64),
  status, liveness: liveness(status === 'running' ? 'live' : 'terminal'), evidence: availability, nodes,
});
const node = (nodeId, dependsOn = [], runId = null) => ({ node_id: nodeId, status: 'queued', feature_run_id: runId, depends_on: dependsOn, liveness: liveness('not_applicable'), evidence: availability });

test('runtime validation accepts a catalog with a correlated graph node', () => {
  const catalog = { protocol: 'harness-run-catalog-snapshot/1', revision: 'rev', generated_at: '2026-08-09T00:00:00Z', source_root: '/audit', source_roots: ['/audit', '/other/audit'], availability, diagnostics: [], feature_runs: [feature], ungrouped_feature_runs: [], plan_graphs: [graph('graph-1', '2026-08-09T00:00:00Z', 'running', [{ ...node('node-1', [], 'run-1'), status: 'running', liveness: liveness('live') }])] };
  assert.equal(validateCatalog(catalog), catalog);
  assert.deepEqual(graphProjection(catalog, catalog.plan_graphs[0]).nodes.map((item) => item.data.runId), ['run-1']);
});

test('attempts are grouped by approved-plan digest and newest live attempt is selected', () => {
  const older = graph('attempt-old', '2026-08-09T00:00:00Z', 'failed', [node('root')]);
  const live = graph('attempt-live', '2026-08-09T00:02:00Z', 'running', [node('root')]);
  const newestTerminal = { ...graph('attempt-terminal', '2026-08-09T00:03:00Z', 'failed', [node('root')]), plan_graph_digest: 'c'.repeat(64) };
  const groups = planGraphGroups({ plan_graphs: [older, live, newestTerminal] });
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].attempts.map((item) => item.run_id), ['attempt-terminal', 'attempt-live', 'attempt-old']);
  assert.equal(defaultGraphAttempt(groups[0]).run_id, 'attempt-live');
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
