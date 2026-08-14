import { useMemo, useState } from 'react';
import { COMPARISON_COLUMNS, groupComparisonRows, isMetricDegraded, sortComparisonRows } from '../snapshots.js';

function Cell({ column, row }) {
  const metric = row[column.key];
  if (isMetricDegraded(metric)) return <td title={metric?.reason || 'unavailable'} className="cell-missing">—</td>;
  return <td>{column.display(row)}</td>;
}

function SortableHeader({ column, sortColumn, sortDirection, onSort }) {
  const active = sortColumn === column.key;
  return (
    <th>
      <button type="button" className={`sort-button${active ? ` sort-button--${sortDirection}` : ''}`} onClick={() => onSort(column.key)} aria-sort={active ? (sortDirection === 'asc' ? 'ascending' : 'descending') : 'none'}>
        {column.label}{active ? (sortDirection === 'asc' ? ' ▲' : ' ▼') : ''}
      </button>
    </th>
  );
}

/**
 * Grouped, sortable cross-graph comparison table (plan DM-06, AC-DM06-2):
 * groups by logical graph by default with expandable per-attempt child
 * rows and a per-attempt toggle; every metric column sorts ascending and
 * descending with degraded/missing values placed last in both directions
 * (`isMetricDegraded`/`sortComparisonRows` in `../snapshots.js`); default
 * sort is `finished_at` descending. Expanded per-attempt child rows follow
 * the same active sort as the group representatives, not a fixed order.
 * Rows are pre-filtered by the caller's metrics-complete toggle; this
 * component only renders the count.
 */
export default function ComparisonTable({ rows, hiddenCount, totalCount, metricsCompleteOnly, onToggleMetricsCompleteOnly, loading }) {
  const [grouping, setGrouping] = useState('grouped');
  const [sortColumn, setSortColumn] = useState('finished_at');
  const [sortDirection, setSortDirection] = useState('desc');
  const [expanded, setExpanded] = useState(() => new Set());

  const handleSort = (key) => {
    if (key === sortColumn) setSortDirection((direction) => (direction === 'asc' ? 'desc' : 'asc'));
    else { setSortColumn(key); setSortDirection('desc'); }
  };
  const toggleExpanded = (key) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const displayRows = useMemo(() => {
    if (grouping === 'attempt') {
      return sortComparisonRows(rows, sortColumn, sortDirection).map((row) => ({ kind: 'attempt', row }));
    }
    const groups = groupComparisonRows(rows);
    const representatives = new Map(groups.map((group) => [group.representative, group]));
    const sortedRepresentatives = sortComparisonRows([...representatives.keys()], sortColumn, sortDirection);
    return sortedRepresentatives.flatMap((representative) => {
      const group = representatives.get(representative);
      const isOpen = expanded.has(group.key);
      const items = [{ kind: 'group', group, isOpen }];
      if (isOpen) items.push(...sortComparisonRows(group.rows, sortColumn, sortDirection).map((row) => ({ kind: 'attempt', row, indented: true })));
      return items;
    });
  }, [rows, grouping, sortColumn, sortDirection, expanded]);

  return (
    <section className="comparison-table" aria-label="PlanGraph comparison">
      <div className="comparison-controls">
        <label className="metrics-complete-toggle">
          <input type="checkbox" checked={metricsCompleteOnly} onChange={onToggleMetricsCompleteOnly} />
          Metrics-complete only
        </label>
        <span className="muted hidden-count">{hiddenCount} of {totalCount} snapshot{totalCount === 1 ? '' : 's'} hidden{metricsCompleteOnly && hiddenCount > 0 ? ' (incomplete metrics)' : ''}</span>
        <div className="comparison-grouping" role="group" aria-label="Comparison grouping">
          <button type="button" className={grouping === 'grouped' ? 'active' : ''} onClick={() => setGrouping('grouped')}>Group by logical graph</button>
          <button type="button" className={grouping === 'attempt' ? 'active' : ''} onClick={() => setGrouping('attempt')}>Per attempt</button>
        </div>
      </div>
      {loading && <p className="loading">Loading comparison metrics…</p>}
      {!rows.length && !loading && <p className="muted">No metrics-complete snapshots are available to compare.</p>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {grouping === 'grouped' && <th>Attempts</th>}
                <th>Graph</th>
                {COMPARISON_COLUMNS.map((column) => <SortableHeader key={column.key} column={column} sortColumn={sortColumn} sortDirection={sortDirection} onSort={handleSort} />)}
              </tr>
            </thead>
            <tbody>
              {displayRows.map((entry) => (entry.kind === 'group' ? (
                <tr key={`group:${entry.group.key}`} className="comparison-group-row">
                  <td>
                    <button type="button" className="expand-toggle" aria-expanded={entry.isOpen} onClick={() => toggleExpanded(entry.group.key)}>
                      {entry.isOpen ? '▾' : '▸'} {entry.group.attemptCount}
                    </button>
                  </td>
                  <td title={entry.group.representative.runId}>{entry.group.displayName}</td>
                  {COMPARISON_COLUMNS.map((column) => <Cell key={column.key} column={column} row={entry.group.representative} />)}
                </tr>
              ) : (
                <tr key={`attempt:${entry.row.runId}`} className={entry.indented ? 'comparison-attempt-row' : ''}>
                  {grouping === 'grouped' && <td />}
                  <td title={entry.row.runId}>{entry.row.displayName}</td>
                  {COMPARISON_COLUMNS.map((column) => <Cell key={column.key} column={column} row={entry.row} />)}
                </tr>
              )))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
