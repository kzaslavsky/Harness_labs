const catalogProtocol = 'harness-run-catalog-snapshot/1';
const runStatuses = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked', 'interrupted', 'corrupt', 'unknown']);
const nodeStatuses = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked']);
const livenessStates = new Set(['live', 'stale', 'remote_unverified', 'liveness_unavailable', 'terminal', 'not_applicable']);
const availabilityStates = new Set(['available', 'partial', 'unavailable']);

function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function isText(value) { return typeof value === 'string' && value.length > 0; }
function validAvailability(value) { return isObject(value) && availabilityStates.has(value.state) && (value.reason === null || typeof value.reason === 'string'); }
function validLiveness(value) { return isObject(value) && livenessStates.has(value.state) && (value.reason === null || typeof value.reason === 'string'); }
function validMetrics(value) {
  const breakdowns = ['by_phase', 'by_agent', 'by_agent_type', 'by_model', 'by_effort', 'by_backend'];
  return isObject(value) && value.protocol === 'harness-run-detail-metrics/1'
    && isObject(value.totals) && isObject(value.quality) && isObject(value.provenance)
    && breakdowns.every((key) => Array.isArray(value[key]));
}

function validFeatureRun(value) {
  return isObject(value) && isText(value.run_id) && ['feature_run', 'legacy_feature_run'].includes(value.kind)
    && runStatuses.has(value.status) && validLiveness(value.liveness) && validAvailability(value.evidence)
    && (value.correlation === null || isObject(value.correlation));
}

function validNode(value) {
  return isObject(value) && isText(value.node_id) && nodeStatuses.has(value.status)
    && (value.feature_run_id === null || isText(value.feature_run_id))
    && Array.isArray(value.depends_on) && value.depends_on.every(isText)
    && validLiveness(value.liveness) && validAvailability(value.evidence);
}

function validGraph(value) {
  return isObject(value) && isText(value.run_id) && runStatuses.has(value.status)
    && isText(value.created_at) && isText(value.plan_path) && isText(value.plan_digest) && isText(value.plan_graph_digest)
    && validLiveness(value.liveness) && validAvailability(value.evidence)
    && Array.isArray(value.nodes) && value.nodes.every(validNode);
}

export function validateCatalog(value) {
  if (!isObject(value) || value.protocol !== catalogProtocol || !isText(value.revision)
      || !Array.isArray(value.plan_graphs) || !Array.isArray(value.feature_runs)
      || !Array.isArray(value.ungrouped_feature_runs) || !validAvailability(value.availability)
      || (value.source_roots !== undefined && (!Array.isArray(value.source_roots) || !value.source_roots.every(isText)))) {
    throw new Error('The dashboard received an invalid catalog response.');
  }
  if (!value.plan_graphs.every(validGraph) || !value.feature_runs.every(validFeatureRun) || !value.ungrouped_feature_runs.every(validFeatureRun)) {
    throw new Error('The dashboard received an invalid catalog record.');
  }
  return value;
}

export function validateRunDetail(value) {
  const arrayFamilies = ['lifecycle', 'evidence_metadata', 'git_custody'];
  const recordFamilies = ['criteria', 'tasks', 'findings', 'decisions'];
  const availabilityFamilies = ['lifecycle', 'criteria', 'tasks', 'findings', 'evidence_metadata', 'git_custody', 'usage'];
  if (!isObject(value) || !isObject(value.availability) || !isObject(value.timing) || !validMetrics(value.metrics)
      || !arrayFamilies.every((key) => Array.isArray(value[key]))
      || !recordFamilies.every((key) => Array.isArray(value[key]) || isObject(value[key]))
      || !availabilityFamilies.every((key) => validAvailability(value.availability[key]))) {
    throw new Error('The dashboard received an invalid FeatureRun detail response.');
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => (
    recordFamilies.includes(key) && isObject(item) ? [key, Object.values(item)] : [key, item]
  )));
}

export function displayState(record) {
  if (record.evidence?.state === 'unavailable') return 'unavailable';
  if (record.liveness?.state === 'stale') return 'stale';
  if (record.liveness?.state === 'remote_unverified' || record.liveness?.state === 'liveness_unavailable') return 'unavailable';
  if (record.liveness?.state === 'terminal') return record.status;
  return record.status;
}

export function stateLabel(record) {
  const state = displayState(record);
  return state === 'unavailable' ? 'Evidence unavailable' : state.replace(/(^|_)([a-z])/g, (_, prefix, letter) => `${prefix}${letter.toUpperCase()}`);
}

export function planGraphGroups(catalog) {
  const groups = new Map();
  for (const graph of catalog.plan_graphs) {
    const key = graph.plan_digest;
    const group = groups.get(key) || { key, planPath: graph.plan_path, planDigest: graph.plan_digest, attempts: [] };
    group.attempts.push(graph);
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    group.attempts.sort((left, right) => right.created_at.localeCompare(left.created_at) || right.run_id.localeCompare(left.run_id));
  }
  return [...groups.values()].sort((left, right) => right.attempts[0].created_at.localeCompare(left.attempts[0].created_at));
}

export function defaultGraphAttempt(group) {
  if (!group) return null;
  return group.attempts.find((graph) => graph.liveness.state === 'live')
    || group.attempts.find((graph) => graph.status === 'running' && graph.liveness.state !== 'terminal')
    || group.attempts[0]
    || null;
}

function graphDepths(graph) {
  const byId = new Map(graph.nodes.map((node) => [node.node_id, node]));
  const memo = new Map();
  const visit = (nodeId, active = new Set()) => {
    if (memo.has(nodeId)) return memo.get(nodeId);
    if (active.has(nodeId)) return 0;
    const nextActive = new Set(active).add(nodeId);
    const dependencies = (byId.get(nodeId)?.depends_on || []).filter((dependency) => byId.has(dependency));
    const depth = dependencies.length ? 1 + Math.max(...dependencies.map((dependency) => visit(dependency, nextActive))) : 0;
    memo.set(nodeId, depth);
    return depth;
  };
  graph.nodes.forEach((node) => visit(node.node_id));
  return memo;
}

export function graphProjection(catalog, graph) {
  if (!graph) return { nodes: [], edges: [] };
  const runs = new Map(catalog.feature_runs.map((run) => [run.run_id, run]));
  const depths = graphDepths(graph);
  const rows = new Map();
  const nodes = graph.nodes.map((node) => {
    const run = node.feature_run_id ? runs.get(node.feature_run_id) : null;
    const record = run || node;
    const depth = depths.get(node.node_id) || 0;
    const row = rows.get(depth) || 0;
    rows.set(depth, row + 1);
    return {
      id: `${graph.run_id}:${node.node_id}`,
      type: 'featureRun',
      position: { x: 40 + depth * 300, y: 40 + row * 150 },
      data: { graphId: graph.run_id, nodeId: node.node_id, plannedRunId: node.feature_run_id, runId: run?.run_id || null, nodeRecord: node, record, title: run?.run_id || node.node_id },
    };
  });
  const nodeIds = new Set(graph.nodes.map((node) => node.node_id));
  const edges = graph.nodes.flatMap((node) => node.depends_on.filter((dependency) => nodeIds.has(dependency)).map((dependency) => ({
    id: `${graph.run_id}:${dependency}->${node.node_id}`,
    source: `${graph.run_id}:${dependency}`,
    target: `${graph.run_id}:${node.node_id}`,
    animated: node.status === 'running',
  })));
  return { nodes, edges };
}

export function selectedRunFor(catalog, runId) {
  if (!runId || !catalog) return null;
  return catalog.feature_runs.find((run) => run.run_id === runId) || null;
}

export async function fetchCatalog({ etag, signal } = {}) {
  const response = await fetch('/api/catalog', { headers: etag ? { 'If-None-Match': etag } : {}, signal });
  if (response.status === 304) return { unchanged: true, etag };
  if (!response.ok) throw new Error(`Catalog request failed (${response.status}).`);
  return { catalog: validateCatalog(await response.json()), etag: response.headers.get('ETag') || undefined };
}

export async function fetchRunDetail(runId, signal) {
  const response = await fetch(`/api/feature-runs/${encodeURIComponent(runId)}`, { signal });
  if (!response.ok) throw new Error(`FeatureRun detail is unavailable (${response.status}).`);
  return validateRunDetail(await response.json());
}
