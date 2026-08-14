import { title } from '../format.js';

/**
 * Left-rail snapshot browser (plan DM-06, AC-DM06-1; plan:332-334): lists
 * every entry the `/api/snapshots` listing serves, including
 * `snapshot_missing` stubs for terminal graphs that have no snapshot file
 * on disk yet, so an emission hole is visible instead of the graph silently
 * disappearing from the view. Each entry also shows its outcome narrative,
 * read from `docs` (the same per-run-id document map `CompletedView` fetches
 * for detail rendering and Compare mode) once that document has loaded.
 */
function narrativeFor(entry, docs) {
  if (entry.snapshot_missing) return null;
  const doc = docs[entry.run_id];
  if (doc === undefined) return 'Loading outcome narrative…';
  if (doc === null) return 'Outcome narrative unavailable.';
  return doc.outcome?.narrative || 'Outcome narrative unavailable.';
}

export default function SnapshotBrowser({ entries, selectedId, onSelect, docs = {} }) {
  if (!entries.length) {
    return <aside className="snapshot-browser empty" aria-label="Completed PlanGraph snapshots"><h3>Snapshots</h3><p className="muted">No terminal PlanGraphs were discovered under the configured audit roots.</p></aside>;
  }
  return (
    <aside className="snapshot-browser" aria-label="Completed PlanGraph snapshots">
      <h3>Snapshots ({entries.length})</h3>
      <div className="snapshot-list">
        {entries.map((entry) => (
          <button
            key={entry.run_id}
            type="button"
            className={`${entry.run_id === selectedId ? 'active' : ''} ${entry.snapshot_missing ? 'snapshot-missing' : ''}`.trim()}
            aria-pressed={entry.run_id === selectedId}
            onClick={() => onSelect(entry)}
          >
            <strong>{entry.display_name || entry.run_id}</strong>
            <span>{title(entry.status)}</span>
            <span>{entry.finished_at ? new Date(entry.finished_at).toLocaleString() : 'Finished date unavailable'}</span>
            {entry.snapshot_missing ? <em title={entry.reason || ''}>Snapshot missing</em> : <p className="snapshot-narrative">{narrativeFor(entry, docs)}</p>}
          </button>
        ))}
      </div>
    </aside>
  );
}
