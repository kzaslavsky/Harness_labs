export const featureRuns = {
  contract: {
    id: 'FR-01', title: 'Evidence contract', short: 'Contract', status: 'succeeded', phase: 'report',
    objective: 'Define the evidence envelope, validation rules, and immutable provenance fields.',
    criteria: { passed: 4, total: 4 }, duration: '18m 02s', tokens: '604k', cost: '$1.09',
    owner: 'Coordinator', branch: 'codex/fr-01-contract', base: '83e540c', candidate: 'a13f6c2',
    changed: '+188 −12', files: 5, attempts: 1, updated: '1h 24m ago', evidence: 12,
  },
  schema: {
    id: 'FR-02', title: 'Schema admission', short: 'Admission', status: 'succeeded', phase: 'report',
    objective: 'Reject invalid source schemas before execution and bind an immutable admission receipt.',
    criteria: { passed: 3, total: 3 }, duration: '27m 11s', tokens: '918k', cost: '$1.82',
    owner: 'Coordinator', branch: 'codex/fr-02-schema', base: 'a13f6c2', candidate: '9b1e4d0',
    changed: '+301 −27', files: 8, attempts: 2, updated: '57m ago', evidence: 18,
  },
  import: {
    id: 'FR-03', title: 'Import pipeline', short: 'Import', status: 'running', phase: 'review',
    objective: 'Normalize source manifests while preserving cryptographic provenance across ingestion.',
    criteria: { passed: 2, total: 3 }, duration: '32m 18s', tokens: '1.34M', cost: '$2.71',
    owner: 'Coordinator', branch: 'codex/fr-03-import', base: '9b1e4d0', candidate: 'pending',
    changed: '+412 −38', files: 7, attempts: 2, updated: '4s ago', evidence: 21,
  },
  api: {
    id: 'FR-04', title: 'API integration', short: 'API', status: 'succeeded', phase: 'report',
    objective: 'Expose the verified evidence read model through bounded, typed API routes.',
    criteria: { passed: 2, total: 2 }, duration: '14m 44s', tokens: '442k', cost: '$0.76',
    owner: 'Coordinator', branch: 'codex/fr-04-api', base: '9b1e4d0', candidate: '75d03f1',
    changed: '+126 −19', files: 4, attempts: 1, updated: '18m ago', evidence: 9,
  },
  audit: {
    id: 'FR-05', title: 'Audit projection', short: 'Audit', status: 'queued', phase: 'orient',
    objective: 'Project hash-chained audit records into a safe operator evidence inventory.',
    criteria: { passed: 0, total: 2 }, duration: 'Not started', tokens: '—', cost: '—',
    owner: 'Unassigned', branch: 'pending', base: 'FR-03 candidate', candidate: 'pending',
    changed: '—', files: 0, attempts: 0, updated: 'waiting on FR-03', evidence: 0,
  },
  ui: {
    id: 'FR-06', title: 'Provenance UI', short: 'UI', status: 'queued', phase: 'orient',
    objective: 'Make source lineage, integrity, and evidence availability legible to operators.',
    criteria: { passed: 0, total: 2 }, duration: 'Est. 42m', tokens: '—', cost: '—',
    owner: 'Unassigned', branch: 'pending', base: 'FR-03 candidate', candidate: 'pending',
    changed: '—', files: 0, attempts: 0, updated: 'waiting on FR-03', evidence: 0,
  },
  e2e: {
    id: 'FR-07', title: 'End-to-end gates', short: 'E2E', status: 'blocked', phase: 'verify',
    objective: 'Run the approved plan tests against the final sequential candidate lineage.',
    criteria: { passed: 0, total: 1 }, duration: 'Est. 16m', tokens: '—', cost: '—',
    owner: 'Controller', branch: 'pending', base: 'FR-05 + FR-06', candidate: 'pending',
    changed: '—', files: 0, attempts: 0, updated: '2 dependencies', evidence: 0,
  },
  integrate: {
    id: 'FR-08', title: 'Program integration', short: 'Integrate', status: 'blocked', phase: 'integrate',
    objective: 'Verify candidate lineage, execute final gates, and merge the approved program.',
    criteria: { passed: 0, total: 1 }, duration: 'Est. 8m', tokens: '—', cost: '—',
    owner: 'Controller', branch: 'pending', base: 'FR-07 candidate', candidate: 'pending',
    changed: '—', files: 0, attempts: 0, updated: '3 dependencies', evidence: 0,
  },
};

export const graphNodes = [
  { id: 'contract', type: 'featureRun', position: { x: 0, y: 150 }, data: featureRuns.contract },
  { id: 'schema', type: 'featureRun', position: { x: 300, y: 150 }, data: featureRuns.schema },
  { id: 'import', type: 'featureRun', position: { x: 600, y: 40 }, data: featureRuns.import },
  { id: 'api', type: 'featureRun', position: { x: 600, y: 280 }, data: featureRuns.api },
  { id: 'audit', type: 'featureRun', position: { x: 920, y: 0 }, data: featureRuns.audit },
  { id: 'ui', type: 'featureRun', position: { x: 920, y: 220 }, data: featureRuns.ui },
  { id: 'e2e', type: 'featureRun', position: { x: 1230, y: 110 }, data: featureRuns.e2e },
  { id: 'integrate', type: 'featureRun', position: { x: 1530, y: 110 }, data: featureRuns.integrate },
];

const edge = (id, source, target, kind = 'queued') => ({
  id, source, target, type: 'smoothstep', animated: kind === 'active',
  className: `flow-edge flow-edge--${kind}`,
});

export const graphEdges = [
  edge('e1', 'contract', 'schema', 'complete'),
  edge('e2', 'schema', 'import', 'complete'),
  edge('e3', 'schema', 'api', 'complete'),
  edge('e4', 'import', 'audit', 'active'),
  edge('e5', 'import', 'ui', 'active'),
  edge('e6', 'api', 'ui', 'complete'),
  edge('e7', 'audit', 'e2e'),
  edge('e8', 'ui', 'e2e'),
  edge('e9', 'e2e', 'integrate'),
];

export const lifecycle = ['orient', 'plan', 'implement', 'verify', 'review', 'integrate', 'report'];

export const activity = {
  import: [
    ['14:32:11', 'review.finding', 'R4-PROV-UI promoted as required', 'warning'],
    ['14:31:48', 'verify.passed', '186 tests · 0 failures · 14.2s', 'success'],
    ['14:31:30', 'task.completed', 'implementation-worker receipt verified', 'neutral'],
    ['14:30:52', 'artifact.bound', 'workspace-change-receipt/2', 'neutral'],
  ],
};
