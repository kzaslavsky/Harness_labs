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

## PG-06 implementation record

The catalog descriptor binder accepts the closed PlanGraph lineage extension
(`logical_graph_id`, `graph_attempt_id`, and `predecessor_attempt_id`) as one
complete field set, while retaining legacy descriptor compatibility. It rejects
unknown, partial, feature-run, and malformed lineage field sets before catalog
or dashboard projection. The bounded change is in
`harness_labs/run_catalog.py`; deterministic discovery coverage is in
`tests/test_run_catalog.py` and `tests/test_dashboard_api.py`.

The catalog projects graph-attempt lineage at the graph level and groups the
dashboard by `logical_graph_id`, so separate graphs sharing an approved-plan
digest are not conflated. It also projects `retention_constraints` as an
explicit unavailable state until a descriptor or checkpoint records a durable
retention policy; evidence references are never assumed retained.

Verified with:

```sh
python3 -m unittest tests.test_run_catalog_contracts tests.test_run_catalog tests.test_dashboard_api
```

Result: 33 tests passed.

## PG-07 certification handoff

Certification proceeds in the recorded dependency order: preserve the frozen
PG-00 contract root at commit `b152bbe6ccef887c1252e3b7d132e2a531be583b`, then
certify the reviewed PG-05B candidate
`c66e196` and reviewed PG-06 candidate `7e348f3`. PG-07 adds no product
behavior and does not alter the controller's full-suite gate:

```sh
python3 -m unittest discover -s tests
```

The controller must run that unchanged command in its socket-permitted
authoritative verification environment. This worker hands off the integrated
candidate and its bounded non-socket evidence; the dashboard E2E gate remains
required and is not weakened or skipped when a restricted worker sandbox cannot
open its loopback server.
