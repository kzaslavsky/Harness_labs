# GR plan — FRAME lens (structure, boundaries, blast radius)

Reviewed: `docs/development/GRAPHRUN_RESTRUCTURE_PLAN.md` at `5e99dda`, tree
`graphrun-restructure`. All measurements taken in this worktree on the base
tree (moves not yet applied). Read-only review; no source touched.

Lens verdict in one line: **the layering is broadly right and the one named
boundary violation is real, but the plan's finding instrument (an AST import
checker) is blind to three of the four coupling channels that actually bind
these modules together, and GR-01's red test as specified can never go
green.**

---

## C1 — GR-01's red test (b) is unsatisfiable; it contradicts the compatibility policy

Plan line 83-89 defines the program's *recorded red* as: `import
harness_labs.feature_run` in a fresh interpreter must not load any
plangraph-layer module, and line 95 asserts "GR-01's red test goes green
here" at GR-02.

Measured, at base:

```
$ python3 -c "import sys, harness_labs.feature_run; \
  print(sorted(m for m in sys.modules if m.startswith('harness_labs.') and 'plan_' in m))"
['harness_labs.plan_approval', 'harness_labs.plan_graph',
 'harness_labs.plan_graph_audit', 'harness_labs.plan_graph_authority',
 'harness_labs.plan_graph_budget', 'harness_labs.plan_graph_contract']
```

Six modules, not two. The plan's diagnosis (`feature_run` →
`plan_graph_budget` → `plan_graph_authority`, plan:16-20) accounts for two of
them. The other four arrive by a completely different route: importing *any*
submodule executes the parent package `__init__`, and
`harness_labs/__init__.py:131` (`from .plan_graph import ...`) and
`:156` (`from .plan_approval import ...`) are eager, among 33 eager
`from .` statements re-exporting 166 names.

So GR-02 — which the plan calls the red-to-green step — removes the
`feature_run` → `plan_graph_budget` edge and the test **still fails**, because
`__init__` alone pulls the whole plangraph cluster. The cause is
over-determined and the plan sees only one half of it.

Worse, the two halves are in direct conflict. Fixing the `__init__` half
requires lazy top-level re-exports (PEP 562 `__getattr__`), but the
Compatibility policy (plan:115-117) says "`harness_labs/__init__.py` keeps
exporting the same names" precisely so package-level consumers are untouched.
That is not a move-and-rename; it changes `dir(harness_labs)`, import-time
side-effect ordering, and when the shims' `DeprecationWarning`s fire. It is
the one place in the program where "byte-identical logic" is false.

**Consequence:** GR-01's stated red is either unachievable, or GR-06 must
absorb an unscoped `__init__` laziness change that the plan classifies as a
non-change. Either the AC must be reworded (e.g. "no plangraph module is
reachable from `featurerun/*` module-level imports", checked statically, not
via `sys.modules`) or a lazy-`__init__` step must be added and costed.

## C2 — `review_fix` in `core/` fails the plan's own placement criterion

Plan:48-50 justifies the placement: "both are consumed by
executors/controllers generically; neither imports `feature_run`."

The second clause is true. The first is false for `review_fix`. Its only
in-package importer other than `__init__` is `feature_run.py:39`:

```
$ grep -rn "from .review_fix" harness_labs/*.py
harness_labs/__init__.py:164
harness_labs/feature_run.py:39
```

No executor, no controller, no kernel module imports it. `review_fix.py`
itself imports only core substrate (`attempts`, `audit`,
`controller_evidence`, `controller_results`, `git_transaction` —
review_fix.py:11-15), so the *checker* will be satisfied either way. But by
the criterion the plan actually states — "consumed by
executors/controllers generically" — `review_fix` belongs in `featurerun/`,
with a promotion to `core/` deferred until a second harness consumes it.

The CB3-06 semantics make this sharper, as the task framing anticipated.
`review_fix` now speaks PlanGraph node vocabulary in its own refusal text:

- review_fix.py:305 — `"finding file anchor is outside the node's writable paths"`
- review_fix.py:184-219 — `transfer_scope_expanding(..., origin_node=...)`,
  "Move eligible findings to their uniquely pre-bound downstream owner"
- review_fix.py:292-296 — comment: "Inherited via cross-node transfer"

Cross-node transfer to a pre-bound downstream owner is not substrate; it is
graph orchestration. Placing it in `core/` means `core/` is the home of the
module that encodes the plangraph node-grant model, while `plangraph/` does
not import it. The direction of the *concept* runs opposite to the direction
of the *import*.

Note the symmetric leak in the other direction: `feature_run.py:149`
declares `origin_node_id` and `:214` raises `"PlanGraph origin_node_id must
be a string"`. Rule 2 ("featurerun imports core only") will pass, and the
plan will claim FeatureRun standalone-ness is "a mechanically enforced
contract instead of a stated intention" (plan:8-9). What is mechanically
enforced is import hygiene. Conceptual standalone-ness remains a stated
intention, and the plan should say so rather than claim the stronger thing.

## C3 — `observability/` is import-clean and data-filthy; the checker cannot see it

Rule 4 ("observability imports core only") holds trivially:

- run_metrics.py:13 → `audit` only
- run_metrics_index.py:5 → `run_metrics` only
- run_catalog.py:15-16 → `audit`, `run_metrics`
- dashboard_server.py:17-18 → `audit`, `run_catalog`

Zero plangraph/featurerun imports. But `run_catalog.py` is a *parser of
plangraph and featurerun journal contracts*, and the coupling is dense:

- run_catalog.py:126, 207-209, 223-225 — snapshot families literally named
  `plan_graphs` / `feature_runs` / `ungrouped_feature_runs`
- run_catalog.py:239-256 — `kind == "plan_graph"` branch reading
  `plan_graph_digest`
- run_catalog.py:1044-1049 — dispatch on event types
  `plan_graph_child_recovery_blocked` / `plan_graph_child_seal_adopted` and
  payload key `plan_node_id`
- run_catalog.py:1183-1208, 1247-1268 — the correlation triple
  `{plan_graph_id, plan_node_id, parent_run_id}` validated as a closed set
- run_catalog.py:349 — projection keyed on `backend_transport` event type

`dashboard_server.py:175, 366-431, 499` reads the same families and emits
`"scope": "cumulative_plan_graph_node"`.

This is a genuine dependency of `observability/` on `plangraph/` — just
expressed through the journal schema rather than through `import`. The plan's
finding instrument "AST-walks the package" (plan:66-68) and will report the
layering clean.

**Does it matter?** Yes, but not as a blocker for this program — nothing
breaks at move time. It matters because the plan sells the checker as the
enforcement of the layering, and a reader will conclude that a future
plangraph journal rename is safe for observability. It is not: the coupling
is real, unversioned, and now *harder* to notice because the two clusters sit
in different subpackages. Minimum fix: state in the allowed-import contract
that rule 4 governs imports only, and that `observability/` additionally
depends on the journal event/field vocabulary of `plangraph/` and
`featurerun/`, with `run_catalog.py` named as the coupling site. Better fix
(out of scope, worth a follow-up): a shared journal-vocabulary constants
module in `core/` that both producers and `run_catalog` import, converting
data-coupling into import-coupling the checker *can* see.

## C4 — Module paths *are* embedded in journals and in operator commands

Plan:123 states flatly: "**Journals/evidence unaffected:** no journaled
payload embeds Python module paths as contract."

Two counterexamples.

1. `harness_labs/backends.py:49-53` journals a `backend_transport` event
   whose payload is
   `{"transport": "deterministic-python", "implementation": f"{type(self).__module__}.{type(self).__name__}"}`.
   That is a Python module path, written into the durable journal. GR-03
   moves `backends.py` to `core/`, so the same backend begins writing
   `harness_labs.core.backends.PoemBackend` where historical journals say
   `harness_labs.backends.PoemBackend`. Nothing currently asserts on the
   field (`run_catalog.py:349` keys on the event type, not `implementation`),
   so this is not a break — but the plan's claim as written is false, and any
   future differ or replay over mixed-vintage journals sees a value change
   that no changelog explains. GR-06's change log should name it.

2. `scripts/run_plan_graph.py:29-37` resolves `--launcher module:callable`
   through `importlib.import_module`. Operator command lines, docs, and
   saved invocations therefore contain `harness_labs.<flat>` strings that no
   AST tool can find or rewrite. They survive the restructure only for as
   long as the shims do — which makes the shim-retirement note in GR-06
   (plan:110-111) load-bearing in a way the plan does not acknowledge, and
   makes rule 5 ("nothing imports `graphrun/*`") unenforceable against
   dynamic dispatch: `--launcher harness_labs.graphrun.x:y` is invisible to
   the checker.

3. A third, in-repo instance the plan's "imports rewritten" scope misses
   entirely: tests patch by **string**, not by import.

```
$ grep -rho '"harness_labs\.[a-z_]*' tests/ | sort -u
"harness_labs.agent_mixture   "harness_labs.audit         "harness_labs.backends
"harness_labs.claude_task_executor  "harness_labs.codex_delegation
"harness_labs.controller_live  "harness_labs.controller_run
"harness_labs.dashboard_server  "harness_labs.feature_run
"harness_labs.plan_graph     "harness_labs.plan_graph_audit
"harness_labs.plan_graph_budget
```

Twelve distinct modules spanning every proposed layer, e.g.
`test_codex_delegation.py:58-59` patches
`"harness_labs.codex_delegation.shutil.which"` and
`"...subprocess.run"`. After GR-03 that string still *resolves* — through the
shim — but it patches the **shim module's** namespace, and the shim is
`from harness_labs.core.codex_delegation import *`, which does not re-export
`subprocess`/`shutil` when `__all__` is set and rebinds a distinct module
object when it is not. The real module goes unpatched, silently. Some of
these will fail loudly; the dangerous ones are those where the unpatched code
path is simply not exercised and the test passes vacuously.

**This is the single largest under-scoped item in the program.** "Imports
rewritten" (plan:96-97, 100, 103) must read "imports and module-path string
literals rewritten", and the GR-03 gate should include a grep assertion that
no `harness_labs.<flat_name>` string literal survives outside the shims
themselves.

## C5 — The base commit is not on `main`; the merge-collision mitigation aims at the wrong branch

Plan:4 — "**Base:** `main` at `1e9514a` (first published integrated tip)".
Plan:159-160 — "nothing else should land on `main` mid-restructure".

Measured:

```
$ git rev-parse --short main           -> c4d6111   ("Initialize Harness Labs repository")
$ git merge-base --is-ancestor 1e9514a main -> NO
$ git branch --contains 1e9514a        -> Impl-redo, graphrun-restructure
```

Local `main` is the initial commit and does not contain `1e9514a` at all. The
integrated line lives on `Impl-redo`. Whatever discipline "nothing lands on
`main`" is meant to buy, it buys nothing here — the collision surface is
`Impl-redo` (and whichever branch the concurrent CB-3 work targets). The plan
must name the actual integration branch, or the risk section is decorative.

This also makes the "few hours, one sitting" framing (plan:160) weaker than
stated: the constraint is not "freeze `main`" but "freeze the branch that
several other in-flight programs — CB-3 among them — are also merging into,"
which is a much harder promise. See M6.

---

## M1 — `agent_mixture` in `core/`: right answer, wrong reason, and `graphrun/` is the better home

Checked for plangraph/featurerun leakage as instructed. Code is clean:
`agent_mixture.py:19-31` imports `agent_sessions`, `audit`,
`claude_agent_session`, `claude_task_executor`, `codex_agent_session`,
`controller_evidence`, `controller_live`, `controller_scheduler`,
`git_transaction`, `usage` — all core-designated. The only plangraph and
featurerun mentions are prose: the module docstring ("per-role agent mixtures
for **FeatureRun** worker scheduling", :1; "a run (or a **PlanGraph node
packet**) names its mixture", :6) and :354. `RoleProfile` comes from
`controller_scheduler` (core), not from a harness. No role profile references
a feature-run concept structurally. **No leak.** The plan's placement is
defensible.

Its *stated* justification is not. "Consumed by executors/controllers
generically" (plan:48-49) is false: nothing in the package imports
`agent_mixture` except `__init__.py:26`. `controller_scheduler.py:83, 90`
mention it in docstrings only. Its real consumers are the runners
(`experiments/run_burden*_plan_graph.py:42-43`, `run_orbit_plan_graph.py:43`,
all importing `build_coordinator_session`).

A module that imports across four core clusters and is consumed only by
top-level runners is, by this plan's own frame, a **composition surface** —
i.e. exactly what `graphrun/` is defined to be (plan:45). Putting it in
`core/` is the safe call for a mechanical program; putting it in `graphrun/`
is the coherent one. Either is fine, but the note at plan:48-50 should be
corrected, because a false rationale will be cited later to justify a
placement that does not follow from it.

## M2 — "nothing imports `graphrun/*`" is a coherent rule for a *surface*, not for a *layer*

Rule 5 (plan:62) defines a package that may import everything and that
nothing may import. Inside a library that is a sink node with no consumers —
its only reachable callers are outside the package (`scripts/`,
`experiments/`, the operator). That is the definition of an **entry-point
surface**, and the honest structure is `bin/`, `scripts/`, or console entry
points, not a peer subpackage of `core/`.

Two concrete costs of keeping it as a package layer:

- It is unenforceable in the direction that matters. The checker can prove no
  *static* import reaches it, but `scripts/run_plan_graph.py:34`'s
  `--launcher module:callable` can dispatch into it dynamically (C4.2) and
  the checker sees nothing.
- It invites exactly the drift it is meant to prevent: the first time a core
  module wants `runner_support`'s argparse shape, the rule is the only thing
  standing in the way, and rules with no positive content ("this exists but
  may not be used") lose those arguments.

The counter-argument, which I find genuinely strong: `graphrun/` inside the
package is *importable by consumers who install the package*, whereas
`scripts/` is not. `runner_support` is library code — decompose/approve/
resume argparse shape, `GATE_ADJUDICATED_CRITERIA` wiring — that runners
should `import`, not copy. That is a real requirement and `scripts/` does not
meet it.

**Resolution I'd propose:** keep `graphrun/` as a package (the import
requirement is real), but drop rule 5's "nothing imports `graphrun/*`" as a
*checker rule* and restate it as what it actually is — rules 1-4 already
forbid every in-package import of `graphrun/`, since no other layer lists it
as permitted. Rule 5 is derivable, not independent, and stating it separately
creates the false impression that something enforces the *external* side.

**Where the `run_burden*` runners belong long-term:** the runners themselves
stay in `experiments/` — they are dated, program-specific artifacts (CB-2's
runner is a historical record, not maintained code, and CB-3's adjudication
item 8 treats "clone the previous runner" as the intended workflow). What
moves into `graphrun/runner_support` is only the part that is *the same
across programs*: argparse shape, gate-criteria wiring, instruction-pin
scaffolding. The dated program logic — node tables, ACs, pins — must stay in
`experiments/`, or the next program's runner will start by fighting the
abstraction.

## M3 — the `runner_support` duplication premise is overstated

Plan:45 cites the shared helpers as "currently duplicated across
`experiments/run_burden*_plan_graph.py`" and names three: argparse shape,
`GATE_ADJUDICATED_CRITERIA` wiring, instruction-pin scaffolding.

`GATE_ADJUDICATED_CRITERIA` appears in exactly one runner:

```
$ grep -rln GATE_ADJUDICATED_CRITERIA .
experiments/run_burden3_plan_graph.py
docs/development/contract-burden-reduction.md
docs/development/GRAPHRUN_RESTRUCTURE_PLAN.md
```

It is a CB-3-era construct, present in the newest runner only, and the CB-3
adjudication (item 8) records that the CB-3 runner did not yet exist at
review time. Extracting a shared helper from *one* instance is speculative
generality. The runners are 855/862/873/1043 lines and do share the argparse
shape, so the extraction is not baseless — but it should be scoped to what is
demonstrably duplicated (>=2 instances), and the plan should not cite the
one-instance construct as evidence of duplication. Plan:53-54 already flags
this as "the only non-mechanical scope" and deferrable; I'd go further and
**default it to deferred**, with GR-06 landing only `graphrun/__init__`.

## M4 — `.claude/launch.json` needs no update

Plan:125 — "`.claude/launch.json` configs updated in GR-05."

```
$ cat .claude/launch.json
... "runtimeArgs": ["scripts/run_dashboard.py", "--audit-root", ..., "--port", "8321", ...]
```

It invokes a script path, not a module path; it contains no `harness_labs.`
string. `scripts/run_dashboard.py:11` holds the only import
(`from harness_labs.dashboard_server import ...`) and that file is already
inside the GR-05 rewrite scope. Harmless, but it is a claimed work item that
does not exist — and its presence suggests the compatibility surface was
enumerated from memory rather than by grep, which is the same failure mode
that produced C4 and C5.

## M5 — measured-baseline drift

Plan:75-77 cites "477 tests at base". Measured: `python3 -m pytest tests/ -q
--collect-only` → **478 tests collected**. One test off. Trivial in itself,
but the plan uses this number as the zero-behavior-change reference for six
consecutive gates; if the operator diffs against 477 at GR-01 they will chase
a phantom. (Suite wall time not re-measured; the ~61 s figure is consistent
with the CB-3 adjudication's measured 61.2 s.)

## M6 — what "supervised refactor" misses that the CB machinery would have caught

The plan's execution-mode argument (plan:129-138) is sound on its own terms
and I largely agree with it: write-grant enforcement degenerates when every
step's writable set is the whole tree, and parallel dispatch is inert on a
strictly serial program. Reasons 1 and 3 are correct.

Reason 2 — "red/green gating adds ceremony, not evidence" for GR-03..GR-06 —
is where the argument overreaches, and C1 and C4.3 are the proof. Three
failure classes survive "boundary checker + full suite, zero behavior change":

- **Vacuous-pass drift.** A string-patch target that silently stops patching
  (C4.3) leaves the suite green while the assertion has become a no-op. A
  full suite proves "nothing turned red", never "everything that was
  meaningful stayed meaningful". A red/green discipline would have forced
  each step to demonstrate a failing state first; the CB programs' review
  stage would have put a second reader on the diff. Neither is present here.
- **Unsatisfiable-AC drift.** GR-01's red (C1) is stated in the plan and is
  unachievable. A PlanGraph decomposition would have hit this at the finding-
  test authoring step, before any code moved. A supervised refactor hits it
  when the operator writes `test_import_boundaries.py`, discovers the test
  cannot pass, and — under one-sitting time pressure — is tempted to weaken
  the assertion to match reality rather than to escalate. That is precisely
  the failure the CB adjudications repeatedly caught (e.g. CB3-05's vacuous
  AC, CB3-01's already-landed feature).
- **Shim-fidelity drift.** Rule 6 checks that a shim contains *only* the
  re-export; nothing checks that the re-export is *complete*. `import *`
  respects `__all__` and skips underscore-prefixed names — so a consumer of
  `harness_labs.feature_run._something` (or of a name absent from a
  hand-written `__all__`) breaks, and only if a test happens to exercise it
  does the suite notice. A cheap, high-value addition to the GR-03/04/05
  gates: for each shim, assert `set(dir(old_module)) >= set(public names of
  new module)` — a generated test, not a hand-written one.

On the one-sitting constraint: it is **not realistic as stated**, for the
reason in C5 — the branch that must be frozen is the shared integration line
(`Impl-redo`), not `main`, and CB-3 is in flight against it. The realistic
mitigation is not a freeze but a **rebase-friendly step shape**: because every
step is a pure rename plus mechanical import rewrite, a concurrent change on
the integration branch can be replayed by re-running the same rewrite, not by
resolving conflicts by hand. That should be written down as the recovery
procedure. If it is violated without such a procedure, the failure mode is
ugly: a mid-restructure merge produces a tree where some files import
`harness_labs.core.x` and others `harness_labs.x`, both resolve (shims!), the
suite passes, and the boundary checker — which derives layer from *path* —
reports clean because both paths exist. The shims that provide the
compatibility guarantee are the same mechanism that hides an incomplete
merge.

---

## Self-refutations

- **On C2.** My "sole consumer" argument proves less than I claimed. By the
  same test, `development_policy` and `coordinator_schema` would need
  auditing, and I did not run it on them — I criticized the plan's criterion
  while applying it selectively. And there is a real cost to moving
  `review_fix` to `featurerun/`: the moment a second harness wants
  review/fix, it must move *back*, and a move back is more expensive than
  leaving it. If the author's actual (unstated) reason is "review/fix is
  substrate we intend to reuse", that is a legitimate forward-looking call
  and my objection reduces to "say that instead". I stand by the vocabulary
  half of C2 (`origin_node`, "the node's writable paths" in `core/`) and
  soften the placement half to: **fix the stated rationale; placement is a
  judgment call the author may keep.**

- **On C3.** I called the data-coupling a defect of the checker, but no
  formulation of a static import checker could see it, and the plan never
  claims otherwise — it claims the checker enforces the *import* contract,
  which it does, exactly. My real complaint is a documentation gap, not a
  design flaw, and I initially framed it as more damaging than it is. It
  blocks nothing in GR-01..GR-06.

- **On C4.1.** The `backends.py:53` `implementation` field is journaled but
  nothing reads it — I verified `run_catalog.py:349` keys on `event_type`
  alone, and no test asserts on the value. So the plan's claim is false as
  *stated* ("no journaled payload embeds Python module paths") but true in
  the sense it *meant* ("no journaled module path is load-bearing"). This is
  a wording fix, not a blocker. C4.3 (the test string-patches) is the part
  that carries real risk, and I have folded them together under one heading
  in a way that overstates 1 and 2 by association.

- **On M2.** I argued `graphrun/` should be an entry-point surface, then
  talked myself out of it on the importability requirement, and ended up
  endorsing the plan's structure with a weaker rule. The plan's instinct was
  right and my objection collapsed to a bookkeeping point about rule 5 being
  derivable. Recorded as such.

- **On C5.** It is possible `main` is simply a stale local ref and the
  author means the GitHub `main`, which may well contain `1e9514a`. I could
  not verify the remote from this worktree. If so, C5's factual half is void
  — but its substantive half survives: the branch to freeze must be named
  explicitly, and "a few hours in one sitting" is a scheduling assumption,
  not a mitigation.

- **Lens bias.** FRAME is structurally biased toward finding under-specified
  boundaries, and I found six. The honest summary is that the plan's *layout*
  is good — the module-to-layer table (plan:38-45) survived every direction-
  of-dependency check I ran except `review_fix`'s rationale, and the one
  claimed boundary violation is real and correctly diagnosed. My findings
  cluster in the *instrumentation and compatibility* sections, not in the
  layering itself.

---

## Per-step verdicts

| Step | Verdict | Blocking condition |
|---|---|---|
| GR-01 | **Rework** | Red test (b) unsatisfiable (C1). Restate as a static reachability assertion over `featurerun/*` module-level imports, or add a lazy-`__init__` step. Fix the 477/478 baseline (M5). |
| GR-02 | **Accept, with C1's consequence noted** | The rename is correct and the boundary fix is real. It does *not* turn GR-01's red green; say so. |
| GR-03 | **Rework — scope** | Must include module-path **string literals** (12 modules, C4.3) and a shim-completeness assertion (M6). "Imports rewritten" is not sufficient scope. |
| GR-04 | **Accept** | Two modules, clean. Correct the standalone-ness claim to "import-level" (C2). |
| GR-05 | **Accept, minor** | Drop the `.claude/launch.json` work item (M4); document the observability↔plangraph journal coupling in the contract (C3). |
| GR-06 | **Rework — scope** | Defer `runner_support` by default (M3); restate rule 5 as derivable (M2); change log must name the `backends.py:53` journal-value change (C4.1); shim-retirement note must acknowledge `--launcher` string dependence (C4.2). |
| Execution mode | **Accept the recommendation, reject one premise** | Supervised refactor is right (reasons 1, 3 hold). Reason 2 understates what red/green + review catch (M6). Name the real integration branch and write the rebase-replay recovery procedure (C5). |
