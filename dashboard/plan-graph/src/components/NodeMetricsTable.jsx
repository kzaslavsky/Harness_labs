import { duration, metricValue, money, title, tokens } from '../format.js';

/**
 * Standalone per-node table over a `harness-plan-graph-metrics/1` document's
 * `nodes[]` rows (plan DM-01/DM-05). Reused verbatim by the completed-snapshot
 * viewer (DM-06). Rows arrive pre-sorted by cost descending (server-side)
 * so the outlier node leads without any client sort.
 *
 * Each row carries two scopes, never mixed: "This attempt" (the graph's own
 * usage — these cells sum to the graph totals) and "Cumulative" (the node's
 * usage across its RECORDED predecessor chain, the same figure the
 * FeatureRun inspector reports). A node reused from a prior attempt shows
 * an em-dash in the attempt cells (hover explains why) and its true
 * ancestors' usage under Cumulative. Cumulative wall time is intentionally
 * absent: wall clocks are not additive across attempts.
 */
export default function NodeMetricsTable({ nodes, nodeObjectives = {} }) {
  if (!nodes || !nodes.length) {
    return <section className="metric-section" aria-label="Per-node metrics"><h3>Per-node metrics</h3><p className="muted">No logical nodes are recorded for this graph.</p></section>;
  }
  const attemptCell = (row, render) => {
    if (!row.totals) return <td title={row.detail.reason || ''} className="cell-missing">—</td>;
    return <td>{render(row.totals)}</td>;
  };
  const cumulativeCell = (row, render) => {
    const totals = row.cumulative && row.cumulative.totals;
    if (!totals) return <td title={row.cumulative?.reason || ''} className="cell-missing">—</td>;
    return <td title={row.cumulative.reason || ''}>{render(totals)}</td>;
  };
  return (
    <section className="metric-section" aria-label="Per-node metrics">
      <h3>Per-node metrics</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th rowSpan={2}>Objective</th>
              <th rowSpan={2}>Status</th>
              <th colSpan={4}>This attempt</th>
              <th colSpan={3}>Cumulative (predecessor chain)</th>
              <th rowSpan={2}>Wait</th>
            </tr>
            <tr>
              <th>Tries</th><th>Tokens</th><th>Cost</th><th>Wall</th>
              <th>Tries</th><th>Tokens</th><th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((row) => (
              <tr key={row.node_id}>
                <td title={row.node_id}>{nodeObjectives[row.node_id] || row.node_id}</td>
                <td>{title(row.status)}</td>
                <td>{row.totals ? row.tries : <span title={row.detail.reason || ''}>—</span>}</td>
                {attemptCell(row, (totals) => tokens(totals.total_tokens))}
                {attemptCell(row, (totals) => money(totals.cost))}
                {attemptCell(row, (totals) => duration(totals.wall_clock_ms))}
                <td title={row.cumulative?.reason || ''}>{row.cumulative && row.cumulative.totals ? `${row.cumulative.tries}${row.cumulative.attempts > 1 ? ` in ${row.cumulative.attempts} attempts` : ''}` : '—'}</td>
                {cumulativeCell(row, (totals) => tokens(totals.total_tokens))}
                {cumulativeCell(row, (totals) => money(totals.cost))}
                <td title={row.wait_ms.reason || ''}>{metricValue(row.wait_ms, duration)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
