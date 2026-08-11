# Live PlanGraph and FeatureRun dashboard

Status: proposed implementation plan

Date: 2026-08-09

Implementation vehicle: one approved `PlanGraph` executing six dependent
`FeatureRun`s in isolated worktrees. The machine-readable decomposition is
[`live-plangraph-dashboard-decomposition.json`](live-plangraph-dashboard-decomposition.json).

## Goal

Turn the React Flow mockup into a read-only local operations dashboard that:

1. discovers PlanGraphs and FeatureRuns recorded under configured audit roots;
2. distinguishes completed, genuinely live, stale, blocked, failed, queued, and
   evidence-unavailable states without guessing;
3. reconstructs each PlanGraph's dependency graph and candidate lineage;
4. correlates every launched graph node with its production FeatureRun journal;
5. opens an individual FeatureRun inspector for lifecycle, criteria, tasks,
   findings, evidence metadata, Git custody, timing, and usage; and
6. refreshes while controllers are active without granting the dashboard any
   execution or mutation authority.

The initial product is local-only and binds to `127.0.0.1` by default.

## Recorded implementation baseline

- Worktree: `/Users/kirillzaslavsky/Documents/harness_labs-featurerun-dashboard`
- Feature branch: `codex/featurerun-plangraph-dashboard`
- Base branch: `Impl-redo`
- UI prototype commit: `e6cc7b24aa95ecee10669e94dcfd663592d1b4a0`
- PlanGraph implementation: `harness_labs/plan_graph.py`
- FeatureRun audit entrypoint: `harness_labs/feature_run.py`
- Audit storage and verification: `harness_labs/audit.py`
- React Flow prototype: `mockups/featurerun-plangraph-dashboard/`
- Existing metrics tracker candidate: commit `615f374` on branch
  `codex/feature-run-metrics-tracker-r2-20260807t220633z`

Before execution, the integration owner must confirm that the approved plan
commit remains the intended base and update the decomposition if the branch has
advanced.

## Execution record

Implementation runs on child branch
`codex/featurerun-plangraph-dashboard-impl` so the original mockup branch and
its audit evidence remain unchanged. Fresh dispatch attempt
`fr_3f40786290c9445aae418f16d9e707d0` stopped before planner launch because the
repository still tracked a completed checkpoint for an unrelated Initializing
project. That attempt was durably marked blocked; it is not resumed or treated
as implementation evidence.

Bootstrap commit `68b10c55b8dc5423c57a7d633900ac210cb49c5f` removes only that obsolete
checkpoint. The six implementation FeatureRuns start from the descendant plan
commit containing this execution record and retain sequential candidate
lineage on the implementation branch.

## Why this needs runtime work, not only UI wiring

### FeatureRuns are discoverable but “running” is not yet trustworthy

`AuditJournal` creates a canonical directory containing `events.jsonl` and
`checkpoint.json`, and terminal runs add `manifest.json` and `summary.json`.
That is enough to discover historical FeatureRuns and verify terminal evidence.

It is not enough to prove present liveness. A process may disappear after a
checkpoint says `running`; the checkpoint then remains nonterminal forever.
The local audit root observed during planning contains exactly this state for
run `20260808T175032Z-schema-import-step4-comprehension`: a `running`
checkpoint, two events, and no terminal manifest. The dashboard must label that
state `stale` or `liveness unavailable`, never `running`, unless a separate live
lease is verified.

### Prior PlanGraph discovery limitation

Before canonical journaling, `PlanGraph` optionally persisted only:

```json
{"completed": {"node-id": "candidate-commit"}}
```

The path was supplied by `--state`. There was no canonical graph run directory,
graph ID, plan digest, per-node state, start/finish time, child FeatureRun ID,
event journal, or terminal manifest. Existing arbitrary state files therefore
cannot be safely found or correlated by scanning the repository.

New PlanGraphs need durable identity and audit records. Historical PlanGraph
state can be imported only when an operator supplies the matching decomposition
and state file; the implementation must not guess associations from names or
commits.

### Reuse the verified metrics work

Commit `615f374` already contains a deterministic, symlink-safe FeatureRun
metrics projector with terminal validation, verified-prefix handling,
classification separation, availability states, usage attribution, and bounded
diagnostics. The implementation should port the relevant projector and tests
onto the approved dashboard base, then add graph correlation and liveness. It
must not replace that work with a second, weaker audit parser or blindly
cherry-pick unrelated branch history.

## Product boundary

The dashboard is a derived read model. It does not:

- start, pause, retry, cancel, merge, or modify a run;
- open raw prompts, model reasoning, environment dumps, or unrestricted files;
- treat synthetic or fabricated evidence as production lifecycle evidence;
- claim remote-host liveness;
- edit PlanGraph nodes or dependencies;
- discover legacy PlanGraphs without an explicit decomposition/state import; or
- introduce WebSockets, a generalized event bus, or a generalized snapshot
  framework.

The current mockup's pause and mutation-looking controls must be removed or
rendered inert before the UI is described as operational.

## Architecture

```text
PlanGraph controller ─┐
                     ├─> logs/runs/<run-id>/ verified journals
FeatureRun controller ┘          │
                                 ├─> deterministic catalog projector
ephemeral liveness leases ───────┘              │
                                                v
                                      read-only localhost API
                                                │ ETag + polling
                                                v
                                      React Flow dashboard
                                      graph -> run inspector
```

### 1. Run descriptor

Add `schemas/run-descriptor.schema.json` with protocol
`harness-run-descriptor/1`. It is a closed, immutable object containing:

- `run_kind`: `plan_graph` or `feature_run`;
- `run_id` and creation timestamp;
- objective and evidence classification;
- repository path, base branch, and base commit;
- approved plan path and digest where applicable; and
- optional parent correlation:
  `plan_graph_id`, `plan_node_id`, and `parent_run_id`.

Write `descriptor.json` atomically at run creation. Record its SHA-256 in a
`run_descriptor_bound` or `plan_graph_initialized` audit event so a dashboard
can distinguish trusted metadata from an unbound side file. Existing runs with
no descriptor remain discoverable as `legacy_feature_run` and ungrouped.

### 2. PlanGraph durable state

Give each PlanGraph an explicit `graph_run_id` and canonical run directory under
`logs/runs/<graph-run-id>/`. Use `AuditJournal` rather than inventing a second
hash-chain implementation.

The graph checkpoint state must include:

- graph ID, plan path/digest, base and current candidate commits;
- ordered node IDs and immutable dependency/criterion assignments;
- per-node status: `queued`, `running`, `succeeded`, `failed`, or `blocked`;
- correlated FeatureRun ID and run-directory reference when launched;
- node start/finish timestamps and candidate commit when succeeded;
- current node, final functionality-test state, and terminal graph status; and
- revision and audit head inherited from the audit checkpoint.

Emit bounded events for `plan_graph_initialized`, `plan_node_started`,
`plan_node_completed`, `plan_node_failed`, `functionality_test_completed`, and
the terminal graph result. Each transition updates the checkpoint before the
next child can launch.

Extend `FeatureRunRequest` and the subprocess JSON with
`plan_graph_id`, `plan_node_id`, `feature_run_id`, and `run_dir`. The PlanGraph
reserves these identities before launch; the FeatureRun descriptor and first
bound event must echo them. A successful launcher result is rejected if its
identity does not match the reservation.

Do not import legacy sequential state. Registered runs use `--registration`,
`--run-root`, and `--graph-attempt-id`; neither the production runner nor the
`PlanGraph` API accepts a legacy state path. The retired importer emits an
explicit incompatibility error because the old state lacks the evidence needed
for safe registered reuse. Do not scan for arbitrary state JSON.

### 3. Liveness lease

Add an ephemeral `liveness.json` beside the durable journal. It is not audit
evidence and is never used to reconstruct historical outcomes. It contains:

- protocol `harness-controller-liveness/1`;
- run ID, controller instance UUID, hostname, PID, and process-start token;
- monotonic heartbeat sequence and UTC heartbeat timestamp; and
- controller kind (`plan_graph` or `feature_run`).

The owning controller writes it atomically with mode `0600`, refreshes it on a
short heartbeat independent of phase duration, and stops refreshing on exit.
The read model accepts `live` only when all are true:

1. the durable run is nonterminal;
2. the hostname is local;
3. the heartbeat is inside the configured freshness window;
4. the PID exists; and
5. the observed process-start token matches, preventing PID-reuse errors.

Otherwise expose `stale`, `remote_unverified`, or `liveness_unavailable`. Tests
inject the clock and process probe; they do not depend on real sleeps or PIDs.

### 4. Catalog read model

Add `harness_labs/run_catalog.py` and a closed
`schemas/run-catalog-snapshot.schema.json`. Discovery starts from one or more
explicit roots and applies the existing metrics tracker's path-containment,
symlink, hash-chain, manifest, classification, and availability rules to each.
It then merges verified projections, re-evaluates exact cross-root child
correlation, and withholds globally ambiguous run IDs.

The snapshot contains:

- catalog revision, generation time, source roots, and bounded diagnostics;
- PlanGraph summaries and detail records;
- FeatureRun summaries and detail records;
- ungrouped legacy FeatureRuns;
- node/edge projections with durable execution and ephemeral liveness states;
- candidate lineage and child correlation;
- lifecycle, phase boundaries, criteria, task, finding, decision, evidence
  metadata, Git receipt, usage, timing, retry, and cost projections; and
- explicit `available`, `partial`, or `unavailable` state for every evidence
  family where missing data could otherwise look like zero.

Malformed or corrupt runs produce diagnostics and remain isolated; one bad
directory cannot remove valid peers from the catalog.

### 5. Read-only local API

Add `harness_labs/dashboard_server.py` and `scripts/run_dashboard.py`. Use a
small standard-library HTTP server; do not add a production web framework for
this local read-only surface.

Required endpoints:

- `GET /api/health`
- `GET /api/catalog`
- `GET /api/plan-graphs`
- `GET /api/plan-graphs/<graph-id>`
- `GET /api/feature-runs/<run-id>`

The server:

- binds to `127.0.0.1` unless the operator explicitly supplies another host;
- resolves and contains every configured audit root before discovery;
- serves only schema-projected JSON and built dashboard assets;
- never serves raw artifact paths or arbitrary files;
- escapes URL components and rejects ambiguous duplicate run IDs;
- caps diagnostics, response size, file size, and scan depth/count;
- provides `ETag` and honors `If-None-Match`; and
- atomically swaps immutable catalog snapshots so API requests never observe a
  partial rebuild.

The browser polls `/api/catalog` every two seconds while visible. A `304`
response changes nothing. A new revision updates statuses and metrics while
preserving the selected graph, selected run, graph viewport, inspector tab, and
filter state.

### 6. React dashboard integration

Promote the current prototype from
`mockups/featurerun-plangraph-dashboard/` to `dashboard/plan-graph/` only in the
UI FeatureRun. Keep `@xyflow/react` as the graph implementation.

Replace fixture imports with an API client and runtime schema guards. The UI
must provide:

- a PlanGraph list ordered by most recently updated;
- one React Flow canvas with dependency edges and durable node states;
- separate visual treatment for live, stale, queued, blocked, terminal, and
  unavailable states;
- click/keyboard selection of any correlated FeatureRun node;
- inspector tabs for overview, activity, evidence metadata, and Git custody;
- explicit legacy/ungrouped and evidence-availability labels;
- loading, empty, API-disconnected, corrupt-run diagnostic, and stale-run
  states; and
- responsive behavior without losing graph controls or inspector access.

No value may fall back to fictional mock data after the API has loaded. Missing
data renders `Unavailable`, not zero.

## Acceptance criteria

| ID | Criterion |
|---|---|
| AC-01 | Closed JSON schemas distinguish PlanGraphs, FeatureRuns, liveness, and unavailable evidence. |
| AC-02 | Every new PlanGraph writes a canonical audit directory and durable node checkpoint. |
| AC-03 | Every launched child is correlated by `plan_graph_id`, `plan_node_id`, and `feature_run_id`. |
| AC-04 | Discovery projects terminal, active, stale, legacy, corrupt, and ungrouped runs without guessing. |
| AC-05 | A run is labeled live only when its same-host lease is fresh and its process identity matches. |
| AC-06 | One FeatureRun detail projection exposes lifecycle, criteria, tasks, findings, evidence metadata, Git custody, usage, and availability. |
| AC-07 | The local API is read-only, path-contained, deterministic, ETag-enabled, and safe against symlinks and malformed journals. |
| AC-08 | The dashboard renders discovered PlanGraphs and updates the selected FeatureRun inspector from API data. |
| AC-09 | Polling preserves selection and distinguishes running, queued, blocked, stale, terminal, and unavailable states. |
| AC-10 | End-to-end tests prove completed and live discovery, stale-run rejection, PlanGraph correlation, UI inspection, and legacy behavior. |

## FeatureRun decomposition

The implementation is intentionally split into six FeatureRuns. Each run owns
disjoint primary paths, executes its declared checks, produces a candidate
commit, and starts from the exact candidate of its predecessor through
PlanGraph's sequential lineage.

### FR-01 — Catalog contracts

Objective: **Define closed discovery, descriptor, liveness, and dashboard
snapshot contracts.**

- Criteria: AC-01
- Primary writable paths:
  - `schemas/run-descriptor.schema.json`
  - `schemas/controller-liveness.schema.json`
  - `schemas/run-catalog-snapshot.schema.json`
  - `tests/fixtures/run_catalog/`
  - `tests/test_run_catalog_contracts.py`
- Work:
  - define closed schemas and representative terminal, active, stale, corrupt,
    legacy, graph, and correlated-child fixtures;
  - keep ephemeral liveness distinct from durable audit evidence; and
  - validate every fixture against the repository schema checker.
- Verification:
  `python3 -m unittest tests.test_run_catalog_contracts`

### FR-02 — PlanGraph journal and child correlation

Objective: **Make PlanGraph executions durable, identifiable, resumable, and
correlated with child FeatureRuns.**

- Depends on: FR-01
- Criteria: AC-02, AC-03
- Primary writable paths:
  - `harness_labs/plan_graph.py`
  - `harness_labs/plan_graph_audit.py`
  - `harness_labs/controller_liveness.py`
  - `scripts/run_plan_graph.py`
  - `scripts/import_plan_graph_state.py`
  - `tests/test_plan_graph.py`
  - `tests/test_plan_graph_observability.py`
- Work:
  - reserve graph/node/child identities before launch;
  - journal graph transitions and checkpoint complete node state;
  - validate launcher identity and preserve candidate lineage;
  - add PlanGraph heartbeat ownership;
  - keep the legacy state adapter explicit and bounded; and
  - preserve existing backend neutrality and failure-stop behavior.
- Verification:
  `python3 -m unittest tests.test_plan_graph tests.test_plan_graph_observability`

### FR-03 — Verified run catalog

Objective: **Build the verified read model that discovers historical and live
FeatureRuns and PlanGraphs.**

- Depends on: FR-01, FR-02
- Criteria: AC-04, AC-05, AC-06
- Primary writable paths:
  - `harness_labs/run_metrics.py`
  - `harness_labs/run_metrics_index.py`
  - `harness_labs/run_catalog.py`
  - `schemas/run-metrics-record.schema.json`
  - `schemas/run-metrics-index.schema.json`
  - `tests/test_run_metrics.py`
  - `tests/test_run_catalog.py`
- Work:
  - port the verified metrics projector from commit `615f374` by file/diff,
    reconciling it with the approved base;
  - classify run kinds and preserve legacy FeatureRuns as ungrouped;
  - join graph nodes to child descriptors only on exact correlation;
  - add injected-clock/process liveness evaluation; and
  - publish deterministic summary and detail projections with bounded
    diagnostics.
- Verification:
  `python3 -m unittest tests.test_run_metrics tests.test_run_catalog`

### FR-04 — Dashboard API

Objective: **Expose the run catalog through a bounded read-only local dashboard
API.**

- Depends on: FR-03
- Criteria: AC-07
- Primary writable paths:
  - `harness_labs/dashboard_server.py`
  - `scripts/run_dashboard.py`
  - `tests/test_dashboard_api.py`
- Work:
  - implement the five read-only endpoints and immutable snapshot cache;
  - add ETag/304 support;
  - enforce root containment, symlink rejection, duplicate-ID rejection, and
    response limits; and
  - serve only the compiled dashboard and schema-projected data.
- Verification:
  `python3 -m unittest tests.test_dashboard_api`

### FR-05 — React Flow live data integration

Objective: **Connect the React Flow dashboard to real PlanGraph and FeatureRun
data.**

- Depends on: FR-01, FR-04
- Criteria: AC-08, AC-09
- Primary writable paths:
  - `dashboard/plan-graph/`
  - removal of `mockups/featurerun-plangraph-dashboard/` after promotion
- Work:
  - add typed API adapters and runtime response validation;
  - map catalog nodes/edges into React Flow;
  - wire every inspector surface to one FeatureRun detail response;
  - preserve UI state across ETag polling;
  - remove mutation-looking controls; and
  - add semantic component tests for status, selection, refresh, unavailable
    evidence, empty, stale, and disconnected states.
- Verification:
  `npm --prefix dashboard/plan-graph run verify`

### FR-06 — End-to-end certification and operator docs

Objective: **Certify the complete discovery-to-inspection workflow and document
operation and migration.**

- Depends on: FR-02, FR-03, FR-04, FR-05
- Criteria: AC-10
- Primary writable paths:
  - `tests/test_dashboard_e2e.py`
  - `scripts/dashboard_fixture_run.py`
  - `docs/observability/logging-and-metrics.md`
  - `docs/development/live-plangraph-dashboard-operations.md`
  - `docs/development/INDEX.md`
- Work:
  - build a temporary real audit root containing one completed graph, one live
    child, one stale child, one legacy ungrouped FeatureRun, and one malformed
    directory;
  - start the local API, load the production build, select nodes, inspect all
    tabs, advance one fixture checkpoint, and prove polling updates status
    without losing selection;
  - verify no write occurs under the audit root;
  - run the legacy PlanGraph import path; and
  - document startup, root selection, liveness semantics, diagnostics, and
    known exclusions.
- Verification:
  `python3 -m unittest tests.test_dashboard_e2e`

## Required final gates

After FR-06 succeeds, PlanGraph runs these against the final candidate:

1. `python3 -m unittest discover -s tests`
2. `python3 scripts/check_repository_contracts.py`
3. `npm --prefix dashboard/plan-graph run verify`
4. `npm --prefix dashboard/plan-graph run build`
5. the dashboard end-to-end fixture walk

Review must independently challenge:

- false-live classification after controller death or PID reuse;
- forged or mismatched PlanGraph/FeatureRun correlation;
- symlink and path traversal escape;
- incomplete or corrupt journals suppressing valid peers;
- terminal status accepted without manifest/event agreement;
- production/synthetic evidence mixing;
- missing evidence rendered as zero;
- polling resetting graph or inspector state; and
- any dashboard endpoint or control that mutates run state.

No merge is allowed with a critical finding, failed criterion, failed final
gate, ambiguous base, or unverified integration result.

## Recovery and rollout

- Each FeatureRun resumes from its own verified audit checkpoint.
- PlanGraph resumes from the last completed node and exact candidate commit; it
  does not rerun succeeded nodes.
- A dashboard catalog failure never changes source journals. The previous
  immutable snapshot stays available with a visible stale timestamp and
  diagnostic.
- The API ships first as localhost-only and read-only.
- New PlanGraph journaling is enabled at the production runner before the UI is
  allowed to label graphs live.
- Legacy FeatureRuns appear immediately as ungrouped. Legacy PlanGraphs appear
  only after explicit import.
- Remote aggregation, authentication, run mutation, and artifact-content
  viewing require separate plans backed by observed need.

## Multi-root extension record

The observed need was confirmed when an audited Retinology PlanGraph could not
appear in a dashboard configured only for the Harness Labs audit root. The
bounded extension is implemented on
`codex/dashboard-multi-root-discovery`, based on
`ab9d0ad0b8d5b91ae06be3ccf107894cbc48e624`.

Its acceptance contract is:

- `--audit-root` is repeatable and a closed, bounded root registry is supported;
- up to 16 unique local roots are projected independently before aggregation;
- a missing or corrupt root cannot hide healthy peers;
- exact PlanGraph/FeatureRun correlation works across roots;
- duplicate run IDs are withheld rather than resolved by root order;
- detail endpoints read only from the verified record's owning root; and
- the schema, browser UI, operator guide, and end-to-end certification remain
  synchronized.
