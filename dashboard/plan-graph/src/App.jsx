import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider } from '@xyflow/react';
import { displayState, fetchCatalog, fetchRunDetail, graphProjection, selectedRunFor, stateLabel } from './api.js';

const POLL_MILLISECONDS = 2_000;
const number = new Intl.NumberFormat('en-US');
const title = (value) => String(value || 'Unavailable').replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
const tokens = (value) => value === null || value === undefined ? 'Unavailable' : number.format(value);
const duration = (milliseconds) => {
  if (milliseconds === null || milliseconds === undefined) return 'Unavailable';
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;
  const hours = Math.floor(milliseconds / 3_600_000); const minutes = Math.floor((milliseconds % 3_600_000) / 60_000); const seconds = Math.floor((milliseconds % 60_000) / 1_000);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds}s`;
};
const money = (cost) => cost?.state === 'available' ? `$${Number(cost.usd).toFixed(4)}` : cost?.state === 'estimated' ? `≈$${Number(cost.usd).toFixed(4)}` : 'Unavailable';
const compactId = (value) => { const parts = String(value || '').split('/'); return parts.length > 1 ? parts.slice(-3).join(' / ') : value; };
function Status({ record }) { const state = displayState(record); return <span className={`status status--${state}`}><i />{stateLabel(record)}</span>; }
function Availability({ value, label }) { return <p className={value?.state === 'available' ? 'availability' : 'availability availability--missing'}><strong>{label}</strong> {value?.state || 'unavailable'}{value?.reason ? ` — ${value.reason}` : ''}</p>; }

function FlowNode({ data, selected }) {
  return <div className={`flow-node ${selected ? 'is-selected' : ''}`}><Handle type="target" position={Position.Left} />
    <Status record={data.record} /><strong>{data.title}</strong><small>{data.graphId} · {data.nodeId}</small>
    {!data.runId && <em>FeatureRun correlation unavailable</em>}<Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { featureRun: FlowNode };

function Detail({ run, detail, loading, error, onClose }) {
  const [tab, setTab] = useState('overview');
  if (!run) return <aside className="inspector empty"><h2>Select a FeatureRun</h2><p>Select a correlated PlanGraph node to inspect verified detail.</p></aside>;
  return <aside className="inspector" aria-label={`${run.run_id} FeatureRun details`}><header><div><Status record={run} /><code>{run.run_id}</code></div><button onClick={onClose} aria-label="Close inspector">×</button></header><h2>{detail?.descriptor?.objective || run.run_id}</h2><Availability label="Catalog evidence:" value={run.evidence} />
    {loading && <p>Loading verified FeatureRun detail…</p>}{error && <p className="error">{error}</p>}
    {detail && <div className="details">
      <nav className="detail-tabs" aria-label="FeatureRun detail tabs">{['overview', 'activity', 'metrics', 'evidence', 'git custody'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}</button>)}</nav>
      {tab === 'overview' && <><section><h3>Acceptance criteria</h3><Availability label="Availability:" value={detail.availability.criteria} /><ReadableList values={detail.criteria} empty="Criteria were not recorded." /></section><section><h3>Tasks</h3><Availability label="Availability:" value={detail.availability.tasks} /><ReadableList values={detail.tasks} empty="Tasks were not recorded." /></section><section><h3>Findings</h3><Availability label="Availability:" value={detail.availability.findings} /><ReadableList values={detail.findings} empty="Findings were not recorded." /></section></>}
      {tab === 'activity' && <Activity events={detail.lifecycle} availability={detail.availability.lifecycle} />}
      {tab === 'metrics' && <Metrics metrics={detail.metrics} />}
      {tab === 'evidence' && <><section><h3>Evidence artifacts</h3><Availability label="Availability:" value={detail.availability.evidence_metadata} /><ReadableList values={detail.evidence_metadata} empty="Evidence metadata is unavailable." /></section><section><h3>Run timing</h3><Definition values={{ Started: detail.timing.started_at, Updated: detail.timing.updated_at }} /></section></>}
      {tab === 'git custody' && <section><h3>Git custody</h3><Availability label="Availability:" value={detail.availability.git_custody} /><ReadableList values={detail.git_custody} empty="Git custody events were not recorded." /></section>}
    </div>}</aside>;
}

function Definition({ values }) {
  return <dl className="definition">{Object.entries(values).filter(([, value]) => value !== undefined && value !== null && value !== '').map(([key, value]) => <div key={key}><dt>{title(key)}</dt><dd>{typeof value === 'boolean' ? (value ? 'Yes' : 'No') : Array.isArray(value) ? value.join(', ') : String(value)}</dd></div>)}</dl>;
}

function ReadableList({ values, empty }) {
  if (!values.length) return <p className="muted">{empty}</p>;
  return <div className="readable-list">{values.slice(0, 20).map((value, index) => {
    if (typeof value !== 'object' || value === null) return <article key={index}><p>{String(value)}</p></article>;
    const heading = value.id || value.key || value.name || value.title || value.path || value.operation || `Record ${index + 1}`;
    const description = value.statement || value.description || value.summary || value.objective || value.reason || value.message;
    const skip = new Set(['id', 'key', 'name', 'title', 'path', 'operation', 'statement', 'description', 'summary', 'objective', 'reason', 'message']);
    const fields = Object.fromEntries(Object.entries(value).filter(([key, item]) => !skip.has(key) && (typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean' || Array.isArray(item))).slice(0, 8));
    return <article key={value.id || value.key || index}><header><strong>{heading}</strong>{value.status && <span className={`pill pill--${String(value.status).toLowerCase()}`}>{title(value.status)}</span>}{value.severity && <span className="pill">{title(value.severity)}</span>}</header>{description && <p>{description}</p>}<Definition values={fields} /></article>;
  })}</div>;
}

function Activity({ events, availability }) {
  return <section><h3>Lifecycle</h3><Availability label="Availability:" value={availability} /><ol className="timeline">{events.slice(-30).reverse().map((event, index) => <li key={`${event.sequence || index}-${event.event_type}`}><i /><div><header><strong>{title(event.event_type || 'event')}</strong><span className={`pill pill--${event.status || 'unknown'}`}>{title(event.status || 'recorded')}</span></header><p>{event.timestamp || event.at || 'Timestamp unavailable'} · {event.actor?.role ? title(event.actor.role) : 'Actor unavailable'}</p>{event.attempt_id && <small>{compactId(event.attempt_id)}</small>}</div></li>)}</ol></section>;
}

function MetricCards({ metrics }) {
  const total = metrics.totals; const quality = metrics.quality;
  return <div className="metric-cards">
    <div><span>Total tokens</span><strong>{tokens(total.total_tokens)}</strong><small>{tokens(total.input_tokens)} in · {tokens(total.output_tokens)} out</small></div>
    <div><span>Peak observed input</span><strong>{tokens(total.peak_input_tokens)}</strong><small>single agent invocation</small></div>
    <div><span>Agent time</span><strong>{duration(total.duration_ms)}</strong><small>{number.format(total.calls)} backend call{total.calls === 1 ? '' : 's'}</small></div>
    <div><span>Wall time</span><strong>{duration(total.wall_clock_ms)}</strong><small>run elapsed time</small></div>
    <div><span>{total.cost.state === 'available' ? 'Recorded API cost' : 'Estimated API cost'}</span><strong>{money(total.cost)}</strong><small>{total.cost.reason || 'audited usage pricing'}</small></div>
    <div><span>Quality</span><strong>{quality.criteria_satisfied}/{quality.criteria_total} criteria</strong><small>{quality.open_findings} open findings · {quality.review_cycles} review cycles · {quality.verification_repairs} repairs</small></div>
  </div>;
}

function MetricsTable({ heading, rows, agents = false }) {
  return <section className="metric-section"><h3>{heading}</h3>{rows.length ? <div className="table-wrap"><table><thead><tr><th>{agents ? 'Agent' : heading.replace('By ', '')}</th>{agents && <><th>Phase</th><th>Model / effort</th><th>Backend</th></>}<th>Calls</th><th>Total tokens</th><th>Peak input</th><th>Agent time</th><th>Cost</th></tr></thead><tbody>{rows.map((row) => <tr key={row.label}><td title={row.label}>{agents ? compactId(row.label) : title(row.label)}</td>{agents && <><td>{title(row.phase)}</td><td>{row.model}<small>{row.effort}</small></td><td>{row.backend}</td></>}<td>{number.format(row.calls)}</td><td>{tokens(row.total_tokens)}<small>{tokens(row.input_tokens)} in · {tokens(row.output_tokens)} out</small></td><td>{tokens(row.peak_input_tokens)}</td><td>{duration(row.duration_ms)}</td><td title={row.cost.reason || ''}>{money(row.cost)}</td></tr>)}</tbody></table></div> : <p className="muted">No audited backend usage records were found.</p>}</section>;
}

function Metrics({ metrics }) {
  if (!metrics) return <section><h3>Metrics</h3><p className="muted">Metrics are unavailable for this run.</p></section>;
  return <div className="metrics"><section><h3>Run metrics</h3><MetricCards metrics={metrics} /><p className="metric-note">Derived from {metrics.provenance.usage_records} {metrics.provenance.collection_method}. Peak context means {metrics.provenance.peak_context_definition}. Cached input is included within input and is not double-counted.</p></section><MetricsTable heading="By phase" rows={metrics.by_phase} /><MetricsTable heading="By agent" rows={metrics.by_agent} agents /><MetricsTable heading="By agent type" rows={metrics.by_agent_type} /><MetricsTable heading="By model" rows={metrics.by_model} /><MetricsTable heading="By effort" rows={metrics.by_effort} /><MetricsTable heading="By backend" rows={metrics.by_backend} /></div>;
}

function Dashboard() {
  const [catalog, setCatalog] = useState(null); const [error, setError] = useState(); const etag = useRef();
  const [selectedRunId, setSelectedRunId] = useState(null); const [detail, setDetail] = useState(null); const [detailError, setDetailError] = useState(); const [detailLoading, setDetailLoading] = useState(false);
  const refresh = useCallback(async (signal) => { try { const result = await fetchCatalog({ etag: etag.current, signal }); if (result.catalog) { etag.current = result.etag; setCatalog(result.catalog); } setError(undefined); } catch (reason) { if (reason.name !== 'AbortError') setError(reason.message); } }, []);
  useEffect(() => { const controller = new AbortController(); const refreshWhileVisible = () => { if (document.visibilityState === 'visible') refresh(controller.signal); }; refreshWhileVisible(); const timer = window.setInterval(refreshWhileVisible, POLL_MILLISECONDS); document.addEventListener('visibilitychange', refreshWhileVisible); return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', refreshWhileVisible); }; }, [refresh]);
  const selectedRun = useMemo(() => selectedRunFor(catalog, selectedRunId), [catalog, selectedRunId]);
  useEffect(() => {
    if (!selectedRunId) { setDetail(null); return undefined; }
    if (!selectedRun) { setDetail(null); setDetailError('The selected FeatureRun is no longer present in the refreshed catalog.'); return undefined; }
    let active = true;
    let requestController;
    const refreshDetail = () => {
      if (document.visibilityState !== 'visible') return;
      requestController?.abort();
      requestController = new AbortController();
      setDetailLoading(true);
      setDetailError(undefined);
      fetchRunDetail(selectedRunId, requestController.signal).then((result) => {
        if (active) setDetail(result);
      }).catch((reason) => {
        if (active && reason.name !== 'AbortError') setDetailError(reason.message);
      }).finally(() => {
        if (active) setDetailLoading(false);
      });
    };
    refreshDetail();
    const timer = window.setInterval(refreshDetail, POLL_MILLISECONDS);
    document.addEventListener('visibilitychange', refreshDetail);
    return () => { active = false; requestController?.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', refreshDetail); };
  }, [selectedRunId, selectedRun]);
  const nodes = useMemo(() => catalog ? graphProjection(catalog) : [], [catalog]);
  const edges = useMemo(() => [], []);
  const onNodeClick = useCallback((_, node) => { if (node.data.runId) setSelectedRunId(node.data.runId); }, []);
  const graphCount = catalog?.plan_graphs.length || 0;
  const rootCount = catalog?.source_roots?.length || (catalog?.source_root ? 1 : 0);
  return <div className="app"><main><header className="top"><div><span className="eyebrow">READ-ONLY OPERATIONS DASHBOARD</span><h1>PlanGraphs</h1><p>{graphCount} discovered PlanGraph{graphCount === 1 ? '' : 's'} across {rootCount} audit root{rootCount === 1 ? '' : 's'} · polling every 2 seconds</p></div><div><span className="readonly">Read-only</span><button onClick={() => refresh()} className="refresh">Refresh</button></div></header>
    {error && <p className="error" role="alert">{error}</p>}{!catalog && !error && <p className="loading">Loading catalog…</p>}
    {catalog && <><Availability label="Catalog:" value={catalog.availability} /><section className="graph"><div className="graph-heading"><div><h2>Execution map</h2><p>Dependencies are unavailable from the current read-only API; nodes are intentionally rendered as disconnected rather than inferred.</p></div><div className="legend">{['running', 'queued', 'blocked', 'stale', 'succeeded', 'unavailable'].map((state) => <span key={state} className={`status status--${state}`}><i />{state}</span>)}</div></div>
      {nodes.length ? <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodeClick={onNodeClick} fitView nodesDraggable={false} nodesConnectable={false} deleteKeyCode={null} proOptions={{ hideAttribution: true }}><Background /><Controls showInteractive={false} /></ReactFlow> : <div className="empty-canvas"><h2>No PlanGraphs discovered</h2><p>The configured audit roots have no verified PlanGraph records.</p></div>}</section>
      <section className="runs"><h2>FeatureRuns</h2>{catalog.feature_runs.length ? catalog.feature_runs.map((run) => <button key={run.run_id} onClick={() => setSelectedRunId(run.run_id)}><code>{run.run_id}</code><Status record={run} /><span>{run.correlation ? `${run.correlation.plan_graph_id} / ${run.correlation.plan_node_id}` : 'Ungrouped or legacy'}</span></button>) : <p className="muted">No FeatureRuns discovered.</p>}</section>
    </>}</main><Detail run={selectedRun} detail={detail} loading={detailLoading} error={detailError} onClose={() => setSelectedRunId(null)} /></div>;
}
export default function App() { return <ReactFlowProvider><Dashboard /></ReactFlowProvider>; }
