# GraphRun restructure plan — three-lens review, adjudication (2026-08-14)

Reviewers: three independent Opus agents (NECESSITY, FRAME, MECHANISM) against
`GRAPHRUN_RESTRUCTURE_PLAN.md` v1 at `5e99dda`. Reports in `necessity.md`,
`frame.md`, `mechanism.md`. The layout survived; most of v1's mechanics did
not. Verdicts are the coordinator's, with deciding evidence.

## Accepted — plan surgery

1. **GR-01's red test replaced (all three lenses, independently measured).**
   `import harness_labs.feature_run` loads six plangraph modules because
   `harness_labs/__init__.py:131,156` eagerly re-exports them and the parent
   package runs first — and GR-06 preserves those re-exports by design, so the
   v1 sys.modules probe is *permanently* red. Worse (MECH R4), annotating
   budget/authority as `core` in the checker would make GR-01 land zero
   evidence. v2 red: a static AST **import-closure** assertion on
   `feature_run`'s own imports — measured red now (`plan_graph_budget`,
   `plan_graph_authority` in closure), green after the boundary fix.
2. **A second, hidden violation enters scope (NECESSITY C1).**
   `development_policy` (core) imports `feature_run_policy` (featurerun) at
   `development_policy.py:107,115` — *deferred in-function imports* closing
   the cycle `feature_run_policy → coordinator_schema → development_policy →
   feature_run_policy`. v1's module-level AST walk never sees it; the checker
   must walk function bodies too, and the cycle gets its own red + fix.
3. **GR-02 shrunk from 986 lines to one function (NECESSITY C2).** FeatureRun
   consumes neither the retry ledger nor `gate_digest`; its only edge is
   `feature_run.py:38 from .plan_graph_budget import failing_identifiers` — a
   14-line pytest-output parser. `RetryBudgetLedger`, `gate_digest`, and
   `AutomaticRecoveryAuthority` are plangraph-only consumers. v2 moves
   `failing_identifiers` (+ regex) into core and leaves budget/authority in
   the plangraph layer under their own names. v1's "misnamed substrate" story
   is withdrawn.
4. **Shim policy deleted (NECESSITY M1/M2 + MECHANISM M1).** The star-import
   shim is mechanically broken: only 17/44 modules define `__all__`;
   MECHANISM built the rule-6 shim and measured `feature_run` exposing 10 of
   66 public names, breaking the exact runner imports
   (`PlanGraphFeatureRunBinding`, `RepairResumeDirective`) the shims existed
   to protect — plus 28 string-literal patch sites would silently patch shim
   copies (vacuous green). And the consumer audit found the shims serve
   nobody: all 13 in-repo importers are rewritten by the same steps; there is
   no installed package, no conftest coupling, no external script inventory.
   v2: no shims; every in-repo consumer (package, tests, `experiments/`,
   `scripts/`) is rewritten in the step that moves its target. (MECHANISM's
   `sys.modules`-alias construction is recorded as the correct shim *if one is
   ever needed* — it is not, today.)
5. **"Rewrite imports" becomes "rewrite imports and module-path strings"
   (FRAME C4).** Twelve modules are patched by string literal in tests
   (`@patch("harness_labs.<mod>...")`); `backends.py:53` journals
   `f"{__module__}.{__name__}"` (label-only — the changed recorded value is
   accepted and noted); `scripts/run_plan_graph.py:34` resolves user-supplied
   `module:callable`. Each move step's checklist includes the string sweep;
   the gate adds a grep for stale `harness_labs.<flat>` strings.
6. **`review_fix` moves to `featurerun/`, not core (FRAME C2).** Import
   direction (only `feature_run` imports it) and concept direction (CB3-06
   gave it "node writable paths", `origin_node`, cross-node transfer
   vocabulary) both point into the harness layer. Layering stays legal:
   featurerun imports core.
7. **`agent_mixture` moves to `graphrun/` (FRAME).** Nothing in-package
   imports it; it is the executor/session composition surface for program
   runners — the definition of the graphrun layer. v1's "core because
   executors use it" rationale was backwards.
8. **Observability's data-coupling documented as an accepted residual
   (FRAME C3).** `run_catalog` parses plangraph journal event shapes
   (`plan_node_id`, recovery-event names) while importing only core — real
   coupling no import checker can see. v2 adds a docstring contract note
   pointing at the journal event names it consumes; no structural change
   (the alternative — moving run_catalog into plangraph — would break its
   equally real featurerun-journal parsing).
9. **Checker mechanics concretized (MECHANISM M3/M4).** Phasing is
   tree-derived and flagless: each layer's rules hard-fail once that layer's
   directory exists — no step-state, no flags. `compileall` dropped (measured:
   exits 0 on broken imports); replaced by a smoke-import loop over every
   `harness_labs` module in the gate.
10. **Steps re-cut: six → five.** GR-04 merges into the cluster-move step by
    v1's own shared-rewrite-cost argument (NECESSITY; MECHANISM verified the
    clusters disjoint). `runner_support` is deferred outright — measured: the
    argparse duplication is real (84% identical) but `GATE_ADJUDICATED_CRITERIA`
    exists in exactly one file and "instruction-pin scaffolding" in zero;
    extraction waits until a third runner exists (rule of three).
11. **Branch bookkeeping fixed (FRAME C5).** The local `main` ref is stale
    (`c4d6111`); the integrated line is `Impl-redo`, pushed as `origin/main`
    (`1e9514a`). v2 names the base as `origin/main @ 1e9514a`, and adds a
    precondition: retarget the stale local `main` to `origin/main` (or delete
    it) so "nothing lands on main mid-restructure" governs the branch people
    actually use. Measured baseline corrected: 477 passed + 1 skipped, ~65 s.

## Rejected / noted

- FRAME's rule-5 redundancy note ("nothing imports graphrun" derivable from
  rules 1–4): kept anyway as an explicit statement — it costs one line and
  reads as intent, not mechanism.
- `.claude/launch.json` edit item deleted (it names a script path, not a
  module — MECH M5, NECESSITY); the real target is `scripts/run_dashboard.py`.
- All lens self-refutations accepted as recorded; notably: the 43-module
  mapping is complete, `observability/` is the cleanest cut, GR-03's
  30-module size is right, and the DeprecationWarning concern was unfounded
  (moot after surgery 4).
