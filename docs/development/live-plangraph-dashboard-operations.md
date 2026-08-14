# Live PlanGraph dashboard operations

Status: active

The PlanGraph dashboard is a local, read-only view of one or more explicit
audit-root directories. It derives its catalog from verified journals and
never starts, stops, retries, imports, edits, or otherwise changes a controller
run.

## Start the dashboard

Build the UI and start the loopback-only server with each direct parent of run
directories supplied as a repeatable `--audit-root`:

```sh
npm --prefix dashboard/plan-graph run build
python3 scripts/run_dashboard.py \
  --audit-root logs/runs \
  --audit-root /path/to/another/repository/logs/runs \
  --assets-root dashboard/plan-graph/dist
```

For durable configuration, use a closed root registry:

```json
{
  "protocol": "harness-dashboard-audit-root-registry/1",
  "audit_roots": [
    "/path/to/harness_labs/logs/runs",
    "/path/to/Retinology/logs/runs"
  ]
}
```

```sh
python3 scripts/run_dashboard.py \
  --audit-root-registry /path/to/dashboard-audit-roots.json \
  --assets-root dashboard/plan-graph/dist
```

Registry-relative paths resolve from the registry's directory. Direct options
and registry entries may be combined, up to 16 unique roots.

The default bind address is `127.0.0.1:8000`. Supplying a different host is an
operator decision; this dashboard provides neither authentication nor remote
aggregation. Source roots must not be symlinks. A missing root is reported as
unavailable without hiding healthy roots. The API
only exposes the catalog, graph records, FeatureRun detail projections, and
compiled dashboard assets; it does not serve raw journals or artifacts.

The browser polls `/api/catalog` every two seconds while visible and uses the
catalog ETag. A `304 Not Modified` response leaves the current selection and
view unchanged. Use the Refresh control for an immediate catalog request.
Selecting a correlated node on the execution map opens that FeatureRun's
metrics directly; selecting the same run from the FeatureRuns list opens its
overview. A node whose planned FeatureRun is not present in the verified
catalog still opens a node inspector with its recorded status, dependencies,
liveness, and evidence, while marking FeatureRun metrics unavailable.
For an inspectable executor run, the Metrics tab also lists audited coordinator
sessions, worker tasks, deterministic verification stages, backend/model
identity, outcomes, attempts, and recorded durations. Token and cost fields
use normalized backend events when present and otherwise use the final
cumulative totals plus per-turn peaks from hash-verified Codex
`thread/tokenUsage/updated` artifacts. Cumulative updates are not summed, so
polling notifications cannot double-count tokens. Fields remain unavailable
when the executor emitted neither source.

Repeated executions of the same approved plan are grouped by its durable plan
digest; each attempt retains its exact PlanGraph digest. The canvas defaults to the newest live or otherwise running
attempt and retains older attempts in an explicit selector. It renders only the
selected attempt. Node positions and edges are derived from the checkpoint's
recorded `depends_on` relationships; the dashboard does not parse or infer a DAG
from Markdown or Mermaid files.

## Interpreting state and evidence

Durable checkpoints describe lifecycle history; they are not proof that a
controller currently runs. A nonterminal run is labelled `live` only when its
ephemeral `liveness.json` lease is local, fresh, names an existing PID, and
matches that process's start token. A stale heartbeat or a mismatched process
identity is `stale`; a remote lease is `remote_unverified`; an absent or invalid
lease is `liveness_unavailable`.

`liveness.json` is intentionally not durable audit evidence and must not be
used to reconstruct a historical outcome. Terminal state is established by the
verified journal and manifest. Missing lifecycle, criteria, findings, usage,
or artifact metadata is shown as unavailable rather than zero.
For a nonterminal run, the status badge continues to show the audited lifecycle
status while the inspector reports an absent or remote lease separately as
unverified liveness. A failed background detail refresh retains the last
verified detail and retries on the normal polling interval.
Metrics for a FeatureRun correlated from a PlanGraph node are cumulative across
all verified tries of that node under the same approved-plan digest. The detail
view keeps the per-try totals so retries and checkpoint resumes remain
individually auditable and are not double-counted within a try.
Catalog refreshes project only graph and run summaries. Verified FeatureRun
detail and cumulative node history are projected on demand for the selected run
and cached only for that immutable catalog snapshot, so a growing audit root
does not block the initial dashboard render on every run's detail.
After the first verified snapshot, catalog reads use stale-while-revalidate:
one background refresh computes the next snapshot while readers continue to
receive the last verified snapshot. Catalog and detail polling schedule the next
request only after the prior request completes, preventing slow projections
from being repeatedly abandoned or multiplied.
ReactFlow cards retain the concise PlanGraph node ID as their visible title
through every lifecycle state. Full FeatureRun and PlanGraph run IDs remain in
the inspector and metric breakdowns rather than expanding the graph card.
PlanGraph records with a recurring declared logical graph ID are grouped by
that identity. When legacy retry launchers instead assign a fresh self-identity
to every attempt, the dashboard combines attempts only when approved-plan
digest, base commit, and normalized node/dependency topology all match.

Malformed run directories are isolated: valid peers stay visible and the
catalog shows a bounded diagnostic. A corrupt summary does not make the raw
run directory downloadable or its detail endpoint available.
The bounded audit-tree scan permits up to 4,096 entries per run so normal Codex
transport evidence remains inspectable. For active nonterminal runs, the full
event hash chain is verified while checkpoint-derived state is explicitly
reported as partial when newer verified journal events exist.

Run IDs are global API identities. If the same run ID exists in more than one
configured root, every copy is withheld as ambiguous and the catalog emits a
diagnostic naming the conflicting roots. Configuration order never selects a
winner. A PlanGraph and its explicitly correlated child FeatureRuns may reside
in different configured roots; correlation is re-evaluated after aggregation.

## Migration and legacy records

New PlanGraphs create canonical audit directories and descriptors. Existing
FeatureRuns without a descriptor remain visible as ungrouped
`legacy_feature_run` records. This is a discovery compatibility path, not a
claim that their historical metadata or liveness can be reconstructed.

Legacy PlanGraph state is never inferred or imported. The retired
`scripts/import_plan_graph_state.py` exits with an explicit incompatibility
error because sequential-prefix state lacks the immutable registration,
manifest, lineage, verification, and attempt evidence required for safe reuse.
Retain legacy state only as source evidence according to the applicable
retention policy; create a new registered attempt for continued execution.

## Deliberate exclusions

This release does not provide remote hosting, authentication, mutation controls,
artifact-content viewing, prompt or reasoning viewing,
or automatic legacy PlanGraph discovery. It uses polling rather than WebSockets
and exposes no API action that changes an audit root.

## Certification coverage

Run the focused browser certification walk with a local Chrome binary (the
test recognizes common macOS/Linux paths; set `DASHBOARD_E2E_CHROME` for any
other location):

```sh
DASHBOARD_E2E_CHROME=/path/to/chrome python3 -m unittest tests.test_dashboard_e2e
```

It builds two temporary fabricated audit roots with completed, live, stale,
legacy, malformed, and correlated records; serves the compiled build, selects
the live run in the rendered UI, checks its inspector tabs, and confirms a
polling refresh retains that selection while its status becomes terminal. It
also checks cross-root correlation and inspection families, confirms read
requests do not modify either audit root, and exercises the explicit
legacy-import command. The test injects
process identity because test fixtures must not depend on a real controller
PID.
