import test from 'node:test';
import assert from 'node:assert/strict';
import { displayState, graphProjection, selectedRunFor, stateLabel, validateCatalog, validateRunDetail } from './api.js';

const availability = { state: 'available', reason: null };
const liveness = (state) => ({ state, reason: null });
const feature = { run_id: 'run-1', kind: 'feature_run', status: 'running', liveness: liveness('live'), evidence: availability, correlation: null };
const metrics = { protocol: 'harness-run-detail-metrics/1', totals: {}, quality: {}, provenance: {}, by_phase: [], by_agent: [], by_agent_type: [], by_model: [], by_effort: [], by_backend: [] };

test('runtime validation accepts a catalog with a correlated graph node', () => {
  const catalog = { protocol: 'harness-run-catalog-snapshot/1', revision: 'rev', generated_at: '2026-08-09T00:00:00Z', source_root: '/audit', source_roots: ['/audit', '/other/audit'], availability, diagnostics: [], feature_runs: [feature], ungrouped_feature_runs: [], plan_graphs: [{ run_id: 'graph-1', status: 'running', liveness: liveness('live'), evidence: availability, nodes: [{ node_id: 'node-1', status: 'running', feature_run_id: 'run-1', liveness: liveness('live'), evidence: availability }] }] };
  assert.equal(validateCatalog(catalog), catalog);
  assert.deepEqual(graphProjection(catalog).map((node) => node.data.runId), ['run-1']);
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
