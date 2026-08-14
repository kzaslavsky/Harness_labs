import { duration, metricValue, money, title, tokens } from '../format.js';

/**
 * Standalone per-node table over a `harness-plan-graph-metrics/1` document's
 * `nodes[]` rows (plan DM-01/DM-05). Reused verbatim by the completed-snapshot
 * viewer (DM-06). Rows arrive pre-sorted by cost descending (server-side)
 * so the outlier node leads without any client sort. The lead column shows
 * the node's human-readable objective (joined client-side from the catalog,
 * which the metrics document itself does not carry), falling back to the
 * node id only when no objective was recorded.
 */
export default function NodeMetricsTable({ nodes, nodeObjectives = {} }) {
  if (!nodes || !nodes.length) {
    return <section className="metric-section" aria-label="Per-node metrics"><h3>Per-node metrics</h3><p className="muted">No logical nodes are recorded for this graph.</p></section>;
  }
  return (
    <section className="metric-section" aria-label="Per-node metrics">
      <h3>Per-node metrics</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Objective</th><th>Status</th><th>Tries</th><th>Total tokens</th><th>Cost</th><th>Wall time</th><th>Wait</th></tr></thead>
          <tbody>
            {nodes.map((row) => {
              // A row's own `totals.calls` is a plain sum, not a tri-state
              // block, but it doubles as the node-scoped usage-records count:
              // zero calls means no FeatureRun try recorded verified usage,
              // so total_tokens is a summed-empty-set zero, not a real 0.
              const hasUsage = row.totals && row.totals.calls > 0;
              const tokensTitle = row.detail.state === 'unavailable'
                ? row.detail.reason
                : !hasUsage
                  ? 'no FeatureRun try for this node reports verified token usage'
                  : '';
              return (
                <tr key={row.node_id}>
                  <td title={row.node_id}>{nodeObjectives[row.node_id] || row.node_id}</td>
                  <td>{title(row.status)}</td>
                  <td>{row.tries > 1 ? `${row.tries} (cumulative)` : row.tries}</td>
                  <td title={tokensTitle}>{hasUsage ? tokens(row.totals.total_tokens) : 'Unavailable'}</td>
                  <td>{row.totals ? money(row.totals.cost) : 'Unavailable'}</td>
                  <td>{row.totals ? duration(row.totals.wall_clock_ms) : 'Unavailable'}</td>
                  <td title={row.wait_ms.reason || ''}>{metricValue(row.wait_ms, duration)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
