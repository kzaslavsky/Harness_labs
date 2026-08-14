const catalogProtocol = 'harness-run-catalog-snapshot/1';
const runStatuses = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked', 'interrupted', 'corrupt', 'unknown']);
const nodeStatuses = new Set(['queued', 'running', 'succeeded', 'failed', 'blocked']);
const livenessStates = new Set(['live', 'stale', 'remote_unverified', 'liveness_unavailable', 'terminal', 'not_applicable']);
const availabilityStates = new Set(['available', 'partial', 'unavailable']);

function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function isText(value) { return typeof value === 'string' && value.length > 0; }
function validAvailability(value) { return isObject(value) && hasOnly(value, ['state', 'reason']) && availabilityStates.has(value.state) && (value.reason === null || typeof value.reason === 'string'); }
function validLiveness(value) { return isObject(value) && hasOnly(value, ['state', 'reason']) && livenessStates.has(value.state) && (value.reason === null || typeof value.reason === 'string'); }
function hasOnly(value, keys) { return Object.keys(value).every((key) => keys.includes(key)); }
function nullableText(value) { return value === null || isText(value); }
function nullableInteger(value) { return value === null || Number.isInteger(value); }
function validMetrics(value) {
  const breakdowns = ['by_phase', 'by_agent', 'by_agent_type', 'by_model', 'by_effort', 'by_backend'];
  return isObject(value) && value.protocol === 'harness-run-detail-metrics/1'
    && isObject(value.totals) && isObject(value.quality) && isObject(value.provenance)
    && breakdowns.every((key) => Array.isArray(value[key])) && Array.isArray(value.stages);
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

function validGraphExecution(value) {
  const validAttempt = (item) => isObject(item) && hasOnly(item, ['node_id', 'logical_attempt', 'allocation_id', 'checkpoint_revision', 'parent_candidate_commit', 'expected_staging_head', 'status', 'candidate_commit'])
    && isText(item.node_id) && Number.isInteger(item.logical_attempt) && nullableText(item.allocation_id)
    && nullableInteger(item.checkpoint_revision) && nullableText(item.parent_candidate_commit)
    && nullableText(item.expected_staging_head) && isText(item.status) && nullableText(item.candidate_commit);
  const validBarrier = (item) => isObject(item) && hasOnly(item, ['barrier_id', 'node_id', 'attempt_id', 'allocation_id', 'logical_attempt', 'checkpoint_revision', 'lease_id', 'action', 'input_commit', 'expected_staging_head', 'integrated_commit', 'evidence_refs'])
    && nullableText(item.barrier_id) && nullableText(item.node_id) && nullableText(item.attempt_id)
    && nullableText(item.allocation_id) && nullableInteger(item.logical_attempt) && nullableInteger(item.checkpoint_revision)
    && nullableText(item.lease_id) && nullableText(item.action) && nullableText(item.input_commit)
    && nullableText(item.expected_staging_head) && nullableText(item.integrated_commit)
    && Array.isArray(item.evidence_refs) && new Set(item.evidence_refs).size === item.evidence_refs.length && item.evidence_refs.every(isText);
  const validLineage = (item) => isObject(item) && hasOnly(item, ['attempt_id', 'node_id', 'logical_attempt', 'allocation_id', 'input_commit', 'predecessor_attempt_id'])
    && isText(item.attempt_id) && isText(item.node_id) && Number.isInteger(item.logical_attempt)
    && isText(item.allocation_id) && isText(item.input_commit) && nullableText(item.predecessor_attempt_id);
  const validInvalidation = (item) => isObject(item) && hasOnly(item, ['attempt_id', 'node_id', 'allocation_id', 'reason', 'invalidated_at'])
    && isText(item.attempt_id) && isText(item.node_id) && isText(item.allocation_id) && isText(item.reason) && isText(item.invalidated_at);
  const validReuse = (item) => isObject(item) && hasOnly(item, ['node_id', 'reused_from_attempt_id', 'replacement_attempt_id'])
    && isText(item.node_id) && isText(item.reused_from_attempt_id) && isText(item.replacement_attempt_id);
  const validDisposition = (item) => isObject(item) && hasOnly(item, ['node_id', 'disposition', 'reason', 'forced', 'evidence_refs'])
    && isText(item.node_id) && ['blocked', 'sealed'].includes(item.disposition) && (item.reason === null || typeof item.reason === 'string')
    && typeof item.forced === 'boolean' && Array.isArray(item.evidence_refs) && new Set(item.evidence_refs).size === item.evidence_refs.length && item.evidence_refs.every(isText);
  const validLeaseRecord = (item) => item === null || (isObject(item) && hasOnly(item, ['node_id', 'lease_id', 'expected_staging_head'])
    && isText(item.node_id) && isText(item.lease_id) && isText(item.expected_staging_head));
  return isObject(value) && hasOnly(value, ['logical_graph', 'attempts', 'concurrency', 'integration', 'recovery'])
    && isObject(value.logical_graph) && hasOnly(value.logical_graph, ['base_commit', 'plan_digest', 'plan_graph_digest'])
    && nullableText(value.logical_graph.base_commit) && nullableText(value.logical_graph.plan_digest) && nullableText(value.logical_graph.plan_graph_digest)
    && Array.isArray(value.attempts) && value.attempts.every(validAttempt)
    && isObject(value.concurrency) && hasOnly(value.concurrency, ['active_nodes', 'active_count', 'max_parallelism'])
    && Array.isArray(value.concurrency.active_nodes) && new Set(value.concurrency.active_nodes).size === value.concurrency.active_nodes.length && value.concurrency.active_nodes.every(isText) && Number.isInteger(value.concurrency.active_count) && value.concurrency.active_count >= 0 && validAvailability(value.concurrency.max_parallelism)
    && isObject(value.integration) && hasOnly(value.integration, ['staging_head', 'lease', 'lease_record', 'barriers'])
    && nullableText(value.integration.staging_head) && validAvailability(value.integration.lease) && validLeaseRecord(value.integration.lease_record)
    && Array.isArray(value.integration.barriers) && value.integration.barriers.every(validBarrier)
    && isObject(value.recovery) && hasOnly(value.recovery, ['active_allocations', 'authority', 'dispositions', 'attempt_lineage', 'retry_state'])
    && Array.isArray(value.recovery.active_allocations) && value.recovery.active_allocations.every(validAttempt) && validAvailability(value.recovery.authority)
    && Array.isArray(value.recovery.dispositions) && value.recovery.dispositions.every(validDisposition)
    && Array.isArray(value.recovery.attempt_lineage) && value.recovery.attempt_lineage.every(validLineage)
    && isObject(value.recovery.retry_state) && hasOnly(value.recovery.retry_state, ['invalidations', 'reuse'])
    && Array.isArray(value.recovery.retry_state.invalidations) && value.recovery.retry_state.invalidations.every(validInvalidation)
    && Array.isArray(value.recovery.retry_state.reuse) && value.recovery.retry_state.reuse.every(validReuse);
}

function validGraph(value) {
  return isObject(value) && isText(value.run_id) && runStatuses.has(value.status)
    && isText(value.created_at) && isText(value.plan_path) && isText(value.plan_digest) && isText(value.plan_graph_digest)
    && (value.logical_graph_id === undefined || isText(value.logical_graph_id))
    && (value.graph_attempt_id === undefined || isText(value.graph_attempt_id))
    && (value.predecessor_attempt_id === undefined || nullableText(value.predecessor_attempt_id))
    && (value.retention_constraints === undefined || validAvailability(value.retention_constraints))
    && validLiveness(value.liveness) && validAvailability(value.evidence)
    && Array.isArray(value.nodes) && value.nodes.every(validNode)
    && (value.execution === undefined || validGraphExecution(value.execution));
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
  if (record.liveness?.state === 'terminal') return record.status;
  return record.status;
}

export function stateLabel(record) {
  const state = displayState(record);
  return state === 'unavailable' ? 'Evidence unavailable' : state.replace(/(^|_)([a-z])/g, (_, prefix, letter) => `${prefix}${letter.toUpperCase()}`);
}

export function planGraphGroups(catalog) {
  const logicalIdCounts = new Map();
  for (const graph of catalog.plan_graphs) {
    if (graph.logical_graph_id) logicalIdCounts.set(graph.logical_graph_id, (logicalIdCounts.get(graph.logical_graph_id) || 0) + 1);
  }
  const groups = new Map();
  for (const graph of catalog.plan_graphs) {
    const declaredIdentityIsShared = graph.logical_graph_id && logicalIdCounts.get(graph.logical_graph_id) > 1;
    const topology = [...graph.nodes]
      .sort((left, right) => left.node_id.localeCompare(right.node_id))
      .map((node) => [node.node_id, [...node.depends_on].sort()]);
    const baseCommit = graph.execution?.logical_graph?.base_commit || 'base-unavailable';
    const key = declaredIdentityIsShared
      ? `logical:${graph.logical_graph_id}`
      : `retry:${graph.plan_digest}:${baseCommit}:${JSON.stringify(topology)}`;
    const group = groups.get(key) || { key, planPath: graph.plan_path, planDigest: graph.plan_digest, attempts: [] };
    group.attempts.push(graph);
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    group.attempts.sort((left, right) => right.created_at.localeCompare(left.created_at) || right.run_id.localeCompare(left.run_id));
    group.planPath = group.attempts[0].plan_path;
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
      data: { graphId: graph.run_id, nodeId: node.node_id, plannedRunId: node.feature_run_id, runId: run?.run_id || null, nodeRecord: node, record, title: node.node_id },
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
