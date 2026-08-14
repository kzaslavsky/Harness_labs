import { elapsedMs, stateLabel } from '../api.js';
import { duration } from '../format.js';

/**
 * Lists every live PlanGraph (liveness.state === "live") with its display
 * name, status, and client-derived elapsed time; clicking one switches the
 * live view's selection without a reload (plan DM-05, AC-DM05-1).
 */
export default function InFlightStrip({ graphs, selectedRunId, onSelect, nowMs }) {
  if (!graphs.length) return null;
  return (
    <section className="in-flight-strip" aria-label="In-flight PlanGraphs">
      <h3>In flight ({graphs.length})</h3>
      <div className="in-flight-list">
        {graphs.map((graph) => (
          <button
            key={graph.run_id}
            type="button"
            className={graph.run_id === selectedRunId ? 'active' : ''}
            aria-pressed={graph.run_id === selectedRunId}
            onClick={() => onSelect(graph)}
          >
            <strong>{graph.display_name || graph.run_id}</strong>
            <span>{stateLabel(graph)}</span>
            <span>{duration(elapsedMs(graph.created_at, nowMs))}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
