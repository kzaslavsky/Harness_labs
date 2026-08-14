# CB plan review — lens findings and adjudication (2026-08-12)

Three independent Opus 5 lenses (FRAME, NECESSITY, MECHANISM) reviewed the
plan, decomposition, runner, and gate script at commit `4bf9603`. Full reports:
`frame.json`, `necessity.json`, `mechanism.json` in this directory. Every
load-bearing claim below was re-verified against source before acceptance.

## Accepted — critical

1. **[MECH] `logs/plan-approval` untracked → base dirty → first node launch
   fails.** Fixed: `.gitignore` now covers `logs/plan-approval/` and
   `logs/registration/`.
2. **[MECH] Default run-id (`%Y%m%dT%H%M%SZ`) violates
   `^[a-z0-9][a-z0-9-]+$`.** Fixed: lowercase default + argparse validation.
3. **[MECH] Nodes ran under `verification_repair_limit=1` — the very defect the
   program indicts.** Fixed: explicit `verification_repair_limit=3`.
4. **[FRAME] CB-05 had `tests/test_feature_run.py` as a regression target it
   could not edit** (the frozen-ownership defect reproduced). Fixed: added to
   allowed_paths, plus `agent_mixture.py`/`tests/test_agent_mixture.py` (third
   `allow_dirty_baseline` construction site).
5. **[NEC] CB-03's new `environment_retryable` class would crash the RB ledger**
   (`_CLASS_LIMITS` is closed; unknown classification → `BudgetError` →
   `PlanGraphError`). Fixed: CB-03 rewritten to extend the existing
   `infrastructure_transient` class with structured-evidence rules; no new
   vocabulary.
6. **[NEC/MECH] The RB ledger was inert — `FeatureRunOutcome.evidence` never
   set, so `import_child_evidence` never ran** (true of the orbit run too).
   Fixed: runner now passes structured verification evidence; run root is
   lineage-stable (`logs/runs/cb-graph`) so budget state survives attempts;
   registration is persisted for `scripts/plan_graph_recover.py`.

## Accepted — material

7. **[NEC+FRAME+MECH] `red_green_check` accepted any nonzero exit as red**
   (ImportError = free pass; CB-07 was structurally guaranteed to exploit it).
   Fixed: red now requires pytest exit 1, ≥1 FAILED, 0 errors; timeouts yield
   JSON verdicts; per-phase timeout passed explicitly (700s × 2 < 1800s node
   ceiling).
8. **[FRAME] Criterion-source enum duplicated in
   `controller_coordinator.py` tool schema** — kernel-only fix accepts a value
   no coordinator can send. Fixed: CB-01 owns both sites.
9. **[FRAME+NEC] CB-07 covered one of two executor boundaries and didn't own
   the shared schema** (`_RAW_OUTPUT_SCHEMA` in `controller_live.py`,
   `validate_semantic_result` in `controller_results.py`). Fixed: CB-07 owns
   the shared boundary; module-path pin removed from its criteria.
10. **[NEC] CB-04 duplicated budget accounting instead of riding the ledger's
    `_failure_keys` substrate.** Fixed: CB-04 owns `plan_graph_budget.py`;
    criteria rebound to the ledger substrate; separate hard-cap criterion
    dropped in favor of the existing loop bound.
11. **[NEC] CB-05's grant was vacuous** (writable-paths disjunct ⊇ receipt
    coverage). Fixed: coverage is the receipted change set alone; AC-CB05-2 now
    explicitly refuses receipt-uncovered dirty paths even inside writable
    paths.
12. **[FRAME] Fake serial chain; no dependency justification in the plan.**
    Fixed: honest DAG (CB-02 fully parallel-eligible; CB-06 ∥ CB-07) with a
    dependency section in the plan.
13. **[FRAME+MECH] Narrow green regressions vs node blast radius; terminal
    full-suite check unownable.** Fixed: every node's green phase runs the full
    suite (~40s measured).
14. **[FRAME] Program leaves its own workarounds in place; diagnosis statuses
    unowned.** Fixed: new CB-08 node retires the runner pins and closes
    diagnosis statuses, gated by `scripts/dev/check_workaround_retirement.py`.
15. **[MECH] Recovery claim not wired** (no persisted registration, per-attempt
    ledger root). Fixed as in item 6; node-level `RecoveryAgent` remains
    unwired and is recorded as an accepted gap in plan rule 3.

## Accepted — minor

16. **[NEC] AC-CB01-4 oversized**: reworded to widen provenance validation to
    journal-resolvable refs instead of minting a new evidence namespace.
17. **[MECH] Gate timeout inconsistency** (2×1500s inside 1800s): fixed with
    explicit `--timeout 700`.
18. **[MECH] python3 pinned by sha256+mtime at approval**: accepted constraint,
    documented in program acceptance (approve and run in one session).
19. **[MECH] Same-run-id relaunch collides on worktree/branch**: documented in
    program acceptance.

## Rejected / partially rejected

- **[NEC] "CB-06 addresses a dead-end the shipped harness does not create"** —
  partially rejected. The lens cited the standard schema's verify segment, but
  the plan-graph **bound** path (`run_plan_graph_feature_worktree`) strips the
  schema to a single implement segment whose instructions prohibit
  verification-only dispatch (`feature_run.py` bound segment). The dead-end is
  real on that path for gate-phrased criteria. Accepted residue: CB-06 is now
  explicitly scoped to the bound path.
- **[NEC] "Preservation criteria (AC-CB02-2) add red/green ceremony"** —
  rejected as a change driver. Program rule 1 requires red/green for finding
  tests, not for every criterion; preservation criteria are covered by the
  full-suite green phase. Wording clarified in rule 1.
- **[MECH] "Red-phase attribution unsound because candidates stack"** —
  partially rejected. The red claim is "the frozen base harness lacks this
  behavior," which stacking does not undermine; the FAILED-not-ERROR rule now
  rejects the cross-node `AttributeError` contamination that made the concern
  concrete.
