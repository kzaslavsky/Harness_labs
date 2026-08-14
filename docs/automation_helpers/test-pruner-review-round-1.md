# Test-Pruner Plan — Multi-Lens Review, Round 1

Status: closed (findings incorporated 2026-08-14)

Four independent Opus reviewers examined the draft plan and decomposition
(commit `bec472d`-era draft), each with one lens. All four verdicts were
variants of "restructure before executing". Every BLOCKER and MAJOR finding
was incorporated into the revised plan and decomposition in the same change
that adds this record; dispositions below.

## Lens verdicts

| Lens | Verdict |
| --- | --- |
| PlanGraph contract conformance | Structurally well-formed, but not executable: red terminal gate, launcher bootstrap paradox, thin criteria-to-gate binding. |
| Codex backend feasibility | Directionally sound but first-of-kind: two mechanical path/sandbox errors, launcher port understated, no all-codex PlanGraph precedent — smoke run required. |
| Mutation methodology | Reject as written: static-only deletion branch has a measured 100% FP rate on this repo; attribution unsound both directions; budget policy self-contradictory against ~20k mutation points. |
| Safety and economics | Restructure: graph inflation (6 nodes for 4 scripts), deletion-first contradicts the source analysis, no recovery story, metrics preached but not instrumented. |

## Dispositions

Consensus blockers (multiple lenses) → all fixed:

1. **Terminal functionality gate red at base** (contract #1, safety #1):
   replaced `unittest discover` with the committed `scripts/dev/tp_gate.py`
   (full suite minus announced conditional exclusions; verified green:
   478 tests, exit 0).
2. **Launcher bootstrap paradox** (contract #2, safety #2, backend #7): the
   graph-executing launcher `experiments/run_test_pruner_build_graph.py` is
   now human-owned PREWORK, committed before `prepare`, one-argument
   `launch(request)`, persisting its registration and recovery authority;
   node TP-04 keeps only the runtime wiring at a base-absent path.
3. **Oracle blind to subprocess-driven tests / zero-denominator candidacy**
   (methodology #2, safety #3): line-level `sys.settrace` attribution;
   subprocess-spawning or zero-trace tests hard-excluded as `out-of-scope`;
   selector requires `killable_in_traced_region >= floor` with zero kills
   reproduced twice; `mutants_run == 0` can never qualify.
4. **Static-only deletion branch, 100% measured FP rate** (methodology #1,
   safety #5): static flags demoted to advisory ranking input;
   `implicit-no-raise-oracle` and `skip-override` classifications pinned by
   tests against the four known real cases; quarantine-first is now the
   routine's only automatic action, deletion is operator-elected and
   calibration-gated.
5. **Budget self-contradiction vs ~20k mutation points** (methodology #3):
   named module allowlist with recorded exhaustiveness proof; seeded
   stratified sampling with recorded metadata if ever enabled;
   population estimate now in the plan.

Other majors → fixed: per-test kill semantics specified (no failfast,
setUp/import breakage = `error`) (meth #4); killable-denominator equivalent-
mutant screen and denominator-bearing report schema (meth #5, #9);
green-baseline preflight + reproduce-twice + `PYTHONHASHSEED` (meth #6);
operator set retuned for this codebase, string→"" replaced (meth #10);
real-suite calibration with operator-labeled precision@5 gate and
post-deletion mutation-score-preservation check (meth #7, #8); graph
collapsed 6→4 nodes, TP-01 owns both engine files (safety #4); recovery
runbook section with persisted registration, budget caps, abort rule
(safety #6, contract #9); live pilot removed from `tests/` — deterministic
fixture pilot only, model runs are operator-invoked (safety #7, contract #6);
`tests/fixtures/**` + pruner-own-tests versioned exclusion root (safety #8,
contract #6); metrics with denominators + run-frequency and open-candidate
budgets in the policy and emitted per run (safety #9); pilot report to
`logs/runs/` cited by digest, allowlisted environment fingerprint
(safety #10, contract #10); `tests/**` glob → prefix form everywhere
(backend #1); sandbox reframed as post-hoc detection with candidate-only +
human review as the boundary (backend #2); four spec constants driving
mixture + hand-wired seats, first-production-use of `build_role_profiles`
acknowledged (backend #3, #4); all-codex smoke run as a gating PREWORK step
(backend #5); `TEST_PRUNER_CODEX_EXECUTABLE`, explicit timeouts, reviewer
seat starts `@medium` (backend #6, #10); evidence tools run controller-side
via preflight, never on read-only codex seats (backend #9); runtime
verification chained via `verification_gates` at the FeatureRun API
(contract #4); TP-02's baseline snapshot in its `allowed_paths` with
gate-adjudicated drift check (contract #5); policy `Status:` line and schema
`$schema`/`$id`/`type` requirements named in the acceptance criteria
(contract #7); `created_by`/`producer_run_id` declarations added (contract #8).

Accepted with rationale (not changed): `experiments/` placement for the
runtime driver (recorded as a follow-up move to a routines package once the
routine proves out — contract #10a); the four-node shape still being
partially gate-technicality-driven (recorded as RISK 4 rather than papered
over — safety #4a).
