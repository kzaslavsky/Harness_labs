# Parallel PlanGraph operator view

Status: active

The read-only dashboard exposes a PlanGraph's recorded logical identity,
allocation attempts, current concurrency, staging integration facts, recovery
facts, and child liveness. Start it using the existing local dashboard command:

```sh
npm --prefix dashboard/plan-graph run build
python3 scripts/run_dashboard.py --audit-root logs/runs --assets-root dashboard/plan-graph/dist
```

Select an execution attempt from the PlanGraph map. The execution-state panel
shows the durable base and staging head, active allocation count and node IDs,
the active integration-lease record, integration barriers and their evidence
references, attempt lineage, retry decisions, and each recorded logical attempt
with its allocation, immutable parent, and candidate when sealed. Dependencies remain the checkpoint's declared
`depends_on` edges; the dashboard never derives them from timing, Markdown, or
branch history.

## Evidence boundaries

The dashboard reports only facts present in the audited checkpoint. Older
checkpoints may not record a max-parallelism value, active integration lease, or
recovery authority, so those fields are deliberately shown as unavailable
rather than reconstructed. A recovery disposition lists its node, force flag,
and evidence references for inspection only. The dashboard has no mutation
endpoint; allocation, recovery disposition, protected-ref advancement, and
integration remain controller-owned actions.

Graph-controller liveness is not child liveness. A running node is shown as
`liveness_unavailable` until a matching FeatureRun is discovered through full
descriptor correlation; then it shows that child FeatureRun's liveness. This
ephemeral liveness signal never establishes a sealed candidate or integration.
