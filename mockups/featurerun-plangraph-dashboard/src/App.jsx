import { useCallback, useMemo, useState } from 'react';
import {
  Background,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getSmoothStepPath,
} from '@xyflow/react';
import { activity, featureRuns, graphEdges, graphNodes, lifecycle } from './data.js';

const statusLabels = {
  succeeded: 'Succeeded',
  running: 'Running',
  ready: 'Ready',
  queued: 'Queued',
  blocked: 'Blocked',
};

function Icon({ name, size = 16 }) {
  const paths = {
    graph: <><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="m8 7 3 8m5-8-3 8M8 6h8"/></>,
    runs: <><path d="M8 5v14l11-7z"/></>,
    check: <><path d="m5 12 4 4L19 6"/></>,
    archive: <><path d="M4 7h16v13H4zM3 3h18v4H3zm6 8h6"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></>,
    pause: <><path d="M8 5v14m8-14v14"/></>,
    branch: <><circle cx="6" cy="5" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10m2-3c6 0 3-7 8-7"/></>,
    file: <><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5"/></>,
    shield: <><path d="M12 3 4 6v6c0 5 3 8 8 10 5-2 8-5 8-10V6z"/><path d="m8 12 3 3 5-6"/></>,
    bolt: <><path d="m13 2-8 12h7l-1 8 8-12h-7z"/></>,
    chevron: <><path d="m9 18 6-6-6-6"/></>,
    close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  };
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function FeatureRunNode({ data, selected }) {
  const progress = data.criteria.total ? (data.criteria.passed / data.criteria.total) * 100 : 0;
  return (
    <div className={`flow-node flow-node--${data.status} ${selected ? 'is-selected' : ''}`}>
      <Handle type="target" position={Position.Left} className="flow-handle" />
      <div className="flow-node__top">
        <span className="flow-node__id">{data.id}</span>
        <span className={`run-status run-status--${data.status}`}><i />{statusLabels[data.status]}</span>
      </div>
      <strong>{data.title}</strong>
      <span className="flow-node__phase">{data.phase}</span>
      <div className="flow-node__progress"><i style={{ width: `${progress}%` }} /></div>
      <div className="flow-node__meta">
        <span>{data.criteria.passed}/{data.criteria.total} criteria</span>
        <span>{data.duration}</span>
      </div>
      <div className="flow-node__footer">
        <code>{data.candidate}</code>
        <span>Inspect <Icon name="chevron" size={12} /></span>
      </div>
      <Handle type="source" position={Position.Right} className="flow-handle" />
    </div>
  );
}

function DependencyEdge(props) {
  const [path] = getSmoothStepPath(props);
  return <BaseEdge path={path} markerEnd={props.markerEnd} className={props.className} />;
}

const nodeTypes = { featureRun: FeatureRunNode };
const edgeTypes = { smoothstep: DependencyEdge };

function Sidebar({ active, setActive, collapsed, setCollapsed }) {
  const items = [
    ['graph', 'graph', 'PlanGraph', '8'],
    ['runs', 'runs', 'FeatureRuns', '8'],
    ['criteria', 'check', 'Criteria & gates', '18'],
    ['evidence', 'archive', 'Evidence', null],
    ['usage', 'clock', 'Usage', null],
  ];
  return (
    <aside className={`sidebar ${collapsed ? 'is-collapsed' : ''}`}>
      <div className="brand-row">
        <div className="brand-mark"><span/><span/><span/></div>
        {!collapsed && <div><strong>Harness Labs</strong><small>Operations</small></div>}
        <button className="quiet-icon collapse-button" onClick={() => setCollapsed(!collapsed)} aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}>‹</button>
      </div>
      <button className="repository-picker">
        <span className="repo-avatar">RL</span>
        {!collapsed && <><span><strong>Retinology</strong><small>retinology-web</small></span><b>⌄</b></>}
      </button>
      <nav aria-label="Dashboard sections">
        {!collapsed && <p>PROGRAM</p>}
        {items.slice(0, 2).map(([id, icon, label, count]) => (
          <button key={id} className={active === id ? 'is-active' : ''} onClick={() => setActive(id)} aria-label={label}>
            <Icon name={icon}/>{!collapsed && <><span>{label}</span>{count && <b>{count}</b>}</>}
          </button>
        ))}
        {!collapsed && <p>ASSURANCE</p>}
        {items.slice(2).map(([id, icon, label, count]) => (
          <button key={id} className={active === id ? 'is-active' : ''} onClick={() => setActive(id)} aria-label={label}>
            <Icon name={icon}/>{!collapsed && <><span>{label}</span>{count && <b>{count}</b>}</>}
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <div className="controller-state"><i/>{!collapsed && <span><strong>Controller online</strong><small>checkpoint 4s ago</small></span>}</div>
        <div className="profile"><span>KZ</span>{!collapsed && <><div><strong>Kirill Z.</strong><small>Owner</small></div><b>•••</b></>}</div>
      </div>
    </aside>
  );
}

function ProgramHeader() {
  return (
    <>
      <header className="topbar">
        <div className="breadcrumb"><span>Programs</span><b>/</b><strong>Retinology v2 evidence import</strong><span className="live-chip"><i/>Running</span></div>
        <div className="top-actions"><span className="sync-state"><i/>Live · 4s</span><button className="icon-button" aria-label="Search"><Icon name="search"/></button><button className="pause-button"><Icon name="pause" size={13}/>Pause</button></div>
      </header>
      <section className="program-heading">
        <div><div className="eyebrow">PLAN GRAPH <code>PG-2026-08-09-017</code></div><h1>Retinology v2 evidence import</h1><p>Ship schema ingestion, provenance UI, and verification against the approved clinical evidence plan.</p></div>
        <div className="program-stats"><div><span>Complete</span><strong>3 / 8</strong><small>runs</small></div><div><span>Criteria</span><strong>11 / 18</strong><small>passed</small></div><div><span>Elapsed</span><strong>1h 42m</strong><small>ETA 34m</small></div></div>
      </section>
    </>
  );
}

function GraphToolbar({ filter, setFilter, query, setQuery }) {
  return (
    <div className="graph-toolbar">
      <div className="graph-title"><div className="eyebrow">EXECUTION MAP</div><h2>PlanGraph</h2><span className="graph-state"><i/>1 active · 4 waiting</span></div>
      <label className="search-control"><Icon name="search" size={14}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find a run" aria-label="Find a run" /></label>
      <div className="filter-control" aria-label="Filter graph">
        {['all', 'running', 'succeeded', 'waiting'].map((item) => <button key={item} onClick={() => setFilter(item)} className={filter === item ? 'is-active' : ''}>{item === 'all' ? 'All' : item === 'succeeded' ? 'Done' : item[0].toUpperCase()+item.slice(1)}</button>)}
      </div>
    </div>
  );
}

function RunOverview({ run }) {
  const phaseIndex = lifecycle.indexOf(run.phase);
  const isComplete = run.status === 'succeeded';
  return (
    <>
      <div className="run-facts">
        <div><span>Owner</span><strong><i className="avatar-dot">CO</i>{run.owner}</strong></div>
        <div><span>Duration</span><strong>{run.duration}</strong></div>
        <div><span>Branch</span><code>{run.branch}</code></div>
        <div><span>Base commit</span><code>{run.base}</code></div>
      </div>
      <section className="inspector-section">
        <div className="section-title"><h3>Lifecycle</h3><span>{isComplete ? '7 of 7 phases' : `${Math.max(phaseIndex, 0)} of 7 phases`}</span></div>
        <div className="lifecycle-list">
          {lifecycle.map((phase, index) => {
            const done = isComplete || index < phaseIndex;
            const current = !isComplete && index === phaseIndex;
            return <div key={phase} className={`lifecycle-row ${done ? 'is-done' : ''} ${current ? 'is-current' : ''}`}><i>{done ? '✓' : ''}</i><span><strong>{phase}</strong><small>{current ? (run.status === 'running' ? 'In progress · 1 finding open' : statusLabels[run.status]) : done ? 'Verified checkpoint' : 'Pending'}</small></span><time>{done ? ['1m','4m','18m','6m','3m','1m','<1m'][index] : '—'}</time></div>;
          })}
        </div>
      </section>
      {run.status === 'running' && <div className="finding-callout"><span>!</span><div><strong>Review gate needs repair</strong><p><code>R4-PROV-UI</code> Imported evidence loses its source digest in the UI projection.</p><button>Inspect finding <Icon name="chevron" size={12}/></button></div></div>}
      <section className="inspector-section criteria-list">
        <div className="section-title"><h3>Acceptance criteria</h3><span>{run.criteria.passed} / {run.criteria.total}</span></div>
        {Array.from({ length: run.criteria.total }, (_, index) => <div key={index} className={index < run.criteria.passed ? 'criterion is-passed' : 'criterion'}><i>{index < run.criteria.passed ? '✓' : index === run.criteria.passed && run.status === 'running' ? '!' : '·'}</i><span><strong>AC-{String(Number(run.id.slice(3))*3 + index + 1).padStart(2,'0')}</strong><small>{index < run.criteria.passed ? 'Deterministic evidence verified' : 'Awaiting required evidence'}</small></span><b>{index < run.criteria.passed ? 'passed' : 'open'}</b></div>)}
      </section>
    </>
  );
}

function RunActivity({ run }) {
  const events = activity[run === featureRuns.import ? 'import' : 'none'] || [
    ['—', 'run.state', `${statusLabels[run.status]} · no active events`, 'neutral'],
  ];
  return <section className="activity-view"><div className="agent-strip"><div><span className="agent-avatar cyan">CO</span><span><strong>Coordinator</strong><small>{run.status === 'running' ? 'Evaluating repair result' : 'No active turn'}</small></span></div><div><span className="agent-avatar purple">RV</span><span><strong>Reviewer</strong><small>{run.status === 'running' ? '1 required finding' : 'Gate complete'}</small></span></div></div><div className="section-title"><h3>Event stream</h3><span>latest first</span></div><div className="event-stream">{events.map(([time, type, message, tone]) => <div key={`${time}-${type}`}><time>{time}</time><span className={`event-tag ${tone}`}>{type}</span><p>{message}</p></div>)}</div></section>;
}

function RunEvidence({ run }) {
  const artifacts = [
    ['review-ledger.json', 'review-ledger/1', '0d28…a612'],
    ['verification-result.json', 'controller-task-result/1', '83cf…9b10'],
    ['workspace-change-receipt.json', 'workspace-change-receipt/2', '5a17…dc02'],
    ['implementation.diff', 'text/x-diff', '70ac…11e8'],
  ];
  if (!run.evidence) return <div className="empty-state"><Icon name="archive" size={24}/><strong>No evidence yet</strong><p>Artifacts appear after the first verified checkpoint.</p></div>;
  return <section className="evidence-view"><div className="integrity-banner"><Icon name="shield"/><span><strong>Hash chain verified</strong><small>{run.evidence} artifacts · event head #1842</small></span></div>{artifacts.map(([name,type,hash]) => <button className="artifact-row" key={name}><span className="artifact-icon"><Icon name="file" size={15}/></span><span><strong>{name}</strong><small>{type}</small></span><code>sha256:{hash}</code><b>✓</b><Icon name="chevron" size={13}/></button>)}</section>;
}

function RunGit({ run }) {
  return <section className="git-view"><div className="custody-step is-complete"><i>✓</i><span><small>Base</small><code>{run.base}</code><b>verified</b></span></div><div className={`custody-step ${run.branch !== 'pending' ? 'is-complete' : ''}`}><i>{run.branch !== 'pending' ? '✓' : '2'}</i><span><small>Isolated worktree</small><code>{run.branch}</code><b>{run.branch !== 'pending' ? 'scope clean' : 'pending'}</b></span></div><div className={`custody-step ${run.candidate !== 'pending' ? 'is-complete' : ''}`}><i>{run.candidate !== 'pending' ? '✓' : '3'}</i><span><small>Candidate</small><code>{run.candidate}</code><b>{run.candidate !== 'pending' ? 'committed' : 'awaiting gates'}</b></span></div><div className="scope-card"><span>Workspace change</span><div><strong>{run.files} files</strong><strong>{run.changed}</strong></div><small>{run.files ? 'All changed paths are within the declared write grant.' : 'No workspace has been created.'}</small></div></section>;
}

function RunInspector({ runKey, onClose }) {
  const run = featureRuns[runKey];
  const [tab, setTab] = useState('overview');
  return (
    <aside className="run-inspector" aria-label={`${run.title} FeatureRun details`}>
      <header className="inspector-header"><div><span className={`run-status run-status--${run.status}`}><i/>{statusLabels[run.status]}</span><code>{run.id}</code></div><button className="icon-button" onClick={onClose} aria-label="Close inspector"><Icon name="close"/></button></header>
      <div className="inspector-title"><h2>{run.title}</h2><p>{run.objective}</p></div>
      <div className="inspector-metrics"><div><span>Criteria</span><strong>{run.criteria.passed}/{run.criteria.total}</strong></div><div><span>Tokens</span><strong>{run.tokens}</strong></div><div><span>Known cost</span><strong>{run.cost}</strong></div><div><span>Attempts</span><strong>{run.attempts}</strong></div></div>
      <nav className="inspector-tabs" aria-label="Run detail views">{['overview','activity','evidence','git'].map(item => <button key={item} className={tab === item ? 'is-active' : ''} onClick={() => setTab(item)}>{item}{item === 'evidence' && run.evidence > 0 && <span>{run.evidence}</span>}</button>)}</nav>
      <div className="inspector-body">
        {tab === 'overview' && <RunOverview run={run}/>} {tab === 'activity' && <RunActivity run={run}/>} {tab === 'evidence' && <RunEvidence run={run}/>} {tab === 'git' && <RunGit run={run}/>} 
      </div>
      <footer className="inspector-footer"><span>Updated {run.updated}</span><button>Open full FeatureRun <Icon name="chevron" size={13}/></button></footer>
    </aside>
  );
}

function GraphWorkspace({ selectedRun, setSelectedRun }) {
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const visibleNodes = useMemo(() => graphNodes.map(node => {
    const waiting = ['ready','queued','blocked'].includes(node.data.status);
    const matchesFilter = filter === 'all' || node.data.status === filter || (filter === 'waiting' && waiting);
    const matchesQuery = !query || `${node.data.id} ${node.data.title}`.toLowerCase().includes(query.toLowerCase());
    return { ...node, hidden: !(matchesFilter && matchesQuery) };
  }), [filter, query]);
  const onNodeClick = useCallback((_, node) => setSelectedRun(node.id), [setSelectedRun]);
  const markerEnd = { type: MarkerType.ArrowClosed, width: 13, height: 13, color: '#4b5665' };
  const edges = useMemo(() => graphEdges.map(edge => ({ ...edge, markerEnd })), []);
  return <section className="graph-workspace"><GraphToolbar filter={filter} setFilter={setFilter} query={query} setQuery={setQuery}/><div className="react-flow-frame"><ReactFlow nodes={visibleNodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodeClick={onNodeClick} fitView fitViewOptions={{ padding: .18, maxZoom: 1 }} minZoom={.35} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} deleteKeyCode={null} proOptions={{ hideAttribution: true }} colorMode="dark"><Background color="#222933" gap={20} size={1}/><Controls position="bottom-left" showInteractive={false}/><MiniMap position="bottom-right" pannable zoomable nodeColor={(node) => ({ succeeded:'#2f9d70',running:'#49bdd0',ready:'#717d8d',queued:'#596473',blocked:'#c58f3c' }[node.data.status])}/><Panel position="top-left" className="canvas-note"><span>Critical path</span><strong>FR-01 → FR-02 → FR-03 → FR-06 → FR-07 → FR-08</strong></Panel></ReactFlow></div><div className="graph-footer"><span><i className="legend-dot succeeded"/>Succeeded</span><span><i className="legend-dot running"/>Running</span><span><i className="legend-dot ready"/>Ready</span><span><i className="legend-dot queued"/>Queued</span><span><i className="legend-dot blocked"/>Blocked</span><b>Drag to pan · Scroll to zoom · Select a node to inspect</b></div>{selectedRun && <div className="mobile-inspector"><RunInspector runKey={selectedRun} onClose={() => setSelectedRun(null)}/></div>}</section>;
}

function RunsView({ setSelectedRun }) {
  return <section className="alternate-view"><div className="alternate-heading"><div><div className="eyebrow">PROGRAM LEDGER</div><h2>FeatureRuns</h2><p>Every run projected from the approved plan, including blocked and incomplete work.</p></div></div><div className="run-table"><div className="run-table__head"><span>Run</span><span>Status</span><span>Phase</span><span>Criteria</span><span>Duration</span><span>Candidate</span><span/></div>{Object.entries(featureRuns).map(([key,run]) => <button key={key} onClick={() => setSelectedRun(key)}><span><strong>{run.id} · {run.title}</strong><small>{run.objective}</small></span><span className={`run-status run-status--${run.status}`}><i/>{statusLabels[run.status]}</span><span>{run.phase}</span><span>{run.criteria.passed} / {run.criteria.total}</span><span>{run.duration}</span><code>{run.candidate}</code><Icon name="chevron" size={13}/></button>)}</div></section>;
}

function PlaceholderView({ active }) {
  return <section className="alternate-view placeholder-view"><Icon name={active === 'criteria' ? 'check' : active === 'evidence' ? 'archive' : 'clock'} size={28}/><h2>{active === 'criteria' ? 'Criteria & gates' : active === 'evidence' ? 'Evidence inventory' : 'Usage & timing'}</h2><p>This navigation destination is represented in the graph and FeatureRun inspector for this bounded mockup.</p></section>;
}

function Dashboard() {
  const [active, setActive] = useState('graph');
  const [selectedRun, setSelectedRun] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  return <div className={`app-shell ${selectedRun && active === 'graph' ? 'has-inspector' : ''} ${collapsed ? 'sidebar-collapsed' : ''}`}><Sidebar active={active} setActive={setActive} collapsed={collapsed} setCollapsed={setCollapsed}/><main><ProgramHeader/><div className="workspace">{active === 'graph' && <GraphWorkspace selectedRun={selectedRun} setSelectedRun={setSelectedRun}/>} {active === 'runs' && <RunsView setSelectedRun={(key) => {setSelectedRun(key);setActive('graph');}}/>} {!['graph','runs'].includes(active) && <PlaceholderView active={active}/>}</div></main>{selectedRun && active === 'graph' && <div className="desktop-inspector"><RunInspector key={selectedRun} runKey={selectedRun} onClose={() => setSelectedRun(null)}/></div>}</div>;
}

export default function App() { return <ReactFlowProvider><Dashboard/></ReactFlowProvider>; }
