const catalogProtocol = 'harness-run-catalog-snapshot/1';
const runStatuses = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked', 'interrupted', 'corrupt', 'unknown']);
const nodeStatuses = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked']);
const livenessStates = new Set(['live', 'stale', 'remote_unverified', 'liveness_unavailable', 'terminal', 'not_applicable']);
const availabilityStates = new Set(['available', 'partial', 'unavailable']);

function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function isText(value) { return typeof value === 'string' && value.length > 0; }
function validAvailability(value) { return isObject(value) && availabilityStates.has(value.state) && (value.reason === null || typeof value.reason === 'string'); }
function validLiveness(value) { return isObject(value) && livenessStates.has(value.state) && (value.reason === null || typeof value.reason === 'string'); }

function validFeatureRun(value) {
  return isObject(value) && isText(value.run_id) && ['feature_run', 'legacy_feature_run'].includes(value.kind)
    && runStatuses.has(value.status) && validLiveness(value.liveness) && validAvailability(value.evidence)
    && (value.correlation === null || isObject(value.correlation));
}

function validNode(value) {
  return isObject(value) && isText(value.node_id) && nodeStatuses.has(value.status)
    && (value.feature_run_id === null || isText(value.feature_run_id))
    && validLiveness(value.liveness) && validAvailability(value.evidence);
}

function validGraph(value) {
  return isObject(value) && isText(value.run_id) && runStatuses.has(value.status)
    && validLiveness(value.liveness) && validAvailability(value.evidence)
    && Array.isArray(value.nodes) && value.nodes.every(validNode);
}

export function validateCatalog(value) {
  if (!isObject(value) || value.protocol !== catalogProtocol || !isText(value.revision)
      || !Array.isArray(value.plan_graphs) || !Array.isArray(value.feature_runs)
      || !Array.isArray(value.ungrouped_feature_runs) || !validAvailability(value.availability)) {
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
  if (!isObject(value) || !isObject(value.availability) || !isObject(value.timing)
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

export function graphProjection(catalog) {
  const runs = new Map(catalog.feature_runs.map((run) => [run.run_id, run]));
  return catalog.plan_graphs.flatMap((graph, graphIndex) => graph.nodes.map((node, index) => {
    const run = node.feature_run_id ? runs.get(node.feature_run_id) : null;
    const record = run || node;
    return {
      id: `${graph.run_id}:${node.node_id}`,
      type: 'featureRun',
      position: { x: 40 + index * 270, y: 50 + graphIndex * 220 },
      data: { graphId: graph.run_id, nodeId: node.node_id, runId: node.feature_run_id, record, title: run?.run_id || node.node_id },
    };
  }));
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
