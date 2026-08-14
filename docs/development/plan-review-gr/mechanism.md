# GR plan review — MECHANISM lens

Subject: `docs/development/GRAPHRUN_RESTRUCTURE_PLAN.md` (GR-01..GR-06).
Tree: `graphrun-restructure` @ `5e99dda`, base `main` @ `1e9514a`.
Method: every claim below was executed, not reasoned about. Commands and raw
output are pasted inline.

---

## M1 — The star-import shim breaks 26 real import sites. Rule 6 forbids the fix.

The plan mandates (contract rule 6, plan:104-105) that a shim contain
*exactly* `from harness_labs.<new>.<mod> import *` plus `__all__` passthrough
plus a `DeprecationWarning`, and that checker rule 6 "rejects any shim with
more than the re-export statement" (plan:154).

**Fact 1: 17 of the 44 flat modules define `__all__`.**

```
$ grep -rn "^__all__" harness_labs/*.py | wc -l
17
$ ls harness_labs/*.py | wc -l
44
```

`from X import *` on a module with `__all__` binds **only** the `__all__`
names. For the 17 modules, the star shim is therefore strictly narrower than
the module's public surface:

```
=== modules with __all__ whose star-set is smaller than public dir() ===
  agent_mixture: __all__=7 public=24 hidden_by_all=17
  capability_broker: __all__=9 public=25 hidden_by_all=16
  claude_task_executor: __all__=1 public=34 hidden_by_all=33
  controller_live: __all__=8 public=40 hidden_by_all=32
  coordinator_dispatcher: __all__=7 public=33 hidden_by_all=26
  coordinator_schema: __all__=3 public=10 hidden_by_all=7
  development_policy: __all__=5 public=12 hidden_by_all=7
  feature_run: __all__=10 public=66 hidden_by_all=56
  feature_run_policy: __all__=2 public=4 hidden_by_all=2
  git_transaction: __all__=7 public=16 hidden_by_all=9
  plan_approval: __all__=11 public=37 hidden_by_all=26
  plan_graph: __all__=30 public=73 hidden_by_all=43
  plan_graph_contract: __all__=10 public=18 hidden_by_all=8
  plan_graph_integration: __all__=6 public=20 hidden_by_all=14
  review_fix: __all__=8 public=26 hidden_by_all=18
  usage: __all__=5 public=15 hidden_by_all=10
```

**Fact 2: 26 in-repo import sites name a symbol the star shim would not
re-export.** (AST sweep of every `.py` in the repo, cross-referenced against
the actual `import *` namespace obtained by executing it.)

```
=== names imported from a module but NOT re-exported by star ===
  controller_coordinator._tool_specs            <- tests/test_relax_kernel.py
  controller_live._RAW_OUTPUT_SCHEMA            <- harness_labs/claude_task_executor.py
  controller_live._WORKSPACE_CHANGE_RECEIPT_KIND<- harness_labs/claude_task_executor.py
  controller_live._filter_satisfied_criteria    <- harness_labs/claude_task_executor.py
  controller_live._is_latest_writable_attempt   <- harness_labs/claude_task_executor.py
  controller_live._parse_context                <- harness_labs/claude_task_executor.py
  controller_live._record_writable_attempt_started <- harness_labs/claude_task_executor.py
  controller_live._snapshot_delta_paths         <- harness_labs/claude_task_executor.py
  controller_live._worker_prompt                <- harness_labs/claude_task_executor.py
  dashboard_server._DashboardHandler            <- tests/test_dashboard_api.py, tests/test_dashboard_e2e.py
  dashboard_server._apply_cumulative_node_metrics <- tests/test_dashboard_api.py
  feature_run.FeatureRunHandoffArtifact         <- harness_labs/__init__.py:121, tests/test_feature_run.py
  feature_run.PlanGraphFeatureRunBinding        <- experiments/run_burden{,2,3}_plan_graph.py
  feature_run.RecoveryAgent                     <- harness_labs/__init__.py:124
  feature_run.RecoveryContext                   <- harness_labs/__init__.py
  feature_run.RecoveryDecision                  <- harness_labs/__init__.py:126, tests/test_feature_run.py
  feature_run.ReviewFixResult                   <- tests/test_feature_run.py
  feature_run.classify_verification_failure     <- scripts/plan_graph_recover.py, tests/test_feature_run.py,
                                                   tests/test_relax_gate_timeout_classification.py
  plan_graph.RepairResumeDirective              <- experiments/run_burden{,2,3}_plan_graph.py:68
  plan_graph._run_functionality_test            <- tests/test_plan_graph.py
  run_catalog._ID_MATCH_REASON                  <- tests/test_run_catalog.py
  run_catalog._REUSE_UNRESOLVED_REASON          <- tests/test_run_catalog.py
  run_catalog._detail_metrics                   <- tests/test_run_catalog.py
  run_catalog._graph_execution                  <- tests/test_run_catalog.py
  run_catalog._snapshot                         <- tests/test_run_catalog.py
  usage.parse_claude_result_usage               <- harness_labs/backends.py,
                                                   harness_labs/claude_agent_session.py,
                                                   harness_labs/claude_task_executor.py
MISSING COUNT: 26
```

Two classes of breakage, both fatal to the shim as specified:

* **Underscore names** (`_parse_context`, `_DashboardHandler`,
  `_run_functionality_test`, the five `run_catalog._*`): `import *` *never*
  re-exports them, `__all__` or not. In-repo consumers get rewritten to the
  new path so they survive — but the shim is then not a compatibility layer
  for anything that reaches for internals.
* **Public names excluded by `__all__`**: these are exactly the ones the
  plan's own compatibility policy (plan:118-123) cites as the *reason* shims
  exist. `experiments/run_burden*_plan_graph.py:68` imports
  `plan_graph.RepairResumeDirective` and `feature_run.PlanGraphFeatureRunBinding`;
  `scripts/plan_graph_recover.py` imports `feature_run.classify_verification_failure`.
  All three are public and all three are outside `__all__`. **The shim breaks
  precisely the unmigrated `experiments/` runners it was written to protect.**

**Executed proof.** I built the shim exactly as rule 6 specifies against a
copy of the tree (`featurerun/feature_run.py` with relative imports lifted one
level, `harness_labs/feature_run.py` = star + `__all__` passthrough +
`warnings.warn`):

```python
import warnings
from harness_labs.featurerun.feature_run import *
from harness_labs.featurerun.feature_run import __all__
warnings.warn("harness_labs.feature_run moved", DeprecationWarning, stacklevel=2)
```

```
BREAK FeatureRunHandoffArtifact -> cannot import name 'FeatureRunHandoffArtifact' from 'harness_labs.feature_run'
BREAK RecoveryDecision          -> cannot import name 'RecoveryDecision' from 'harness_labs.feature_run'
BREAK classify_verification_failure -> cannot import name 'classify_verification_failure' ...
BREAK PlanGraphFeatureRunBinding -> cannot import name 'PlanGraphFeatureRunBinding' ...

shim names: ['DeterministicVerificationResult', 'FeatureContractFactory',
 'FeatureProfileBuilder', 'FeatureRunResult', 'FeatureSessionFactory',
 'ReviewFixPolicy', 'VerificationGate', 'VerificationRepairExecutorFactory',
 'run_feature_worktree', 'run_plan_graph_feature_worktree', 'warnings']
```

10 of 66 public names survive. The `__all__` passthrough line is also inert:
it re-binds a list that `import *` already honoured; it adds nothing and
cannot widen the namespace.

**Isinstance is fine, patching is not.** `import *` rebinds the *same* class
object, so `isinstance` across shim and new path agrees. But the shim holds
*copies* of the bindings: `unittest.mock.patch("harness_labs.plan_graph_budget.<name>")`
would patch the shim's copy and leave the real module untouched — a silent
behaviour divergence, not an error. The repo has 20+ string-target patches
(`tests/test_plan_graph_budget.py:55`, `tests/test_controller_live.py:136`,
`tests/test_codex_delegation.py:58`, …). In-repo tests get rewritten so this
is latent, not live; it is still a property the plan claims shims have and
they do not.

### R1 — Proposed replacement mechanism (executed, passing)

Replace rule 6 with a **module-alias shim**:

```python
import sys, warnings, importlib
warnings.warn("harness_labs.feature_run moved to harness_labs.featurerun.feature_run",
              DeprecationWarning, stacklevel=2)
sys.modules[__name__] = importlib.import_module("harness_labs.featurerun.feature_run")
```

```
OK    FeatureRunHandoffArtifact
OK    RecoveryDecision
OK    classify_verification_failure
OK    PlanGraphFeatureRunBinding
OK    FeatureRunResult
identity (patch-safe): True
```

Every name — public, `__all__`-excluded, and underscore — resolves, and
`harness_labs.feature_run is harness_labs.featurerun.feature_run`, so patching
and `isinstance` are exact. It is still a three-line file, so checker rule 6
remains mechanically checkable (assert the shim body matches this template
AST-exactly). Caveat to state in the plan: the alias binds on *first* import,
so `from harness_labs import feature_run` before the shim executes yields the
shim object; in practice `harness_labs/__init__.py` imports the new paths, so
this does not arise. A PEP-562 `__getattr__` delegate is the alternative if
the `sys.modules` swap is considered too clever.

### R2 — `DeprecationWarning` at import time is safe here

```
$ ls pyproject.toml pytest.ini setup.cfg tox.ini
(none exist)
$ find . -name conftest.py -not -path "./.git/*"
(none)
$ grep -rn "filterwarnings|-W error|simplefilter" (repo)
(no hits)
$ grep -rn "pytest.warns|recwarn|catch_warnings|DeprecationWarning" tests/
(no hits)
$ python3 -c "import pytest; print(pytest.__version__)"
8.3.3
```

No `filterwarnings = error`, no conftest, no test asserts on warnings. pytest
will *display* the DeprecationWarnings (its default filters un-ignore them)
but nothing fails. **Self-refutation: this concern was unfounded.** The only
residual cost is warning noise in the suite summary for every shim imported —
worth a one-line `filterwarnings` ignore added alongside the shims, which in
turn requires creating `pyproject.toml`/`pytest.ini` (the repo has neither —
unstated new file in GR-02).

### R3 — `from harness_labs.plan_graph_budget import gate_digest` survives either way

`plan_graph_budget.py` has **no** `__all__`, and `gate_digest` is public
(`harness_labs/plan_graph_budget.py:43`), so star re-export covers it.
Consumers: `harness_labs/plan_graph.py:21`,
`tests/test_plan_graph_observability.py:21`,
`tests/test_relax_gate_decomposition.py:49`. The module's private helpers
(`_FAILING_IDENTIFIER_RE:75`, `_CLASS_LIMITS:94`, `_ATTEMPT_COUNTERS:106`,
`_Lock:838`) would *not* survive a star shim; nothing imports them today.
This specific case is safe — M1 is about the 16 other `__all__` modules.

---

## M2 — GR-01's red test is a permanent false red. It never goes green.

The plan specifies (plan:132-137): `import harness_labs.feature_run` in a
fresh interpreter must not load any plangraph-layer module, and this "fails
behaviorally at base."

**Executed at HEAD:**

```
$ python3 -c "import sys, harness_labs.feature_run;
  print(sorted(m for m in sys.modules if 'plan_graph' in m or 'plan_approval' in m))"
['harness_labs.plan_approval', 'harness_labs.plan_graph', 'harness_labs.plan_graph_audit',
 'harness_labs.plan_graph_authority', 'harness_labs.plan_graph_budget',
 'harness_labs.plan_graph_contract']
```

Six modules, not the two the plan predicts. **Attribution experiment** —
`import harness_labs` *alone*:

```
$ python3 -c "import sys, harness_labs; print(sorted(m for m in sys.modules if m.startswith('harness_labs')))"
[... 'harness_labs.plan_approval', 'harness_labs.plan_graph', 'harness_labs.plan_graph_audit',
 'harness_labs.plan_graph_authority', 'harness_labs.plan_graph_budget',
 'harness_labs.plan_graph_contract' ...]   # 38 modules
```

`harness_labs/__init__.py:131` (`from .plan_graph import ...`) and `:156`
(`from .plan_approval import ...`) load the whole plangraph cluster. Python
executes a package's `__init__` **before** any submodule, so
`import harness_labs.feature_run` can never avoid it.

**Isolating feature_run's own closure** — same tree, `__init__.py` emptied:

```
$ cp -r harness_labs $S/ && : > $S/harness_labs/__init__.py && cd $S && python3 -c "..."
WITH EMPTY __init__, feature_run closure (harness_labs.*):
['agent_sessions','attempts','audit','composition','controller_commands',
 'controller_coordinator','controller_evidence','controller_kernel','controller_live',
 'controller_projection','controller_results','controller_run','controller_scheduler',
 'coordinator_dispatcher','coordinator_schema','development_policy','feature_run',
 'git_transaction','plan_graph_authority','plan_graph_budget','review_fix',
 'text_executor','usage']
```

**Verdict:** feature_run's *genuine* plangraph dependency is exactly the two
modules GR-02 moves (via `harness_labs/feature_run.py:38`,
`from .plan_graph_budget import failing_identifiers`). But the plan's stated
red test measures `sys.modules` after a package import, which is dominated by
`__init__`. GR-06 explicitly *preserves* those `__init__` re-exports
(plan:151-153), so after the full restructure `__init__` will import
`harness_labs.plangraph.plan_graph` and the test — however its layer predicate
is written — stays red forever. **The test as written is red at base for the
wrong reason and never goes green.**

### R4 — GR-01 additionally has no red at all under its own checker design

Plan:137-139: "GR-01 lands the checker with the budget/authority modules
annotated `core` (their true layer), making the red pass only after GR-02
moves them." That is self-contradictory. If the checker's layer table
annotates `plan_graph_budget`/`plan_graph_authority` as `core`, then
`feature_run` (featurerun) → `core` satisfies rule 2 **at base, immediately**.
The checker is green on day one. So GR-01's finding instrument produces no
finding, and its only claimed red is M2's false red. GR-01 as specified lands
zero evidence.

### R5 — Correct construction (executed; red now, green at GR-02)

Assert on `feature_run`'s **own AST import closure**, not `sys.modules`. This
sidesteps `__init__` entirely, needs no interpreter isolation, and reuses the
checker's own AST machinery:

```
$ python3 - <<'EOF'  # transitive AST walk of relative/absolute intra-package imports from feature_run
AST closure of feature_run, size 24
plangraph-named in closure: ['plan_graph_authority', 'plan_graph_budget']
EOF
```

Red today (two plangraph-named modules in the closure); green the moment
GR-02 renames them into `core/`; and it stays green through GR-03..GR-06
because `__init__` is not on feature_run's closure. Assert the predicate as
"no module whose checker-assigned layer is `plangraph` appears in
`featurerun/*`'s closure" so it generalises to the post-move names.

If a runtime (rather than static) red is wanted, the isolated-`__init__`
experiment above is the construction: exec the module from its file with a
stub parent package. Note `importlib.spec_from_file_location` alone does
**not** work — it fails on the relative imports:

```
EXEC FAILED: ImportError attempted relative import with no known parent package
```

### R6 — `harness_labs/__init__.py` has no layer

The checker "derives the layer of every module from its path" (plan:152).
`harness_labs/__init__.py` sits at the package root and imports *everything*,
including plangraph (`:131`, `:156`). Under rules 1-5 it belongs to no layer;
under rule 5 it behaves like `graphrun/`. The plan never assigns it. Specify:
`harness_labs/__init__.py` is graphrun-layer (may import all, imported by
none) — otherwise the checker either crashes on it or silently skips the one
file that concentrates every cross-layer edge.

---

## M3 — The warns-only → hard-fail phasing has no stated mechanism

Plan:139-140 ("Checker initially warns-only for edges scheduled to be fixed by
later steps; hard-fails at GR-06"), plan:145 ("boundary rules 1-2 flip to
hard-fail"), plan:148 ("rules 3-4 hard-fail"). Nowhere does the plan say how
the checker knows which step the tree is at. A flag (`--step GR-04`) is
gameable and untestable from a bare gate invocation; a mutable constant in the
script is invisible to review.

**R7 — Concrete mechanism.** Make the phase a property of the *tree*, not an
argument. The checker infers enforcement per rule from directory existence:

| rule | enforced hard when |
|---|---|
| 1 (`core/*` → `core/*` only) | `harness_labs/core/` exists |
| 2 (`featurerun/*`) | `harness_labs/featurerun/` exists |
| 3 (`plangraph/*`) | `harness_labs/plangraph/` exists |
| 4 (`observability/*`) | `harness_labs/observability/` exists |
| 5 (nothing imports `graphrun/*`) | `harness_labs/graphrun/` exists |
| 6 (shim body template) | always, for every flat `harness_labs/*.py` shadowed by a new-home module |

A rule that is not yet enforceable is *inapplicable* (its layer has no
members) rather than "warns-only" — so there is no phase state at all, no
flag, and the checker source is identical from GR-01 through GR-06. Flat
modules not yet moved are checked against the *annotated future layer* table
(which GR-01 lands) in warn mode; the annotation table shrinks by one entry
per move, and GR-06's closure assertion is simply "annotation table is empty."
That is mechanically verifiable in the checker's own test.

Precedent for the shape exists: `scripts/check_repository_contracts.py` is a
flagless, tree-derived, exit-1 contract checker. Note the plan places the new
checker at `scripts/dev/check_import_boundaries.py`; `scripts/dev/` exists and
holds `red_green_check.py`, `check_workaround_retirement.py` — consistent.

---

## M4 — Gates: `compileall` is contradictory, and it cannot catch what the risk claims

**Contradiction.** Plan:129-131 defines the per-step gate as the checker
**and** `python3 -m pytest tests/ -q` — `compileall` is absent. Plan:157-159
(Risks) asserts mitigation "by `python3 -m compileall harness_labs experiments
scripts` **in each gate**." The gate definition and the risk mitigation
disagree. Pick one; if compileall is a gate, list it in the Steps section.

**It would not help anyway.** Executed:

```
$ printf 'import harness_labs.nonexistent_module_xyz\n' > /tmp/cae/broken.py
$ python3 -m compileall -q /tmp/cae ; echo EXIT=$?
EXIT=0                      # bad import NOT caught
$ printf 'def f(:\n' > /tmp/cae/syntax.py
$ python3 -m compileall -q /tmp/cae ; echo EXIT=$?
*** Error compiling '/tmp/cae/syntax.py'... SyntaxError: invalid syntax
EXIT=1                      # only syntax
```

`compileall` byte-compiles; it never executes an import. The named risk —
"import-rewrite misses in rarely-executed paths (e.g.
`controller_live_scenarios`, experiment runners)" — is exactly an *import*
failure, which compileall is blind to.

**R8 — What actually catches it:** a smoke-import loop over every module in
the package plus every top-level script/experiment, e.g.

```python
for path in Path("harness_labs").rglob("*.py") + experiments/scripts entrypoints:
    importlib.import_module(module_name_for(path))   # fail loudly
```

executed as a gate step. This is the only mechanism that exercises
`controller_live_scenarios` (which the AST closure above confirms is *not*
reachable from `feature_run` or from `harness_labs/__init__`, hence never
imported by the suite). Alternatively, since the checker already AST-walks
every file, have it *resolve* every intra-package import target to an existing
file — a static equivalent with no execution risk. That is strictly better
than compileall and costs nothing extra.

**Suite timing confirmed.** Plan claims ~61 s / 477 tests:

```
$ time python3 -m pytest tests/ -q
477 passed, 1 skipped in 64.60s (0:01:04)
```

477 passed + 1 skipped (478 collected). 64.6 s wall on this machine. Six steps
× (checker + suite) ≈ 7 min of gate time — the plan's cost claim holds.

---

## M5 — `.claude/launch.json` does not reference `dashboard_server`

Plan:126-127: "`.claude/launch.json` configs updated in GR-05." Executed:

```
$ cat .claude/launch.json
{ "version": "0.0.1", "configurations": [ { "name": "cb-dashboard",
  "runtimeExecutable": "python3",
  "runtimeArgs": ["scripts/run_dashboard.py", "--audit-root", "logs/runs/cb-graph",
                  "--assets-root", "dashboard/plan-graph/dist", "--port", "8321",
                  "--refresh-seconds", "10"], "port": 8321 } ] }
```

It invokes `scripts/run_dashboard.py` by path and names no Python module.
**`launch.json` requires no change in GR-05.** The file that does is
`scripts/run_dashboard.py:11` (`from harness_labs.dashboard_server import ...`),
which is already covered by the generic "rewrite `scripts/`" work. Fix the
plan's GR-05 line to name `scripts/run_dashboard.py`; otherwise GR-05 carries
a work item that resolves to a no-op and an actual edit site that is only
implicitly covered. Repo-wide, the only non-`.py` references to
`dashboard_server` are in `docs/development/*.md` (historical plans) — no
JSON/shell/TOML config binds the module path.

---

## M6 — GR-05's clusters are disjoint as *modules*, overlapping as *tests*

Plan:147-150 justifies merging plangraph and observability into one step
because "the clusters are disjoint."

**True at the package level.** Executed:

```
=== observability modules' intra-package imports ===
run_metrics:        from .audit import AuditError, AuditJournal
run_metrics_index:  from .run_metrics import project_run_metrics
run_catalog:        from .audit import AuditError
                    from .run_metrics import TERMINAL_STATUSES, availability, project_run_metrics
dashboard_server:   from .audit import AuditError
                    from .run_catalog import RunCatalog, build_run_detail, merge_run_catalogs
=== plangraph modules importing observability? ===
(no hits)
```

Observability depends only on `audit` (core) and itself; plangraph never
touches observability. No shared file between the two move sets. The claim
holds for the code being moved.

**False at the test level** — four files import from both clusters:

```
BOTH: tests/test_dashboard_api.py        (pg=10 ob=5)
BOTH: tests/test_dashboard_e2e.py        (pg=2  ob=4)
BOTH: tests/test_run_catalog.py          (pg=29 ob=11)
BOTH: tests/test_run_catalog_contracts.py(pg=7  ob=3)
```

**Self-refutation:** this does not damage GR-05 — it is *one* step, so both
rewrites land in the same commit and the overlap is irrelevant. It does
damage the reasoning: had GR-05 been split, the two halves would have
collided in four test files. The plan should say "merged because the
tests overlap" rather than "merged because the clusters are disjoint" — the
overlap is the stronger argument and the disjointness argument is the weaker
one.

The same sweep confirms the plan's serial-only premise is real for GR-04 vs
GR-05: 13 test files import both `feature_run` and a GR-05 cluster module,
including `tests/test_plan_graph.py`, `tests/test_feature_run.py`,
`tests/test_run_catalog.py`. No two steps can share the tree. The
"supervised branch, not a PlanGraph program" recommendation (plan:163-175) is
mechanically well-founded.

---

## Per-step verdicts

| Step | Verdict | Blocking findings |
|---|---|---|
| GR-01 | **Unsound as specified** | M2 (false red, never goes green), R4 (checker green at base — no finding at all), M3 (phasing mechanism absent). Needs R5 + R7 before it is executable. |
| GR-02 | **Sound goal, broken shim** | M1 (rule-6 shim). `plan_graph_budget` itself survives a star shim (R3), so GR-02 in isolation is lucky; the mechanism it establishes for GR-03+ is not. Adopt R1. Also: R2's `filterwarnings` config file is unstated new work. |
| GR-03 | **Blocked on M1** | 30 modules, includes `controller_live` (32 public names hidden by `__all__`, 8 underscore names imported by `claude_task_executor.py`), `usage` (`parse_claude_result_usage` hidden, 3 importers), `git_transaction`, `development_policy`, `coordinator_schema`. Largest star-shim blast radius. |
| GR-04 | **Blocked on M1** | `feature_run` is the worst case: 56 of 66 public names hidden; 7 imported from `__init__.py`, `experiments/`, `scripts/`, `tests/`. |
| GR-05 | **Sound, two corrections** | M5 (`launch.json` needs no edit; `scripts/run_dashboard.py:11` does), M6 (fix the merge rationale). Module disjointness verified. |
| GR-06 | **Underspecified** | R6 (`__init__.py` unlayered but imports everything), M3/R7 (what "fully hard-fail" means mechanically), M4/R8 (replace compileall with a smoke-import or static-resolution gate). Preserving `__init__` re-exports is the direct cause of M2. |

## Self-refutations recorded

* **R2** — I expected `DeprecationWarning` at import time to break the suite
  via a `filterwarnings = error` config. There is no pytest config file at
  all, no conftest, and no test asserts on warnings. The concern was wrong;
  the only real cost is summary noise.
* **R3** — I expected `from harness_labs.plan_graph_budget import gate_digest`
  to break through a star shim. It does not: that module has no `__all__` and
  `gate_digest` is public. The M1 breakage is real but does not touch the
  specific symbol the review brief flagged.
* **M6** — I expected the GR-05 disjointness claim to be false and to force a
  split. The module-level claim is true; the test-level overlap I found
  *supports* merging rather than splitting. The plan's conclusion is right for
  a reason it did not state.
* **M2 attribution** — my first `sys.modules` run showed six plangraph modules
  and I could have stopped there and called GR-02 insufficient. The
  empty-`__init__` isolation showed feature_run's own closure contains exactly
  the two modules GR-02 moves. GR-02's *scope* is correct; only its *test* is
  broken.
