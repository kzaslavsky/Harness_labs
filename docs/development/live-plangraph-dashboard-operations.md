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

Malformed run directories are isolated: valid peers stay visible and the
catalog shows a bounded diagnostic. A corrupt summary does not make the raw
run directory downloadable or its detail endpoint available.

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

Legacy PlanGraph state is never inferred by scanning arbitrary JSON. Import a
single graph only when the matching approved decomposition and legacy state
file are both supplied:

```sh
python3 scripts/import_plan_graph_state.py \
  docs/development/live-plangraph-dashboard-decomposition.json \
  path/to/legacy-state.json \
  --run-root logs/runs \
  --graph-run-id imported-legacy-graph
```

The importer validates the decomposition and legacy dependency lineage before
writing a canonical graph journal. It does not launch FeatureRuns. Retain the
original state file as source evidence according to the applicable retention
policy.

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
