import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Background, Controls, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider } from '@xyflow/react';
import { displayState, fetchCatalog, fetchRunDetail, graphProjection, selectedRunFor, stateLabel } from './api.js';

const POLL_MILLISECONDS = 2_000;
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
      <nav className="detail-tabs" aria-label="FeatureRun detail tabs">{['overview', 'activity', 'evidence', 'git custody'].map((name) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name}</button>)}</nav>
      {tab === 'overview' && <><section><h3>Acceptance criteria</h3><Availability label="Availability:" value={detail.availability.criteria} /><List values={detail.criteria} empty="Criteria were not recorded." /></section><section><h3>Tasks and findings</h3><Availability label="Tasks:" value={detail.availability.tasks} /><List values={detail.tasks} empty="Tasks were not recorded." /><Availability label="Findings:" value={detail.availability.findings} /><List values={detail.findings} empty="Findings were not recorded." /></section></>}
      {tab === 'activity' && <section><h3>Lifecycle</h3><Availability label="Availability:" value={detail.availability.lifecycle} /><ol>{detail.lifecycle.slice(-8).reverse().map((event, index) => <li key={`${event.sequence || index}-${event.event_type}`}><code>{event.event_type || 'event'}</code> <span>{event.timestamp || event.at || 'timestamp unavailable'}</span></li>)}</ol></section>}
      {tab === 'evidence' && <><section><h3>Evidence metadata</h3><Availability label="Evidence:" value={detail.availability.evidence_metadata} /><List values={detail.evidence_metadata} empty="Evidence metadata is unavailable." /></section><section><h3>Usage and timing</h3><Availability label="Usage:" value={detail.availability.usage} /><pre>{JSON.stringify({ usage: detail.usage, timing: detail.timing }, null, 2)}</pre></section></>}
      {tab === 'git custody' && <section><h3>Git custody</h3><Availability label="Git:" value={detail.availability.git_custody} /><List values={detail.git_custody} empty="Git custody events were not recorded." /></section>}
    </div>}</aside>;
}
function List({ values, empty }) { return values.length ? <ul>{values.slice(0, 12).map((value, index) => <li key={index}>{typeof value === 'string' ? value : JSON.stringify(value)}</li>)}</ul> : <p className="muted">{empty}</p>; }

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
  return <div className="app"><main><header className="top"><div><span className="eyebrow">READ-ONLY OPERATIONS DASHBOARD</span><h1>PlanGraphs</h1><p>{graphCount} discovered PlanGraph{graphCount === 1 ? '' : 's'} · polling every 2 seconds</p></div><div><span className="readonly">Read-only</span><button onClick={() => refresh()} className="refresh">Refresh</button></div></header>
    {error && <p className="error" role="alert">{error}</p>}{!catalog && !error && <p className="loading">Loading catalog…</p>}
    {catalog && <><Availability label="Catalog:" value={catalog.availability} /><section className="graph"><div className="graph-heading"><div><h2>Execution map</h2><p>Dependencies are unavailable from the current read-only API; nodes are intentionally rendered as disconnected rather than inferred.</p></div><div className="legend">{['running', 'queued', 'blocked', 'stale', 'succeeded', 'unavailable'].map((state) => <span key={state} className={`status status--${state}`}><i />{state}</span>)}</div></div>
      {nodes.length ? <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodeClick={onNodeClick} fitView nodesDraggable={false} nodesConnectable={false} deleteKeyCode={null} proOptions={{ hideAttribution: true }}><Background /><Controls showInteractive={false} /><MiniMap nodeColor={(node) => displayState(node.data.record) === 'running' ? '#4ad5e8' : '#64748b'} /></ReactFlow> : <div className="empty-canvas"><h2>No PlanGraphs discovered</h2><p>The configured audit root has no verified PlanGraph records.</p></div>}</section>
      <section className="runs"><h2>FeatureRuns</h2>{catalog.feature_runs.length ? catalog.feature_runs.map((run) => <button key={run.run_id} onClick={() => setSelectedRunId(run.run_id)}><code>{run.run_id}</code><Status record={run} /><span>{run.correlation ? `${run.correlation.plan_graph_id} / ${run.correlation.plan_node_id}` : 'Ungrouped or legacy'}</span></button>) : <p className="muted">No FeatureRuns discovered.</p>}</section>
    </>}</main><Detail run={selectedRun} detail={detail} loading={detailLoading} error={detailError} onClose={() => setSelectedRunId(null)} /></div>;
}
export default function App() { return <ReactFlowProvider><Dashboard /></ReactFlowProvider>; }
