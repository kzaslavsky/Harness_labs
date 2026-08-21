import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fetchSnapshotDocument, fetchSnapshots, shouldPoll } from './api.js';
import { duration, title } from './format.js';
import { buildComparisonRow, filterMetricsComplete } from './snapshots.js';
import ComparisonTable from './components/ComparisonTable.jsx';
import GraphTotals from './components/GraphTotals.jsx';
import NodeMetricsTable from './components/NodeMetricsTable.jsx';
import SnapshotBrowser from './components/SnapshotBrowser.jsx';

const POLL_MILLISECONDS = 2_000;

function OutcomeSummary({ snapshot }) {
  const outcome = snapshot.outcome;
  const delta = outcome.delta;
  return (
    <section className="outcome-summary" aria-label="Outcome summary">
      <h3>Outcome</h3>
      <p className="narrative">{outcome.narrative}</p>
      <div className="metric-cards">
        <div><span>Nodes attempted</span><strong>{outcome.nodes_attempted}</strong><small>of {outcome.nodes_total} total</small></div>
        <div><span>Succeeded</span><strong>{outcome.nodes_succeeded}</strong></div>
        <div><span>Blocked</span><strong>{outcome.nodes_blocked}</strong></div>
        <div><span>Failed</span><strong>{outcome.nodes_failed}</strong></div>
        <div><span>Change delta</span><strong>{delta.state === 'available' ? `${delta.files_changed} file${delta.files_changed === 1 ? '' : 's'}` : 'Unavailable'}</strong><small>{delta.state === 'available' ? `+${delta.insertions} / -${delta.deletions}` : delta.reason}</small></div>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Node</th><th>Status</th><th>Criteria</th><th>Evidence</th></tr></thead>
          <tbody>
            {outcome.nodes.map((node) => (
              <tr key={node.node_id || node.objective}>
                <td title={node.node_id || ''}>{node.objective || node.node_id || 'Unavailable'}</td>
                <td>{title(node.status)}</td>
                <td>{node.criteria_state === 'available' ? `${node.criteria_satisfied}/${node.criteria_total}` : 'Unavailable'}</td>
                <td>{node.evidence_reason || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function SnapshotDetail({ entry, snapshot }) {
  if (!entry) return <p className="muted">Select a snapshot to inspect it.</p>;
  if (entry.snapshot_missing) {
    return <p className="muted">No metrics snapshot has been written for {entry.display_name || entry.run_id}. {entry.reason}</p>;
  }
  if (snapshot === undefined) return <p className="loading">Loading snapshot…</p>;
  if (snapshot === null) return <p className="error">The snapshot document for {entry.display_name || entry.run_id} could not be loaded.</p>;
  const nodeObjectives = Object.fromEntries(snapshot.feature_runs.filter((run) => run.objective).map((run) => [run.node_id, run.objective]));
  return (
    <div className="completed-detail" aria-label={`${snapshot.identity.run_id} snapshot detail`}>
      <header className="completed-detail-header">
        <h2>{snapshot.display_name}</h2>
        <p>{title(snapshot.status)} · finished {snapshot.timing.finished_at || 'unknown'} · wall {duration(snapshot.timing.wall_clock_ms.value)}</p>
      </header>
      <GraphTotals metrics={snapshot.graph_metrics} />
      <NodeMetricsTable nodes={snapshot.graph_metrics.nodes} nodeObjectives={nodeObjectives} />
      <OutcomeSummary snapshot={snapshot} />
    </div>
  );
}

/**
 * Completed-PlanGraph viewer (plan DM-06): browses every snapshot and
 * `snapshot_missing` stub the `/api/snapshots` listing serves, renders a
 * selected snapshot through the same GraphTotals/NodeMetricsTable
 * components the live view uses (against the snapshot document alone --
 * AC-DM06-1), and offers a grouped, sortable comparison mode over every
 * snapshot (AC-DM06-2).
 */
export default function CompletedView() {
  const [listing, setListing] = useState(null);
  const [error, setError] = useState();
  const etagRef = useRef();
  const [selectedId, setSelectedId] = useState(null);
  const [docs, setDocs] = useState({});
  const docsRef = useRef({});
  const [mode, setMode] = useState('browse');
  const [metricsCompleteOnly, setMetricsCompleteOnly] = useState(true);

  const refresh = useCallback(async (signal) => {
    try {
      const result = await fetchSnapshots({ etag: etagRef.current, signal });
      if (result.listing) { etagRef.current = result.etag; setListing(result.listing); }
      setError(undefined);
    } catch (reason) {
      if (reason.name !== 'AbortError') setError(reason.message);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer;
    // First fetch runs even while hidden (headless/embedded viewers report
    // 'hidden' permanently); only repeat polls are visibility-gated.
    let fetchedOnce = false;
    const poll = async () => {
      if (shouldPoll(document.visibilityState, fetchedOnce)) {
        fetchedOnce = true;
        await refresh(controller.signal);
      }
      if (active) timer = window.setTimeout(poll, POLL_MILLISECONDS);
    };
    poll();
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [refresh]);

  const entries = listing?.snapshots || [];
  useEffect(() => {
    if (!selectedId && entries.length) setSelectedId(entries[0].run_id);
  }, [entries, selectedId]);

  const fetchDoc = useCallback(async (runId, signal) => {
    if (docsRef.current[runId] !== undefined) return docsRef.current[runId];
    try {
      const snapshot = await fetchSnapshotDocument(runId, signal);
      docsRef.current = { ...docsRef.current, [runId]: snapshot };
      setDocs(docsRef.current);
      return snapshot;
    } catch (reason) {
      if (reason.name === 'AbortError') return undefined;
      docsRef.current = { ...docsRef.current, [runId]: null };
      setDocs(docsRef.current);
      return null;
    }
  }, []);

  useEffect(() => {
    const entry = entries.find((item) => item.run_id === selectedId);
    if (!entry || entry.snapshot_missing) return undefined;
    const controller = new AbortController();
    fetchDoc(selectedId, controller.signal);
    return () => controller.abort();
  }, [selectedId, entries, fetchDoc]);

  // The left rail's outcome narrative (plan:332-334) and Compare mode's
  // metric columns both need every entry's full document, not just the
  // selected one, so this fetch-all effect runs regardless of `mode`.
  useEffect(() => {
    const controller = new AbortController();
    const unresolved = entries.filter((entry) => !entry.snapshot_missing && docsRef.current[entry.run_id] === undefined);
    Promise.all(unresolved.map((entry) => fetchDoc(entry.run_id, controller.signal)));
    return () => controller.abort();
  }, [entries, fetchDoc]);

  const selectedEntry = entries.find((entry) => entry.run_id === selectedId) || null;
  const selectedDoc = selectedId ? docs[selectedId] : undefined;

  const allComparisonRows = useMemo(() => entries.map((entry) => buildComparisonRow(entry, entry.snapshot_missing ? null : docs[entry.run_id])), [entries, docs]);
  const filtered = useMemo(() => (metricsCompleteOnly ? filterMetricsComplete(allComparisonRows) : { visible: allComparisonRows, hiddenCount: 0 }), [allComparisonRows, metricsCompleteOnly]);
  const compareLoading = mode === 'compare' && entries.some((entry) => !entry.snapshot_missing && docs[entry.run_id] === undefined);

  return (
    <div className="completed-view">
      {error && <p className="error" role="alert">{error}</p>}
      {!listing && !error && <p className="loading">Loading snapshots…</p>}
      {listing && (
        <>
          <div className="completed-toolbar">
            <button type="button" className={mode === 'browse' ? 'active' : ''} onClick={() => setMode('browse')}>Browse</button>
            <button type="button" className={mode === 'compare' ? 'active' : ''} onClick={() => setMode('compare')}>Compare</button>
          </div>
          {listing.diagnostics.length > 0 && <p className="muted">{listing.diagnostics.length} snapshot diagnostic{listing.diagnostics.length === 1 ? '' : 's'}: malformed or oversize snapshot files are excluded from this listing.</p>}
          {mode === 'compare' ? (
            <ComparisonTable
              rows={filtered.visible}
              hiddenCount={filtered.hiddenCount}
              totalCount={allComparisonRows.length}
              metricsCompleteOnly={metricsCompleteOnly}
              onToggleMetricsCompleteOnly={() => setMetricsCompleteOnly((value) => !value)}
              loading={compareLoading}
            />
          ) : (
            <div className="completed-browse">
              <SnapshotBrowser entries={entries} selectedId={selectedId} onSelect={(entry) => setSelectedId(entry.run_id)} docs={docs} />
              <SnapshotDetail entry={selectedEntry} snapshot={selectedDoc} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
