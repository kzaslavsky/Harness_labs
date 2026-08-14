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
    && (value.correlation === null || isObject(value.correlation))
    && (value.display_name === undefined || isText(value.display_name))
    && (value.objective === undefined || nullableText(value.objective));
}

function validNodeCorrelation(value) {
  if (value === undefined || value === null) return true;
  return isObject(value) && value.state === 'reused'
    && isText(value.origin_attempt_id) && isText(value.origin_feature_run_id)
    && isText(value.reused_from_attempt) && isText(value.reason);
}

function validNode(value) {
  return isObject(value) && isText(value.node_id) && nodeStatuses.has(value.status)
    && (value.feature_run_id === null || isText(value.feature_run_id))
    && (value.reused_from_attempt === undefined || nullableText(value.reused_from_attempt))
    && (value.candidate_commit === undefined || nullableText(value.candidate_commit))
    && validNodeCorrelation(value.correlation)
    && Array.isArray(value.depends_on) && value.depends_on.every(isText)
    && validLiveness(value.liveness) && validAvailability(value.evidence)
    && (value.objective === undefined || nullableText(value.objective));
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
  const validBlockEscalation = (item) => isObject(item) && hasOnly(item, ['escalated', 'blocker_evidence_ref', 'stable_path'])
    && typeof item.escalated === 'boolean' && nullableText(item.blocker_evidence_ref) && nullableText(item.stable_path);
  return isObject(value) && hasOnly(value, ['logical_graph', 'attempts', 'concurrency', 'integration', 'recovery', 'block_escalation'])
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
    && Array.isArray(value.recovery.retry_state.reuse) && value.recovery.retry_state.reuse.every(validReuse)
    && (value.block_escalation === undefined || validBlockEscalation(value.block_escalation));
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
    && (value.execution === undefined || validGraphExecution(value.execution))
    && (value.display_name === undefined || isText(value.display_name));
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

const graphMetricsProtocol = 'harness-plan-graph-metrics/1';
const genericMetricStates = new Set(['available', 'partial', 'unavailable']);
const tokenBlockStates = new Set(['available', 'partial', 'unavailable']);
const costBlockStates = new Set(['available', 'estimated', 'unavailable']);
const ledgerBlockStates = new Set(['available', 'unavailable']);
const distributionStates = new Set(['available', 'partial', 'unavailable', 'estimated']);

function nullableNumber(value) { return value === null || typeof value === 'number'; }
function validGenericMetric(value) { return isObject(value) && hasOnly(value, ['state', 'value', 'reason']) && genericMetricStates.has(value.state) && nullableNumber(value.value) && nullableText(value.reason); }
function validTokenBlock(value) { return isObject(value) && hasOnly(value, ['state', 'reason', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens']) && tokenBlockStates.has(value.state) && nullableText(value.reason) && nullableInteger(value.input_tokens) && nullableInteger(value.cached_input_tokens) && nullableInteger(value.output_tokens) && nullableInteger(value.total_tokens); }
function validCostBlock(value) { return isObject(value) && hasOnly(value, ['state', 'usd', 'reason']) && costBlockStates.has(value.state) && nullableNumber(value.usd) && nullableText(value.reason); }
function validLedgerBlock(value) { return isObject(value) && hasOnly(value, ['state', 'reason', 'graph_launches', 'gate_invocations', 'repair_dispatches', 'structural_decisions']) && ledgerBlockStates.has(value.state) && nullableText(value.reason) && nullableInteger(value.graph_launches) && nullableInteger(value.gate_invocations) && nullableInteger(value.repair_dispatches) && nullableInteger(value.structural_decisions); }
function validDistribution(value) { return isObject(value) && hasOnly(value, ['state', 'reason', 'mean', 'median', 'max', 'sample_size', 'population']) && distributionStates.has(value.state) && nullableText(value.reason) && nullableNumber(value.mean) && nullableNumber(value.median) && nullableNumber(value.max) && Number.isInteger(value.sample_size) && Number.isInteger(value.population); }
function validNodeTableRow(value) {
  return isObject(value) && hasOnly(value, ['node_id', 'status', 'tries', 'detail', 'totals', 'wait_ms'])
    && isText(value.node_id) && nullableText(value.status) && Number.isInteger(value.tries)
    && isObject(value.detail) && hasOnly(value.detail, ['state', 'reason']) && ['available', 'unavailable'].includes(value.detail.state) && nullableText(value.detail.reason)
    && (value.totals === null || isObject(value.totals))
    && validGenericMetric(value.wait_ms);
}

export function validateGraphMetrics(value) {
  if (!isObject(value) || value.protocol !== graphMetricsProtocol || !isText(value.run_id)) {
    throw new Error('The dashboard received an invalid PlanGraph metrics response.');
  }
  if (value.error !== undefined) {
    if (!hasOnly(value, ['protocol', 'run_id', 'status', 'error']) || !validAvailability(value.error)) {
      throw new Error('The dashboard received an invalid PlanGraph metrics error document.');
    }
    return value;
  }
  const valid = hasOnly(value, ['protocol', 'run_id', 'status', 'timing', 'totals', 'retries', 'recovery', 'blockers', 'counts', 'per_feature_run', 'nodes', 'scheduling', 'cache', 'lineage_totals'])
    && isObject(value.timing) && hasOnly(value.timing, ['started_at', 'wall_clock_ms']) && nullableText(value.timing.started_at) && validGenericMetric(value.timing.wall_clock_ms)
    && isObject(value.totals) && hasOnly(value.totals, ['tokens', 'cost', 'calls', 'agent_busy_ms', 'parallelism', 'peak_input_tokens'])
    && validTokenBlock(value.totals.tokens) && validCostBlock(value.totals.cost) && validGenericMetric(value.totals.calls) && validGenericMetric(value.totals.agent_busy_ms) && validGenericMetric(value.totals.parallelism) && validGenericMetric(value.totals.peak_input_tokens)
    && isObject(value.retries) && hasOnly(value.retries, ['budget_ledger', 'node_retries', 'graph_attempts']) && validLedgerBlock(value.retries.budget_ledger) && Number.isInteger(value.retries.node_retries) && Number.isInteger(value.retries.graph_attempts)
    && isObject(value.recovery) && hasOnly(value.recovery, ['dispositions', 'attempt_lineage_count', 'invalidations_count']) && Array.isArray(value.recovery.dispositions) && Number.isInteger(value.recovery.attempt_lineage_count) && Number.isInteger(value.recovery.invalidations_count)
    && isObject(value.blockers) && hasOnly(value.blockers, ['count', 'nodes']) && Number.isInteger(value.blockers.count) && Array.isArray(value.blockers.nodes)
    && isObject(value.counts) && hasOnly(value.counts, ['logical_nodes', 'feature_run_tries']) && Number.isInteger(value.counts.logical_nodes) && Number.isInteger(value.counts.feature_run_tries)
    && isObject(value.per_feature_run) && hasOnly(value.per_feature_run, ['wall_ms', 'tokens', 'cost_usd']) && validDistribution(value.per_feature_run.wall_ms) && validDistribution(value.per_feature_run.tokens) && validDistribution(value.per_feature_run.cost_usd)
    && Array.isArray(value.nodes) && value.nodes.every(validNodeTableRow)
    && isObject(value.scheduling) && hasOnly(value.scheduling, ['critical_path_ms']) && validGenericMetric(value.scheduling.critical_path_ms)
    && isObject(value.cache) && hasOnly(value.cache, ['savings_usd']) && validGenericMetric(value.cache.savings_usd)
    && isObject(value.lineage_totals) && hasOnly(value.lineage_totals, ['tokens', 'cost', 'calls', 'agent_busy_ms', 'peak_input_tokens', 'reason']) && validTokenBlock(value.lineage_totals.tokens) && validCostBlock(value.lineage_totals.cost) && validGenericMetric(value.lineage_totals.calls) && validGenericMetric(value.lineage_totals.agent_busy_ms) && validGenericMetric(value.lineage_totals.peak_input_tokens) && isText(value.lineage_totals.reason);
  if (!valid) {
    throw new Error('The dashboard received an invalid PlanGraph metrics record.');
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
    // The newest attempt's display_name represents the group in the plan
    // selector; per-attempt names (with their "(Attempt N)" suffix, when
    // present) are shown separately in the attempt selector.
    group.displayName = group.attempts[0].display_name || group.planPath;
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
    const reused = !run && node.correlation?.state === 'reused' ? node.correlation : null;
    const originRun = reused ? runs.get(reused.origin_feature_run_id) : null;
    const record = run || originRun || node;
    const depth = depths.get(node.node_id) || 0;
    const row = rows.get(depth) || 0;
    rows.set(depth, row + 1);
    return {
      id: `${graph.run_id}:${node.node_id}`,
      type: 'featureRun',
      position: { x: 40 + depth * 300, y: 40 + row * 150 },
      data: { graphId: graph.run_id, nodeId: node.node_id, plannedRunId: node.feature_run_id, runId: run?.run_id || originRun?.run_id || null, reused, nodeRecord: node, record, title: node.node_id },
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

// ---------------------------------------------------------------------------
// Completed-PlanGraph snapshots (plan DM-06): `GET /api/snapshots` (bounded
// per-request listing, own ETag) and `GET /api/snapshots/<id>` (one full
// document). Validation mirrors validateCatalog/validateGraphMetrics above
// -- tri-state blocks are checked with the same helpers so a malformed
// response is rejected before it reaches a component.
// ---------------------------------------------------------------------------

const snapshotsListingProtocol = 'harness-dashboard-snapshots-listing/1';
const snapshotProtocol = 'plangraph-metrics-snapshot/1';
const completenessGrades = new Set(['complete', 'partial', 'minimal']);

function validSnapshotEntry(value) {
  return isObject(value) && isText(value.run_id)
    && nullableText(value.logical_graph_id) && nullableText(value.graph_attempt_id)
    && nullableText(value.display_name) && nullableText(value.status) && nullableText(value.finished_at)
    && (value.wall_clock_ms === null || validGenericMetric(value.wall_clock_ms))
    && (value.tokens === null || validTokenBlock(value.tokens))
    && (value.cost === null || validCostBlock(value.cost))
    && (value.completeness === null || completenessGrades.has(value.completeness))
    && typeof value.snapshot_missing === 'boolean' && nullableText(value.reason) && nullableText(value.source_root);
}

export function validateSnapshotsListing(value) {
  if (!isObject(value) || value.protocol !== snapshotsListingProtocol || !isObject(value.bounds)
      || !Array.isArray(value.snapshots) || !Array.isArray(value.diagnostics) || !value.snapshots.every(validSnapshotEntry)) {
    throw new Error('The dashboard received an invalid snapshots listing.');
  }
  return value;
}

function validSnapshotIdentity(value) {
  return isObject(value) && hasOnly(value, ['logical_graph_id', 'graph_attempt_id', 'run_id', 'plan_path', 'plan_digest', 'base_commit', 'repository_id'])
    && nullableText(value.logical_graph_id) && nullableText(value.graph_attempt_id) && isText(value.run_id)
    && nullableText(value.plan_path) && nullableText(value.plan_digest) && nullableText(value.base_commit) && nullableText(value.repository_id);
}

function validSnapshotTiming(value) {
  return isObject(value) && hasOnly(value, ['started_at', 'finished_at', 'wall_clock_ms'])
    && nullableText(value.started_at) && nullableText(value.finished_at) && validGenericMetric(value.wall_clock_ms);
}

function validFeatureRunRow(value) {
  return isObject(value) && hasOnly(value, ['node_id', 'objective', 'display_name', 'status', 'feature_run_id', 'tries', 'detail', 'metrics'])
    && nullableText(value.node_id) && nullableText(value.objective) && nullableText(value.display_name) && nullableText(value.status)
    && nullableText(value.feature_run_id) && Number.isInteger(value.tries)
    && isObject(value.detail) && hasOnly(value.detail, ['state', 'reason']) && ['available', 'unavailable'].includes(value.detail.state) && nullableText(value.detail.reason)
    && (value.metrics === null || isObject(value.metrics));
}

function validOutcomeNode(value) {
  return isObject(value) && hasOnly(value, ['node_id', 'objective', 'status', 'criteria_satisfied', 'criteria_total', 'criteria_state', 'evidence_reason'])
    && nullableText(value.node_id) && nullableText(value.objective) && nullableText(value.status)
    && nullableInteger(value.criteria_satisfied) && nullableInteger(value.criteria_total)
    && ['available', 'unavailable'].includes(value.criteria_state) && nullableText(value.evidence_reason);
}

function validDeltaNode(value) {
  return isObject(value) && hasOnly(value, ['node_id', 'candidate_commit']) && nullableText(value.node_id) && nullableText(value.candidate_commit);
}

function validDelta(value) {
  return isObject(value) && hasOnly(value, ['state', 'reason', 'base_commit', 'final_integrated_commit', 'files_changed', 'insertions', 'deletions', 'nodes'])
    && ['available', 'unavailable'].includes(value.state) && nullableText(value.reason)
    && nullableText(value.base_commit) && nullableText(value.final_integrated_commit)
    && nullableInteger(value.files_changed) && nullableInteger(value.insertions) && nullableInteger(value.deletions)
    && Array.isArray(value.nodes) && value.nodes.every(validDeltaNode);
}

function validOutcome(value) {
  return isObject(value) && hasOnly(value, ['nodes', 'nodes_total', 'nodes_attempted', 'nodes_succeeded', 'nodes_blocked', 'nodes_failed', 'delta', 'plan_sections', 'acceptance_criteria', 'narrative'])
    && Array.isArray(value.nodes) && value.nodes.every(validOutcomeNode)
    && Number.isInteger(value.nodes_total) && Number.isInteger(value.nodes_attempted) && Number.isInteger(value.nodes_succeeded)
    && Number.isInteger(value.nodes_blocked) && Number.isInteger(value.nodes_failed) && validDelta(value.delta)
    && (value.plan_sections === null || isObject(value.plan_sections)) && (value.acceptance_criteria === null || isObject(value.acceptance_criteria))
    && isText(value.narrative);
}

function validDataQuality(value) {
  return isObject(value) && hasOnly(value, ['summary_missing', 'token_records_missing', 'cost_state', 'busy_unavailable_reason', 'criteria_text_unavailable', 'reconstructed', 'reconstruction_notes', 'completeness'])
    && typeof value.summary_missing === 'boolean' && typeof value.token_records_missing === 'boolean'
    && ['available', 'estimated', 'unavailable'].includes(value.cost_state) && nullableText(value.busy_unavailable_reason)
    && typeof value.criteria_text_unavailable === 'boolean' && typeof value.reconstructed === 'boolean'
    && Array.isArray(value.reconstruction_notes) && value.reconstruction_notes.every(isText)
    && completenessGrades.has(value.completeness);
}

export function validateSnapshotDocument(value) {
  if (!isObject(value) || value.protocol !== snapshotProtocol || !validSnapshotIdentity(value.identity)
      || !isText(value.display_name) || !isText(value.status) || !validSnapshotTiming(value.timing)
      || !Array.isArray(value.feature_runs) || !value.feature_runs.every(validFeatureRunRow)
      || !validOutcome(value.outcome) || !validDataQuality(value.data_quality) || !isObject(value.provenance)) {
    throw new Error('The dashboard received an invalid PlanGraph snapshot document.');
  }
  // graph_metrics shares the exact `harness-plan-graph-metrics/1` shape the
  // live endpoint serves (DM-03's schema mirrors it field-for-field), so the
  // same validator -- and the same GraphTotals/NodeMetricsTable components
  // downstream -- apply to a snapshot's embedded metrics unchanged.
  validateGraphMetrics(value.graph_metrics);
  return value;
}

export async function fetchSnapshots({ etag, signal } = {}) {
  const response = await fetch('/api/snapshots', { headers: etag ? { 'If-None-Match': etag } : {}, signal });
  if (response.status === 304) return { unchanged: true, etag };
  if (!response.ok) throw new Error(`Snapshot listing request failed (${response.status}).`);
  return { listing: validateSnapshotsListing(await response.json()), etag: response.headers.get('ETag') || undefined };
}

export async function fetchSnapshotDocument(snapshotId, signal) {
  const response = await fetch(`/api/snapshots/${encodeURIComponent(snapshotId)}`, { signal });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Snapshot document is unavailable (${response.status}).`);
  return validateSnapshotDocument(await response.json());
}

export async function fetchRunDetail(runId, signal) {
  const response = await fetch(`/api/feature-runs/${encodeURIComponent(runId)}`, { signal });
  if (!response.ok) throw new Error(`FeatureRun detail is unavailable (${response.status}).`);
  return validateRunDetail(await response.json());
}

export async function fetchPlanGraphMetrics(runId, signal) {
  const response = await fetch(`/api/plan-graph-metrics/${encodeURIComponent(runId)}`, { signal });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`PlanGraph metrics are unavailable (${response.status}).`);
  return validateGraphMetrics(await response.json());
}

// Elapsed time is deliberately never served by the metrics endpoint for live
// graphs (plan DM-04); the client derives it from the catalog-served
// `started_at` against the current clock.
export function elapsedMs(startedAt, nowMs = Date.now()) {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return null;
  return Math.max(0, nowMs - started);
}

// "In flight" means not yet terminal (queued/running), matching the
// terminal-status set the graph rollup itself uses -- not the separate,
// lease-derived `liveness` field (a graph legitimately has no liveness
// lease yet still be genuinely in flight).
const _TERMINAL_GRAPH_STATUSES = new Set(['succeeded', 'failed', 'blocked', 'interrupted', 'corrupt']);

export function liveGraphs(catalog) {
  if (!catalog) return [];
  return catalog.plan_graphs
    .filter((graph) => !_TERMINAL_GRAPH_STATUSES.has(graph.status))
    .sort((left, right) => right.created_at.localeCompare(left.created_at) || right.run_id.localeCompare(left.run_id));
}
