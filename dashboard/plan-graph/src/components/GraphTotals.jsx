import { distributionSummary, duration, ledgerValue, metricValue, money, tokenBlockValue, tokens, usd } from '../format.js';

function Metric({ label, value, reason, hint }) {
  return <div><span>{label}</span><strong>{value}</strong><small>{reason || hint || ''}</small></div>;
}

/**
 * Standalone PlanGraph totals panel over one `harness-plan-graph-metrics/1`
 * document (plan DM-01 / DM-04). Reused verbatim by the completed-snapshot
 * viewer (DM-06) against a snapshot's embedded `graph_metrics`, so every
 * prop here is drawn only from that document plus a client-derived elapsed
 * time for still-live graphs.
 */
export default function GraphTotals({ metrics, elapsedMs = null }) {
  if (!metrics) {
    return <section className="graph-totals" aria-label="PlanGraph totals"><h3>PlanGraph totals</h3><p className="loading">Loading PlanGraph metrics…</p></section>;
  }
  if (metrics.error) {
    return <section className="graph-totals" aria-label="PlanGraph totals"><h3>PlanGraph totals</h3><p className="error">PlanGraph metrics are unavailable: {metrics.error.reason}</p></section>;
  }
  const { totals, retries, recovery, blockers, counts, per_feature_run: perFeatureRun, scheduling, cache, lineage_totals: lineage, timing } = metrics;
  const wallAvailable = timing.wall_clock_ms.state === 'available';
  const wallValue = wallAvailable ? duration(timing.wall_clock_ms.value) : elapsedMs === null || elapsedMs === undefined ? 'Unavailable' : duration(elapsedMs);
  const wallReason = wallAvailable ? timing.wall_clock_ms.reason : elapsedMs === null ? timing.wall_clock_ms.reason : 'elapsed since started_at, derived client-side';
  const blockerReason = blockers.count ? blockers.nodes.map((node) => `${node.node_id}: ${node.reason}`).join('; ') : 'no blocked nodes';
  return (
    <section className="graph-totals" aria-label="PlanGraph totals">
      <h3>PlanGraph totals</h3>
      <div className="metric-cards">
        <Metric label="Total tokens" value={tokenBlockValue(totals.tokens, 'total_tokens')} reason={totals.tokens.reason} hint={`${tokenBlockValue(totals.tokens, 'input_tokens')} in · ${tokenBlockValue(totals.tokens, 'output_tokens')} out · ${tokenBlockValue(totals.tokens, 'cached_input_tokens')} cached`} />
        <Metric label={totals.cost.state === 'estimated' ? 'Est. API cost (estimated)' : 'Est. API cost'} value={money(totals.cost)} reason={totals.cost.reason} />
        <Metric label="Backend calls" value={metricValue(totals.calls, tokens)} reason={totals.calls.reason} />
        <Metric label="Peak observed input" value={metricValue(totals.peak_input_tokens, tokens)} reason={totals.peak_input_tokens.reason} />
        <Metric label="Agent-busy time" value={metricValue(totals.agent_busy_ms, duration)} reason={totals.agent_busy_ms.reason} hint="sum of per-FeatureRun busy time; can exceed wall time under parallel dispatch" />
        <Metric label="Parallelism achieved" value={metricValue(totals.parallelism, (value) => `${value.toFixed(2)}×`)} reason={totals.parallelism.reason} hint="≥1 means concurrent FeatureRuns" />
        <Metric label={wallAvailable ? 'Wall time' : 'Elapsed'} value={wallValue} reason={wallReason} />
        <Metric label="Cache savings" value={metricValue(cache.savings_usd, usd)} reason={cache.savings_usd.reason} />
        <Metric label="Critical path" value={metricValue(scheduling.critical_path_ms, duration)} reason={scheduling.critical_path_ms.reason} />
      </div>
      <div className="metric-cards">
        <Metric label="Logical nodes" value={tokens(counts.logical_nodes)} />
        <Metric label="FeatureRun tries" value={tokens(counts.feature_run_tries)} />
        <Metric label="Node retries (cumulative)" value={tokens(retries.node_retries)} hint="tries beyond the first, per logical node" />
        <Metric label="Graph attempts" value={tokens(retries.graph_attempts)} hint="attempt lineage length" />
        <Metric label="Blockers" value={tokens(blockers.count)} reason={blockerReason} />
      </div>
      <div className="metric-cards">
        <Metric label="Budget: graph launches" value={ledgerValue(retries.budget_ledger, 'graph_launches')} reason={retries.budget_ledger.reason} />
        <Metric label="Budget: gate invocations" value={ledgerValue(retries.budget_ledger, 'gate_invocations')} reason={retries.budget_ledger.reason} />
        <Metric label="Budget: repair dispatches" value={ledgerValue(retries.budget_ledger, 'repair_dispatches')} reason={retries.budget_ledger.reason} />
        <Metric label="Budget: structural decisions" value={ledgerValue(retries.budget_ledger, 'structural_decisions')} reason={retries.budget_ledger.reason} />
        <Metric label="Recovery dispositions" value={tokens(recovery.dispositions.length)} />
        <Metric label="Attempt lineage" value={tokens(recovery.attempt_lineage_count)} />
        <Metric label="Retry invalidations" value={tokens(recovery.invalidations_count)} />
      </div>
      <div className="metric-cards">
        <Metric label="Wall time per FeatureRun" value={distributionSummary(perFeatureRun.wall_ms, duration)} reason={perFeatureRun.wall_ms.reason} hint="per logical node, cumulative across tries" />
        <Metric label="Tokens per FeatureRun" value={distributionSummary(perFeatureRun.tokens, tokens)} reason={perFeatureRun.tokens.reason} hint="per logical node, cumulative across tries" />
        <Metric label="Cost per FeatureRun" value={distributionSummary(perFeatureRun.cost_usd, usd)} reason={perFeatureRun.cost_usd.reason} hint="per logical node, cumulative across tries" />
      </div>
      <p className="metric-note">Campaign totals ({lineage.reason}): {tokenBlockValue(lineage.tokens, 'total_tokens')} tokens · {money(lineage.cost)}.</p>
    </section>
  );
}
