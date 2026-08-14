# GR restructure plan — NECESSITY lens (2026-08-14)

Subject: `docs/development/GRAPHRUN_RESTRUCTURE_PLAN.md` at `5e99dda`; base
`main` `1e9514a` (= HEAD~1; the plan text is the only diff, so all code
citations below are equally true at base).

Method: full AST import graph of all 43 non-`__init__` modules, resolving
relative imports and recording whether each edge is module-level or deferred
inside a function; cross-checked against a live `sys.modules` probe, a sweep of
every non-package consumer (`experiments/ scripts/ dashboard/ bin/ .claude/
agents/ skills/ examples/ templates/`), and a string-target sweep of
`tests/` (`patch("harness_labs.…")`).

---

## C1 — "exactly one real boundary violation" is false: there is a second, and
## it is a cycle through `core/`

`development_policy` (plan assigns **core**, line 40, and defends the call at
line 50) imports `feature_run_policy` (plan assigns **featurerun**, line 42) —
twice, as deferred in-function imports:

- `harness_labs/development_policy.py:107` — `from .feature_run_policy import standard_feature_run_policy`
- `harness_labs/development_policy.py:115` — `from .feature_run_policy import standard_feature_run_dispatch_schema`

This is a `core → featurerun` upward edge, violating allowed-import rule 1
(plan line 58) and rule 2's spirit. It is not incidental: it closes a cycle
`feature_run_policy → coordinator_schema → development_policy →
feature_run_policy` (`harness_labs/feature_run_policy.py:160`,
`harness_labs/coordinator_schema.py:10`, `development_policy.py:107`), and the
deferred-import form is precisely the device that keeps the cycle importable
today. Both edges survive into a package layout, where the cycle would span
package directories.

Consequences the plan does not carry:
1. The grounding discovery (lines 13-24, "No other upward or cross edges
   violate the target layering") is wrong, and the finding that motivates the
   whole program is under-measured.
2. A checker that only walks *module-level* `Import`/`ImportFrom` nodes — the
   natural reading of "AST-walks the package" (line 66) — will not see this
   edge at all, so GR-04's "rules 1–2 flip to hard-fail" (line 100) would pass
   over a live violation. The checker must descend into function bodies.
3. GR-04 acquires unplanned design work: either `standard_feature_run_policy` /
   `standard_feature_run_dispatch_schema` (deprecated `implement_v13_*`
   aliases, `development_policy.py:104-118`) get deleted, or they move to
   `featurerun/`, or `core/` must permit a documented deferred exception.
   None is a "move and rename only, byte-identical logic" change (line 77).

Three other deferred edges exist and are all layer-legal:
`controller_run → controller_commands` (`controller_run.py:436`),
`feature_run_policy → coordinator_schema` (`feature_run_policy.py:160`),
plus the two above.

## C2 — GR-02's stated rationale is factually wrong; the fix is ~15 lines,
## not two 986-line module renames

Plan lines 17-19: "The RB retry-budget ledger and `gate_digest` … are consumed
by FeatureRun's `_verify_with_recovery` — they are shared recovery substrate,
misnamed."

FeatureRun consumes neither. The sole edge is:

    harness_labs/feature_run.py:38
        from .plan_graph_budget import failing_identifiers

used at `feature_run.py:1854` and `:2202`. `failing_identifiers`
(`plan_graph_budget.py:78-91`) is a 14-line pure parser over pytest's
`FAILED`/`ERROR` summary lines. Whole-repo consumers of the ledger and digest:

- `RetryBudgetLedger` / `gate_digest` / `BudgetError`: `plan_graph.py:21`
  (+ uses at :1181, :1292, :1447, :1663), `scripts/run_plan_graph.py:27`,
  `scripts/plan_graph_recover.py`, and plangraph tests only.
- `AutomaticRecoveryAuthority` / `RecoveryAuthorityError` /
  `validate_recovery_decision`: `plan_graph_budget.py:19` and
  `plan_graph_authority.py` itself — nothing else.

So the 851-line ledger and the 135-line authority module are consumed
**exclusively by plangraph**. Relocating them to `core/` (plan line 41) does
not fix a layering error; it imports plangraph-only policy into the substrate
every future harness is supposed to reuse, and dilutes what `core/` means —
the exact failure mode the program exists to prevent. The minimal, correct
fix is to move `failing_identifiers` + `_FAILING_IDENTIFIER_RE`
(`plan_graph_budget.py:75-91`) into core (it belongs next to gate-output
parsing, not next to a retry ledger) and leave `plan_graph_budget` /
`plan_graph_authority` in `plangraph/`, renamed there if the branding still
offends. That is one small function move plus three import lines, and it
eliminates GR-02's shims, its two renames, and the churn in
`tests/test_plan_graph_budget.py`, `tests/test_plan_graph_authority.py`,
`tests/test_plan_graph_observability.py`, `tests/test_relax_gate_decomposition.py`.

## C3 — GR-01's red is not a red: the specified probe can never go green

The finding test (plan lines 83-89) is: `import harness_labs.feature_run` in a
fresh interpreter must load no plangraph-layer module. Measured on a clean
interpreter at base:

    plangraph-ish modules in sys.modules: plan_approval, plan_graph,
    plan_graph_audit, plan_graph_authority, plan_graph_budget,
    plan_graph_contract

The reason is not `feature_run.py:38`. Importing any submodule executes the
parent package `__init__`, and `harness_labs/__init__.py:131` does
`from .plan_graph import (…)` unconditionally (also `:156 from .plan_approval
import …`). After GR-02, after GR-05, after everything, that probe stays red —
`plan_graph` will still be eagerly imported by `__init__`. And it must be:
the compatibility policy (lines 115-118) and GR-06 (line 108) both mandate
that `__init__` keeps eagerly re-exporting the plangraph surface. The plan's
one genuine red phase and its top-level-API-unchanged guarantee are in direct
contradiction; as written GR-02's exit criterion cannot be met.

Fixes, either of which must be chosen explicitly in GR-01: (a) make the red a
*static closure* assertion — compute `feature_run`'s transitive module-level
import closure by AST and assert no plangraph member — which is genuinely red
at base and genuinely green after the C2 fix; or (b) run the probe in a
subprocess that loads the module file directly with a stubbed parent package,
which is fragile and tests something no consumer does. (a) is right, and it
also makes GR-01 one instrument (the checker) instead of two.

## C4 — GR-06's `runner_support` extraction is scope growth, and one of its
## three named targets does not exist to be shared

Plan line 45 lists three things duplicated "across
`experiments/run_burden*_plan_graph.py`": the decompose/approve/resume argparse
shape, `GATE_ADJUDICATED_CRITERIA` wiring, and instruction-pin scaffolding.

- Argparse shape: real. `run_burden_plan_graph.py:824-830`,
  `run_burden2_plan_graph.py:819-825`, `run_burden3_plan_graph.py:837-843` are
  the same block; burden2 vs burden3 are 84% line-identical overall (737 of
  ~860 lines).
- `GATE_ADJUDICATED_CRITERIA`: exists in exactly **one** file
  (`experiments/run_burden3_plan_graph.py:577,586`). Nothing to de-duplicate.
- "Instruction-pin scaffolding": zero occurrences of any `instruction_pin` /
  `INSTRUCTION_PIN` identifier anywhere in `harness_labs/` or `experiments/`.
  The nearest referent is a prose comment at `run_burden3_plan_graph.py:92`.

Beyond the mis-citation: these runners are frozen records of executed programs
(CB, CB-2, CB-3, orbit). Rewriting ~3,900 lines of past-run scaffolding is a
behavior-bearing refactor with no test coverage, bolted onto a program whose
entire safety argument is "moves and renames only, byte-identical logic"
(line 77). The plan already concedes it "may be deferred" (lines 53-54). It
should be deferred outright, not left as an author's-choice branch inside a
step.

## C5 — GR-04 and GR-05 should be one step, by the plan's own argument

GR-04 moves two files (`feature_run`, `feature_run_policy`). The plan merges
the plangraph and observability clusters into GR-05 with the reason "the
clusters are disjoint, and the tree-wide import rewrite cost is shared"
(lines 103-104). Featurerun is disjoint from both and is the *smallest*
cluster; the same argument applies with more force. Since every step is
strictly serial and each pays a whole-tree import rewrite plus a ~61 s suite,
each extra step is pure overhead. The one thing that argues for keeping GR-04
separate — flipping rules 1–2 to hard-fail — is a checker config line that a
merged step flips just as well, and it now carries the C1 `development_policy`
design decision, which is the real reason to keep a boundary here. Merge
GR-04+GR-05 into one move step; if C1's fix turns out to be non-mechanical,
split *that* out as its own small step instead (a genuine red, unlike the
moves).

---

## M1 — The shim policy is unnecessary: nothing outside the repo imports these
## paths, and the plan rewrites everything inside it

Shims are 43 files plus checker rule 6 plus a retirement operator note
(lines 63-64, 110-111, 156). Their justification (lines 119-122) is
"`experiments/` runners, the dashboard launch config, old branches, and any
user scripts."

Measured:
- In-repo non-test consumers of flat submodule paths: **13 files** — eight in
  `experiments/`, five in `scripts/` (`approve_plan.py`,
  `dashboard_fixture_run.py`, `plan_graph_recover.py`, `run_dashboard.py`,
  `run_plan_graph.py`). GR-03 and GR-05 already rewrite these (lines 96-97).
  A shim protecting a call site you rewrote in the same step protects nothing.
- The dashboard launch config does **not** import a module path.
  `.claude/launch.json` invokes `python3 scripts/run_dashboard.py …`. Plan
  line 125 ("`.claude/launch.json` configs updated in GR-05") is a no-op item.
- `dashboard/`, `bin/`, `agents/`, `skills/`, `examples/`, `templates/`,
  `schemas/`: zero `harness_labs.<mod>` imports.
- "User scripts" and "old branches": there is no `pyproject.toml`, `setup.py`,
  or `setup.cfg` anywhere in the tree, no `conftest.py`, and the package
  resolves purely by cwd (`harness_labs.__file__` is the worktree path).
  `harness_labs` is not installable and is not installed; the `featurerun` /
  `plangraph` branches are separate checkouts carrying their own copy of the
  package. No consumer exists that a shim could serve.
- 51 test files import flat paths; the plan rewrites them each step.

So the shims serve a hypothetical population of size zero, while creating
real cost: 43 files, a checker rule to police them, and a retirement decision
deferred to "one release cycle or when `experiments/` runners are migrated"
(line 111) for a repo that has no releases.

Recommendation: drop shims entirely. Keep only the `harness_labs/__init__.py`
top-level re-exports (which the plan already keeps, line 116) — those are what
actually preserve the public surface, and they cost nothing extra. Rewrite the
13 + 51 in-repo call sites, which the plan does anyway. If a migration net is
wanted at all, one `__getattr__`-based lazy alias in `harness_labs/__init__.py`
(PEP 562, ~10 lines, one `DeprecationWarning`) covers every old path and is
deleted in one edit — versus 43 files.

## M2 — If shims ship anyway, they silently break string-target patching in
## the five modules that have no `__all__`

`tests/` contains 62 `patch("harness_labs.<mod>.<attr>")` sites. Under a
`from .core.<mod> import *` shim (line 63), a patch aimed at the shim rebinds
the *shim's* copy of the name; the real module's globals — what the code
actually reads — are untouched.

Whether that fails loudly or silently depends on `__all__`. Only 16 of 43
modules define one. The un-`__all__`'d modules carrying patch targets are
`backends` (13 sites), `codex_delegation` (8, e.g.
`tests/test_codex_delegation.py:58-59`), `dashboard_server` (4),
`plan_graph_budget` (2, `tests/test_plan_graph_budget.py:55-56`), `audit` (1)
— 28 sites where `import *` *does* re-export the incidental
`subprocess`/`shutil`/`os` module names, so the patch succeeds, does nothing,
and the test goes green against unpatched code. The plan's "`__all__`
passthrough" (line 64) does not save these: the source modules have no
`__all__` to pass through. This is a vacuous-green channel in a program whose
whole assurance is "the suite stays green."

## M3 — Two dynamic-reference channels the AST checker cannot see

The risk section (lines 152-155) claims AST coverage of all files is the
mitigation for rewrite misses. Two channels escape it:

- `scripts/run_plan_graph.py:30-37` resolves a launcher from a
  `module:callable` **string** via `importlib.import_module`. A
  `--launcher harness_labs.x:y` argument, or such a string persisted in a
  registration, survives no move and is invisible to the checker. (I found no
  persisted `harness_labs.*` launcher refs under `logs/` or `.harness/`, so
  this is a live hazard for future invocations, not a present breakage.)
- The 62 `patch("harness_labs…")` string targets in `tests/` (M2) are strings,
  not imports; a text sweep of `tests/` must be an explicit gate item in every
  move step, not left to the AST walk.

Both are cheap to cover: add a grep-based check for `harness_labs.<flat_name>`
appearing inside string literals to the same script.

---

## Self-refutations — attacks I considered and dropped

**R1 — "The module mapping is probably incomplete or padded."** It is neither.
Set-differenced the plan's 43 assignments against the tree: zero unmapped
modules, zero phantom entries, 43 = 43. The mapping is exact.

**R2 — "`review_fix` and `agent_mixture` don't really belong in core."** The
plan's judgment call (lines 48-50) survives closure inspection.
`review_fix` → `{attempts, audit, controller_evidence, controller_results,
git_transaction}`; `agent_mixture` → `{agent_sessions, audit,
claude_agent_session, claude_task_executor, codex_agent_session,
controller_evidence, controller_live, controller_scheduler, git_transaction,
usage}`. Every member is core under the plan's own table, transitively, and
neither reaches `feature_run*` or `plan_graph*` at any depth. `coordinator_schema`
is likewise clean going *down* (`→ development_policy` only) — the problem in
C1 is one level further down, not with `coordinator_schema` itself.

**R3 — "`observability/` is a made-up layer; fold it into core."** Rejected on
evidence: `run_metrics → {audit}`, `run_metrics_index → {run_metrics}`,
`run_catalog → {audit, run_metrics}`, `dashboard_server → {audit,
run_catalog}` — a closed four-module cluster with a single edge into core and
zero in-package consumers. It is the cleanest cut in the whole plan and costs
one directory.

**R4 — "The whole program is unnecessary; the graph is already clean, so just
write the checker."** Tempting after C2 shrinks the real violation to one
function, but it does not hold. The `graph is clean` property is currently
unenforced and undocumented, and a checker keyed to 43 hand-annotated flat
module names (GR-01's design, line 80) is a table that rots on the first new
module. Directories make the layer derivable from the path, which is what
makes the contract mechanical rather than clerical. The layout earns its keep;
it is the *fix* inside it (GR-02) and the *scaffolding* around it (shims,
GR-06) that are oversized.

**R5 — "The tests couple layers, so the layer contract is fiction."** Checked:
several test files import both featurerun and plangraph symbols, but `tests/`
is outside the package, sits above `graphrun/` in the dependency order, and is
allowed to compose everything. No test-side coupling constrains the layout.

**R6 — "GR-03 is too big — 30 modules in one step — and should split."** It
should not. The 30 moves are mutually referential (the controller/executor
cluster is densely intra-connected), so any split leaves a half-moved core
with cross-directory edges that the checker must be told to tolerate mid-flight
— more special-casing than the big step costs. Serial + one whole-tree rewrite
+ full suite is the right shape here. GR-03 is the one step I would leave
exactly as sized.

---

## Verdicts

| Step | Verdict | Reason |
|---|---|---|
| GR-01 | REWORK | C3 (specified red can never go green), C1 (checker must see deferred imports), M3 |
| GR-02 | SHRINK | C2 — move `failing_identifiers` only; keep budget/authority in plangraph |
| GR-03 | KEEP | R6; minus shims (M1) |
| GR-04 | SHRINK | C5 — merge into GR-05; absorb the C1 `development_policy` decision |
| GR-05 | KEEP | absorbs GR-04; minus shims (M1); drop the no-op launch.json item |
| GR-06 | SHRINK | C4 — delete `runner_support`, defer explicitly; keep `__init__`, docs, checker closure |
| Shim policy | DELETE | M1 — zero consumers; M2 — active vacuous-green hazard |
