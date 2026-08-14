# GraphRun restructure — package layout for a multi-harness lab (GR)

**Version:** v2 — post three-lens adversarial review
(`plan-review-gr/adjudication.md`, 2026-08-14). The layout survived review;
most of v1's mechanics did not: the red test was unsatisfiable, the shim
policy was measured broken and consumer-less, GR-02 was 986 lines too big,
and a second (hidden, in-function) boundary violation entered scope.
**Base:** `origin/main` at `1e9514a` (the integrated line, local branch
`Impl-redo`). The local `main` ref is stale (`c4d6111`) — see preconditions.
**Goal:** reshape the flat 44-module `harness_labs/` package into a layered
lab — `core/` substrate, `featurerun/` and `plangraph/` harness layers,
`observability/`, and a thin `graphrun/` composition surface — so future
harnesses land as siblings on `core`, and FeatureRun's standalone-ness becomes
a mechanically enforced contract.

## Preconditions

1. Retarget the stale local `main` branch to `origin/main` (or delete it), so
   "nothing lands on the trunk mid-restructure" governs the ref actually in
   use. Work happens on `graphrun-restructure`; the trunk receives one merge.
2. Measured baseline at `1e9514a`: full suite 477 passed + 1 skipped, ~65 s.

## The grounding (v2 — review-corrected)

The measured import graph shows **two** boundary violations, not one:

1. `feature_run.py:38` imports `failing_identifiers` from `plan_graph_budget`
   — a 14-line pytest-output parser. That parser is the *only* thing
   FeatureRun consumes from the plangraph-branded modules;
   `RetryBudgetLedger`, `gate_digest`, and the recovery-authority machinery
   are consumed exclusively by the plangraph layer and stay there.
2. `development_policy.py:107,115` (core) imports `feature_run_policy`
   (featurerun) via **deferred in-function imports**, closing the cycle
   `feature_run_policy → coordinator_schema → development_policy →
   feature_run_policy`. Invisible to a module-level-only AST walk — the
   checker must walk function bodies.

Also measured: `harness_labs/__init__.py:131,156` eagerly re-exports the
plangraph surface, so any `sys.modules`-based standalone test is permanently
red; standalone-ness must be asserted on **static import closures**, not
runtime module tables. And module paths appear as *strings* — twelve modules
patched by literal in tests, a journaled `f"{__module__}.{__name__}"` label
(`backends.py:53`), and `scripts/run_plan_graph.py:34`'s `module:callable`
resolver — so every move step rewrites imports **and** module-path strings.

## Target layout and complete module mapping

```
harness_labs/
  core/           # substrate any harness reuses
  featurerun/     # single-feature harness (imports core only)
  plangraph/      # graph orchestration (imports core + featurerun)
  observability/  # metrics/catalog/dashboard (imports core only)
  graphrun/       # composition + operator surface (may import all; nothing imports it)
```

| New home | Modules |
|---|---|
| `core/` | `attempts`, `audit`, `usage`, `git_transaction`, `text_executor`, `backends`, `composition`, `agent_sessions`, `claude_agent_session`, `codex_agent_session`, `omlx_agent_session`, `claude_task_executor`, `model_capability_executor`, `codex_delegation`, `capability_broker`, `controller_commands`, `controller_evidence`, `controller_results`, `controller_kernel`, `controller_live`, `controller_live_scenarios`, `controller_projection`, `controller_scheduler`, `controller_coordinator`, `controller_run`, `coordinator_dispatcher`, `coordinator_schema`, `development_policy`, plus `test_output.py` (new home of `failing_identifiers`) |
| `featurerun/` | `feature_run`, `feature_run_policy`, `review_fix` (import direction: only feature_run consumes it; concept direction: CB3-06 gave it node/writable-path vocabulary) |
| `plangraph/` | `plan_graph`, `plan_graph_audit`, `plan_graph_contract`, `plan_approval`, `plan_graph_integration`, `plan_graph_budget`, `plan_graph_authority` (budget/authority are plangraph-only consumers — v1's move-to-core withdrawn) |
| `observability/` | `run_metrics`, `run_metrics_index`, `run_catalog`, `dashboard_server` — import-clean on core; **data-coupled** to featurerun/plangraph journal event shapes (accepted residual, documented in `run_catalog`'s docstring; no import checker can see it) |
| `graphrun/` | `agent_mixture` (the executor/session composition surface — nothing in-package imports it) + `__init__` re-exporting the FeatureRun + PlanGraph operator surface |

`runner_support` extraction (deduplicating `experiments/run_burden*.py`):
**deferred** by rule of three — the argparse duplication is real (~84%
identical) but `GATE_ADJUDICATED_CRITERIA` exists in exactly one file;
extract when a third runner exists.

## Allowed-import contract

1. `core/*` imports only `core/*`.
2. `featurerun/*` imports only `core/*` and `featurerun/*`.
3. `plangraph/*` imports only `core/*`, `featurerun/*`, `plangraph/*`.
4. `observability/*` imports only `core/*` and `observability/*`.
5. `graphrun/*` may import all of the above; nothing imports `graphrun/*`
   (derivable from 1–4; stated as intent).
6. `harness_labs/__init__.py` is the compatibility surface: it re-exports the
   public API from the new homes, unchanged names. **No per-module shims** —
   the review measured the star-import shim exposing 10 of 66 names on
   `feature_run` and silently vacating 28 mock-patch sites, and the consumer
   audit found zero consumers needing shims (all 13 in-repo importers are
   rewritten in-step; nothing external imports flat paths).

Checker: `scripts/dev/check_import_boundaries.py` —
- AST-walks every module **including function bodies** (violation 2 is
  invisible at module level) and resolves both `import`/`from` forms and
  relative imports;
- derives each module's layer from its path;
- phasing is **tree-derived and flagless**: a layer's rules hard-fail once
  that layer's directory exists; before that they warn. No step-state.
- companion string sweep: greps for stale `harness_labs.<flat_name>` module
  paths in string literals (test patches, launcher specs) once the flat
  module is gone.

## Steps

Serial (every step rewrites imports across `tests/`). Per-step gate:
1. `python3 scripts/dev/check_import_boundaries.py` (from GR-01 on);
2. smoke-import loop over every `harness_labs.*` module (`compileall` was
   measured useless — it passes broken imports);
3. full suite (`python3 -m pytest tests/ -q`) — zero behavior change: moves
   and renames only;
4. stale-string grep (from GR-03 on).

- **GR-01 — Checker + red capture.** Land the checker (function-body-aware)
  and `tests/test_import_boundaries.py` with two red assertions on **static
  import closures** (not sys.modules): (a) `feature_run`'s closure contains
  no plangraph-layer module — red now via `plan_graph_budget` /
  `plan_graph_authority`; (b) `development_policy`'s closure (including
  deferred imports) contains no featurerun-layer module — red now via
  `feature_run_policy`. Checker warns-only (no layer dirs exist yet).
- **GR-02 — Boundary fixes.** (a) Move `failing_identifiers` + its regex to
  `harness_labs/core/test_output.py` (creating `core/`); rewrite the
  importing sites; `plan_graph_budget` keeps everything else. (b) Break the
  `development_policy ⇄ feature_run_policy` cycle: relocate the two deferred
  call sites' logic so the core module no longer reaches into the featurerun
  layer (the featurerun side may import core freely). Both GR-01 reds go
  green; no other behavior change.
- **GR-03 — Core move.** The core modules move to `core/`; all in-repo
  imports and module-path strings rewritten (package, tests, `experiments/`,
  `scripts/`, including the twelve string-literal patch targets and
  `backends.py`'s journaled label — the changed label value is accepted).
  Core rules hard-fail from here.
- **GR-04 — Harness + observability move.** `featurerun/` (3 modules),
  `plangraph/` (7), `observability/` (4) in one step — the clusters are
  measured disjoint and share the tree-wide rewrite cost; four test files
  import across clusters, which is why the moves cannot be parallel steps
  anyway. `scripts/run_dashboard.py` import updated (the launch config names
  the script, not a module). Rules 2–4 hard-fail.
- **GR-05 — GraphRun surface + closure.** `agent_mixture` → `graphrun/`;
  `graphrun/__init__` re-exports; `harness_labs/__init__` re-pointed at new
  homes (public names unchanged); README/docs updated to the layout;
  `run_catalog` data-coupling docstring; checker fully hard-fail; change-log
  entry; explicit deferral notes (`runner_support`, shim mechanism recorded
  in the adjudication if ever needed).

## Execution mode — recommendation (unchanged, premise corrected)

**Supervised branch refactor.** Write-grants degenerate on whole-package
steps, reds exist only in GR-01/02, and the steps are strictly serial — all
three PlanGraph advantages are inert here. (The v1 claim that CB review
machinery would catch nothing extra was challenged by FRAME and is softened:
a review stage *would* re-check the string-sweep completeness; the supervised
gate covers this with the stale-string grep instead.) If run as a program:
five nodes, `max_parallelism=1`, closure tests as finding tests (genuine reds
in GR-01/02 only), full-suite `verification_argv`, gate-only criteria bound
with `adjudication: "deterministic_verification"`.

## Risks

- **String-path misses**: the sweep greps for every flat module name in
  string literals across the repo each step; the twelve known patch sites are
  enumerated in the step checklist.
- **Rarely-executed paths**: smoke-import loop covers every module, not only
  suite-imported ones.
- **Trunk collisions**: precondition 1 makes the trunk ref unambiguous;
  restructure lands as one merge, scheduled in one sitting.
- **Old branches** (`featurerun`, `plangraph` on GitHub): historical
  snapshots, untouched by design.

## Change log

- **2026-08-14 (executed)** — GR-01..GR-05 landed on `graphrun-restructure`
  (supervised, per the execution-mode recommendation): checker + closure reds
  (`67a3ad3`), boundary fixes with both reds green (`61f9566`, including
  deletion of the dead `implement_v13_*` aliases), core move (`6c67e77`),
  featurerun/plangraph/observability moves (`59ea54e`), graphrun surface +
  closure (this commit). Every step gated by checker (0 errors), smoke-import
  loop over all modules, and the full suite (480 passed + 1 skipped).
  Execution surfaced two string forms v2 had not enumerated — indented
  in-function relative imports and bare-filename source-scan paths
  (`tests/test_controller_scenarios.py`) — both folded into the step recipe.
  Deferred as planned: `runner_support` (rule of three); per-module shims
  (none needed — zero external flat-path consumers).
