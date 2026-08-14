# GraphRun restructure — package layout for a multi-harness lab (GR)

**Program id (if run as PlanGraph):** `graphrun-restructure-1`
**Base:** `main` at `1e9514a` (first published integrated tip).
**Goal:** reshape the flat 44-module `harness_labs/` package into a layered lab
— `core/` substrate, `featurerun/` and `plangraph/` harness layers,
`observability/`, and a thin `graphrun/` composition surface — so future
harnesses land as siblings on `core`, and FeatureRun's standalone-ness becomes
a mechanically enforced contract instead of a stated intention.

## The grounding discovery

The measured import graph (2026-08-14, at `1e9514a`) shows the layering is
already almost clean, with **exactly one real boundary violation**:

- `feature_run` imports `plan_graph_budget` (which imports
  `plan_graph_authority`). The RB retry-budget ledger and `gate_digest` live
  in plangraph-branded modules but are consumed by FeatureRun's
  `_verify_with_recovery` — they are shared recovery substrate, misnamed.
  **FeatureRun today cannot import without pulling `plan_graph_*` modules.**
  This is the program's one genuine red phase.
- `plan_graph` itself does **not** import `feature_run` — composition happens
  in `feature_run`'s plan-graph-bound entry points and the program runners.
- No other upward or cross edges violate the target layering.

## Target layout and complete module mapping

```
harness_labs/
  core/           # substrate any harness reuses
  featurerun/     # single-feature harness (imports core only)
  plangraph/      # graph orchestration (imports core + featurerun)
  observability/  # metrics/catalog/dashboard (imports core only)
  graphrun/       # thin composition + operator surface
  <legacy>.py     # deprecation shims re-exporting from new homes (one cycle)
```

| New home | Modules (from flat `harness_labs/*.py`) |
|---|---|
| `core/` | `attempts`, `audit`, `usage`, `git_transaction`, `text_executor`, `backends`, `composition`, `agent_sessions`, `claude_agent_session`, `codex_agent_session`, `omlx_agent_session`, `claude_task_executor`, `model_capability_executor`, `codex_delegation`, `capability_broker`, `controller_commands`, `controller_evidence`, `controller_results`, `controller_kernel`, `controller_live`, `controller_live_scenarios`, `controller_projection`, `controller_scheduler`, `controller_coordinator`, `controller_run`, `coordinator_dispatcher`, `coordinator_schema`, `development_policy`, `review_fix`, `agent_mixture` |
| `core/` (renamed) | `plan_graph_authority` → `core/recovery_authority`; `plan_graph_budget` → `core/retry_budget` (the boundary fix: ledger + `gate_digest` are substrate) |
| `featurerun/` | `feature_run`, `feature_run_policy` |
| `plangraph/` | `plan_graph`, `plan_graph_audit`, `plan_graph_contract`, `plan_approval`, `plan_graph_integration` |
| `observability/` | `run_metrics`, `run_metrics_index`, `run_catalog`, `dashboard_server` |
| `graphrun/` | new: `__init__` re-exporting the FeatureRun + PlanGraph operator surface; `runner_support` extracting the shared program-runner helpers currently duplicated across `experiments/run_burden*_plan_graph.py` (decompose/approve/resume argparse shape, `GATE_ADJUDICATED_CRITERIA` wiring, instruction-pin scaffolding) |

Notes on judgment calls:
- `review_fix` and `agent_mixture` are core, not featurerun: both are consumed
  by executors/controllers generically; neither imports `feature_run`.
- `coordinator_schema`/`development_policy` stay core (imported by kernel-side
  modules); `feature_run_policy` goes with its harness.
- `plan_approval` is plangraph (imports `plan_graph`, `plan_graph_contract`).
- The `graphrun/runner_support` extraction is the only non-mechanical scope in
  the program; it may be deferred to a follow-up without weakening the layout.

## Allowed-import contract (the enforced boundaries)

1. `core/*` imports only `core/*` (and stdlib/third-party).
2. `featurerun/*` imports only `core/*` and `featurerun/*`.
3. `plangraph/*` imports only `core/*`, `featurerun/*`, `plangraph/*`.
4. `observability/*` imports only `core/*` and `observability/*`.
5. `graphrun/*` may import all of the above; nothing imports `graphrun/*`.
6. Legacy shims contain exactly one `from harness_labs.<new>.<mod> import *`
   (plus `__all__` passthrough) and a `DeprecationWarning`.

Checker: `scripts/dev/check_import_boundaries.py` — AST-walks the package
(no imports executed), derives the layer of every module from its path, and
exits 1 naming each violating edge. Runs in CI/gates and is the program's
finding instrument.

## Steps

Serial — every step rewrites imports across `tests/`, so no two steps can
share the tree. Each step's gate: `python3 scripts/dev/check_import_boundaries.py`
(from GR-01 on) **and** the full suite (`python3 -m pytest tests/ -q`, ~61 s,
477 tests at base) with zero behavior change expected — moves and renames
only, byte-identical logic.

- **GR-01 — Boundary checker + red capture.** Add
  `scripts/dev/check_import_boundaries.py` with the layer table above keyed to
  the *current flat names* (each flat module annotated with its future layer),
  plus `tests/test_import_boundaries.py` asserting (a) the checker passes on
  the tree, and (b) `import harness_labs.feature_run` in a fresh interpreter
  does not load any `plangraph`-layer module. At base, (b) **fails
  behaviorally** — `plan_graph_budget`/`plan_graph_authority` appear in
  `sys.modules` — which is the recorded red. GR-01 lands the checker with the
  budget/authority modules annotated `core` (their true layer), making the
  red pass only after GR-02 moves them. Checker initially warns-only for
  edges scheduled to be fixed by later steps; hard-fails at GR-06.
- **GR-02 — Substrate renames (the boundary fix).**
  `plan_graph_budget` → `harness_labs/core/retry_budget.py`,
  `plan_graph_authority` → `harness_labs/core/recovery_authority.py`; create
  the `core/` package; rewrite the ~6 importing sites (`feature_run`,
  `plan_graph`, tests); leave `plan_graph_budget.py`/`plan_graph_authority.py`
  shims. GR-01's red test goes green here.
- **GR-03 — Core move.** The 30 substrate modules move to `core/`; all
  in-repo imports rewritten (package, tests, `experiments/`, `scripts/`);
  shims at every old path. Largest mechanical step.
- **GR-04 — FeatureRun move.** `feature_run`, `feature_run_policy` →
  `featurerun/`; imports + shims; boundary rules 1–2 flip to hard-fail.
- **GR-05 — PlanGraph + observability move.** The five plangraph modules →
  `plangraph/`, the four observability modules → `observability/`; imports +
  shims; rules 3–4 hard-fail. (One step, not two: the clusters are disjoint,
  and the tree-wide import rewrite cost is shared.)
- **GR-06 — GraphRun surface + closure.** `graphrun/__init__` re-exports; the
  `runner_support` extraction (or its explicit deferral note); checker fully
  hard-fail (rule 5–6 included); README/docs updated to the new layout;
  `__init__.py` top-level re-exports preserved so
  `from harness_labs import FeatureRun`-style consumers are untouched; change
  log + shim-retirement operator note (shims removed after one release cycle
  or when `experiments/` runners are migrated, whichever is later).

## Compatibility policy

- **Top-level API unchanged:** `harness_labs/__init__.py` keeps exporting the
  same names from the new homes — external code using package-level imports
  never notices.
- **Old module paths keep working** for one cycle via shims (with
  `DeprecationWarning`), because `experiments/` runners, the dashboard
  launch config, old branches (`featurerun`, `plangraph`), and any user
  scripts import `harness_labs.<flat_name>` directly.
- **Journals/evidence unaffected:** no journaled payload embeds Python module
  paths as contract; run dirs and receipts are path-independent.
- **Dashboard:** `dashboard_server` moves but its CLI entry and HTTP surface
  are unchanged; `.claude/launch.json` configs updated in GR-05.

## Execution mode — recommendation

**Supervised branch refactor, not a PlanGraph program.** Reasons:
1. Every step's writable set is effectively the whole package + tests, so
   PlanGraph's write-grant enforcement — its sharpest tool — degenerates to
   "may touch everything," and review anchoring loses meaning.
2. Only GR-01/GR-02 have a genuine red; GR-03..GR-06 are green-preserving
   moves where red/green gating adds ceremony, not evidence. The deterministic
   value is fully captured by the boundary checker + full suite after each
   step, which a supervised branch provides at a fraction of the token cost.
3. The steps are strictly serial (shared-tree), so PlanGraph's parallel
   dispatch — its other advantage — is inert here.

If run as a program anyway (the self-hosting appeal is real), the node table
maps 1:1 from the steps with `max_parallelism=1`, each node's allowed_paths =
`harness_labs/**, tests/**, scripts/dev/check_import_boundaries.py` (+
`experiments/**, docs/**` for GR-03/GR-06), finding test =
`tests/test_import_boundaries.py` (genuine red only on GR-01/GR-02; waived
sink-style with full-suite `verification_argv` for GR-03..06, gate-only
criteria bound with `adjudication: "deterministic_verification"` per the CB-3
runner-template lesson).

## Risks

- **Import-rewrite misses in rarely-executed paths** (e.g.
  `controller_live_scenarios`, experiment runners): mitigated by the AST
  checker walking *all* files, not only imported ones, and by `python3 -m
  compileall harness_labs experiments scripts` in each gate.
- **Shim drift** (someone adds real code to a shim): checker rule 6 rejects
  any shim with more than the re-export statement.
- **Old branches** (`featurerun`, `plangraph` on GitHub): untouched by design;
  they predate the layout and stay historical snapshots.
- **Merge collisions:** nothing else should land on `main` mid-restructure;
  the program is a few hours of work — schedule it in one sitting.
