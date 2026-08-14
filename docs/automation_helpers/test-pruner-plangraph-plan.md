# Useless-Test Pruner — PlanGraph Build Plan

Status: revised after multi-lens review round 1, 2026-08-14
Decomposition: `docs/automation_helpers/test-pruner-decomposition.json`
(protocol `plan-graph-plan/1`)
Review record: `docs/automation_helpers/test-pruner-review-round-1.md`

## OVERVIEW

Build the useless-test pruner — the routine selected in
`automation-routines-analysis.md` §4 — as a PlanGraph-developed,
FeatureRun-hosted maintenance routine. Two goals, both explicit:

1. **The routine**: deterministic evidence tools
   (`scripts/dev/mutation_check.py`, `scripts/dev/mutation_score.py`,
   `scripts/dev/vacuous_test_check.py`, `scripts/dev/test_pruner_select.py`)
   plus an all-Codex FeatureRun wiring
   (`experiments/run_test_pruner_feature.py`) that turns a pruner report into
   a bounded, **quarantine-first**, human-reviewed candidate branch.
2. **Dogfooding**: this is deliberately GraphRun operating on itself with a
   full Codex-exec seat fleet — the first all-codex PlanGraph run. The graph
   is kept at four nodes; the parts that only a human should own (launcher,
   gates, calibration labels, attestation) are pre-work, not nodes.

The routine's predicate, hardened per review round 1: a test is a
**quarantine candidate** only when the mutation evidence affirmatively
demonstrates vacuity — traced line-level attribution, a non-zero killable
mutant denominator, zero kills, reproduced twice — and static vacuity flags
are advisory ranking input only, never sufficient alone. Deletion is a
separate, operator-elected second pass gated on calibration (see RUNBOOK).
Models never select candidates; they only execute the bounded quarantine
edit and draft the explanation.

Contract bindings inherited from `AGENTS.md`: bounded work only, no
generalized frameworks, every claim bound to a verifiable artifact, and one
accountable integration owner.

### Environment facts the plan depends on

- Python 3.11 stdlib only; **no pytest, coverage, mutmut, or cosmic-ray**
  here; repo convention is dependency-free tooling
  (`scripts/dev/red_green_check.py` precedent). The mutation engine is built
  on stdlib `ast` + `unittest`; per-test attribution uses stdlib
  `sys.settrace` line tracing (upgrade path: `sys.monitoring` on 3.12+).
- `tests/test_relax_gate_timeout_classification.py` (3 tests) is red on any
  host without pytest, because it exercises `red_green_check.py`, which
  shells out to pytest. The committed gate wrapper `scripts/dev/tp_gate.py`
  runs the full suite minus conditionally-excluded modules, announcing every
  exclusion; it is green at base (478 tests, 2 skips) and is the graph's
  functionality gate. Node verification argv use explicit module lists.
- Suite wall-time is ~40 s, but that is the *multiplicand*: the repo has
  roughly **20,000 classical mutation points** (AST count across
  `harness_labs/`; `plangraph/plan_graph.py` alone ~1,600, ≥1.5 h to mutate
  exhaustively at the measured 3.4 s scoped-run cost). Exhaustive repo-wide
  mutation is out of reach; the methodology below is built around that fact.
- ~30 of 58 test files drive behavior through `subprocess` (18
  `sys.executable` call sites); in-process tracing cannot see them. They are
  structurally out of scope for the pruner and must be reported as such.

## METHODOLOGY

The measurement core, incorporating the methodology-lens findings:

- **Attribution** is per-test, line-level, via `sys.settrace` scoped to
  `harness_labs/`: each test method's exercised-line set is recorded in a
  fresh child process per test module. Tests whose trace records zero
  in-process target-package lines, or whose AST contains a
  subprocess/spawn/`os.system` call, are marked
  `attribution: out-of-scope` and are **permanently ineligible** — reported,
  never candidates, and never described as "survived".
- **Scope**: the first build targets a named module allowlist chosen at
  TP-01 time by measured mutation-point count, with the exhaustiveness proof
  (population count, wall-clock) recorded in the report. Initial proposal:
  `harness_labs/core/attempts.py`, `harness_labs/core/composition.py`,
  `harness_labs/core/usage.py`. Everything outside the allowlist is
  `out-of-scope` by construction. If sampling is ever enabled it must be
  seeded, uniform, stratified per function, recorded as
  `sampling_method`/`seed`/`sampling_fraction`, and mutants must intersect
  the test's traced lines — otherwise `insufficient-evidence`.
- **Kill semantics**: one mutant run executes the full scoped selection with
  **no failfast**; per-test kill = that test's own failure; a mutant that
  breaks import/collection/`setUp` classifies the whole run `error` and is
  excluded from every test's denominator.
- **Denominators**: a per-mutant `killable` flag (killed by at least one
  test in the scoped suite) screens equivalent mutants. A test's denominator
  is killable mutants intersecting its traced lines; candidacy requires
  `killable_in_traced_region >= floor` (floor > 0, recorded), zero kills,
  `baseline_status == pass`, and the zero-kill verdict reproduced across two
  independent runs (`PYTHONHASHSEED` pinned; disagreement ⇒ `flaky` ⇒
  disqualified).
- **Baseline**: a pre-flight run of the scoped selection; failing or skipped
  tests are excluded from candidacy with `baseline_status` recorded
  (a failing test fakes kills; a skipped test fakes vacuity).
- **Operator set**: comparison/boolean/arithmetic swaps, boundary off-by-one,
  `return`/`raise`/`continue` statement deletion, **call-statement deletion**
  (side-effect removal — the operator matching this codebase's
  journal/checkpoint/guard logic), exception-type swap, return→`None`, and
  constant perturbation with string literals substituted by *another literal
  from the same module* (status-string confusion) rather than `""` (crash
  bait). Kill rates are reported per operator class so trivially-killable
  classes can be discounted.
- **Static detector (advisory only)**: flags assert-free, tautological, and
  mock-neutralized tests as *ranking input*; a static flag alone never
  qualifies a candidate. The detector must classify bare-invocation
  *must-not-raise* tests (e.g. the three in `tests/test_relax_plan_gates.py`)
  as `implicit-no-raise-oracle` and `skipTest` overrides as `skip-override`,
  neither of which is `vacuous`. Its committed baseline snapshot records the
  current suite's flag population with a published false-positive count —
  not a laundering allowlist.
- **Report schema** (`test-pruner-report/1`): per-test record with
  `attribution_method`, `attribution_confidence`, `exercised_line_overlap`,
  `mutants_eligible`, `mutants_killable`, `mutants_sampled`, `mutants_run`,
  `mutants_killed`, per-operator-class kills, `sampling_method`/`seed`/
  `sampling_fraction`, `module_suite_mutation_score`, `baseline_status`,
  statuses, budgets, an allowlisted environment fingerprint (Python version,
  platform tuple, target commit, budget values — no absolute paths,
  usernames, or environment dumps), and the target commit SHA. Generated
  explanation text is constrained to the defensible claim ("killed 0 of N
  killable mutants intersecting its traced lines; attribution X; sampling
  Y"), never "this test is useless".

## BACKENDS

All build-time and runtime model seats run on **Codex exec**. The single
exception is the human-side operator loop (plan authoring, pre-work,
attestation, calibration labels, PR review), performed by the Claude session
driving this repository — an operator role, not a run backend.

Reality check (backend lens): this is **first-of-kind integration**, not a
constants swap. `experiments/run_burden3_plan_graph.py` hand-builds Claude
executors in three places (implementer `:387-429`, review/fix `:500-517`,
verification repair `:553-565`) with `effort=`, which
`CodexSemanticTaskExecutor` does not accept (it takes `reasoning=`,
`harness_labs/core/controller_live.py:440`); the mixture layer
(`harness_labs/graphrun/agent_mixture.py`) has no production caller yet; and
no codex run has exercised the PlanGraph-bound path, controller-owned
verification, or verification repair. The pre-work smoke run exists to
retire exactly that risk before six seats of spend.

Mechanism:

- Four module-level spec constants drive everything: `COORDINATOR_SPEC`,
  `IMPLEMENTER_SPEC`, `REVIEWER_SPEC`, `REPAIR_SPEC`, all
  `codex:gpt-5.6-terra@{reasoning}`. Worker seats route through
  `build_role_profiles` (`agent_mixture.py:184` — its first production use);
  the reviewer and repair seats are hand-wired factories (they do not pass
  through the mixture layer) but are constructed from the same constants.
- Coordinator: `build_coordinator_session(COORDINATOR_SPEC, ...)`
  (`agent_mixture.py:342` → `CodexAppServerSession`) with an explicit
  `timeout_seconds >= 600` (the 180 s default is per-message and too low —
  burden3 uses 600 for the analogous seat).
- The codex binary path comes from a required `TEST_PRUNER_CODEX_EXECUTABLE`
  environment variable, threaded to both `build_coordinator_session
  (executable=...)` and `build_role_profiles(executables={"codex": ...})`.
  No binary is present on this container; the smoke run is the first check.
- Evidence tools (mutation engine, detector, selector) are **never run by
  model seats**. They run controller-side via the `preflight_argv` +
  `require_preflight_success` mechanism (`controller_live.py:586-606`,
  trebuchet precedent), which executes outside the codex sandbox and hands
  the seat `controller_verified_command` evidence. Read-only codex seats are
  not asked to perform temp-tree writes.
- **Path confinement is post-hoc detection, not prevention**: codex receives
  `--sandbox workspace-write` with no writable-root configuration
  (`controller_live.py:617-644`); `writable_paths` is prompt guidance plus a
  git-delta check after exit (`:683-692`). The quarantine lane's real safety
  boundary is `candidate_only=True` plus human review. Writable-path grants
  use **prefix form** (`tests`, or the explicit candidate file list) — the
  path layer does pure prefix matching and a `tests/**` glob matches nothing
  (`harness_labs/core/git_transaction.py:60-88`).

Seat map (initial; revisit after the smoke run):

| Seat | Spec | Timeout |
| --- | --- | --- |
| Node coordinator | `codex:gpt-5.6-terra@high` | 600 s/message |
| Implementer workers | `codex:gpt-5.6-terra@medium` | 900 s |
| Reviewer workers | `codex:gpt-5.6-terra@medium` (escalate to `@high` only if the smoke run shows misses) | 600 s |
| Verification-repair executor | `codex:gpt-5.6-terra@medium`, `verification_repair_limit=1` | 900 s |

`allow_dirty_baseline` stays `False` on every lane.

## PREWORK

Human-owned, committed **before** `approve_plan.py prepare` (the subject
must byte-match the base commit, and several of these are load-bearing for
the gates):

1. `scripts/dev/tp_gate.py` — deterministic suite gate: full discovery minus
   conditionally-excluded modules (announced), used as
   `functionality_tests[0]`. **Committed with this plan; verified green at
   base (478 tests, 2 skips, exit 0).**
2. `experiments/run_test_pruner_build_graph.py` — the graph-executing
   launcher: exposes a **one-argument** `launch(request)` returning
   `FeatureRunOutcome` (`scripts/run_plan_graph.py:243-249` calls it with
   exactly one argument; burden3's two-argument `_launch_node` cannot be
   used directly), reads acceptance criteria from the validated receipt,
   drives `PlanGraph(..., max_parallelism=2, logical_graph_id=...)`,
   **persists the registration** (the `--approval-receipt` path of
   `run_plan_graph.py` never calls `persist_registration`, which would
   foreclose `scripts/plan_graph_recover.py`), and registers
   resume/extend-budget recovery authority as burden3 does. All seats from
   the four spec constants. This file is *distinct* from TP-04's runtime
   wiring and is never touched by any node.
3. **Smoke run**: one trivial PlanGraph-bound FeatureRun (single node,
   touch-a-file objective, failing-then-repaired gate) through that
   launcher, all-codex. Gates: codex binary resolves; coordinator tool loop
   completes; `reasoning=` threading; review-ledger guards; verification
   repair fires once. Recorded under `logs/runs/<run-id>/`. No graph
   execution before this is green.
4. Re-run `approve_plan.py prepare` after pre-work lands (any commit changes
   the subject hash).

## GRAPH

```
TP-01 (mutation measurement engine) ─┐
                                     ├─▶ TP-03 (selector + policy + metrics) ─▶ TP-04 (runtime wiring + deterministic pilot + calibration harness)
TP-02 (static vacuity detector) ─────┘
```

Four nodes (down from six after the safety-lens review: the TP-01/TP-02
seam existed only to satisfy the approval gate's base-commit `modify` rule,
and the old TP-05/TP-06 mixed human-owned work into model nodes). TP-01 and
TP-02 are independent; the pre-work launcher runs them in parallel lanes
(`max_parallelism=2`).

## TP-01 — Mutation measurement engine

Create `scripts/dev/mutation_check.py` (enumeration: deterministic
stdlib-`ast` mutant generation, stable IDs `<module>:<line>:<col>:<operator>`,
METHODOLOGY operator set, refusal to mutate outside the target root) and
`scripts/dev/mutation_score.py` (execution: per-test `sys.settrace`
attribution in fresh processes, temp-tree materialization — never worktree
writes — full-selection no-failfast runs, killable-denominator computation,
baseline pre-flight, reproduce-twice, budgets with `budget-exhausted`
status), plus `schemas/test-pruner-report.schema.json` (draft-2020-12
`$schema`, unique `$id`, `"type": "object"` — `check_repository_contracts.py`
enforces all three) and tests for both modules. One node owns both files —
no cross-node API seam.

## TP-02 — Static vacuity detector (advisory)

Create `scripts/dev/vacuous_test_check.py` + tests +
`tests/fixtures/vacuous_baseline.json` (the committed baseline snapshot;
declared in this node's `allowed_paths` so the gate can bind it). Must
classify `implicit-no-raise-oracle` and `skip-override` correctly — the
node's tests pin the four known real cases from this repo's suite — and its
own verification runs the detector over `tests/` and diffs against the
snapshot, so AC-DET-1 is gate-adjudicated, not worker-claimed.

## TP-03 — Selector, policy, and metrics

Create `scripts/dev/test_pruner_select.py`: merges mutation report + static
flags into **quarantine** candidates under machine-checked hard
disqualifiers (traced attribution only; `killable_in_traced_region >= floor`;
zero kills reproduced twice; `baseline_status == pass`; no
timeout/error/budget-exhausted contamination; `mutants_run == 0` or
out-of-scope attribution ⇒ never a candidate), capped at `--max-candidates`
(default 5), with the pruner's own tests and `tests/fixtures/**` as a
versioned exclusion root enforced in code and pinned by a test. Create
`docs/automation_helpers/test-pruner-policy.md` (with a `Status:` line —
contract-checked): predicate, budgets (≤5 candidates/run, ≤1 run/week,
≤2 open candidate branches), quarantine-then-delete lifecycle (deletion only
after N green days *and* the calibration gate), reviewer-of-record, metrics
with denominators (candidates proposed/accepted/rejected, escaped defects,
suite wall-time delta), and the runtime metric record emitted per run into
the observability sink. Create `tests/test_pruner_select.py`.

## TP-04 — Runtime wiring, deterministic pilot, calibration harness

Create `experiments/run_test_pruner_feature.py`: runs the evidence tools
controller-side (preflight), then dispatches one `run_feature_worktree(...)`
(`harness_labs/featurerun/feature_run.py:567`) whose codex-exec implementer
receives the selector report and applies only the quarantine edits
(writable prefix `tests`), with
`verification_gates=(("suite", tp_gate), ("contracts", check_repository_contracts))`
— `verification_gates` is the correct chaining mechanism at the FeatureRun
API (`feature_run.py:609-611` makes it exclusive with `verification_argv`;
the committed decomposition's schema constraint applies only to the
decomposition document, not runtime code) — `candidate_only=True` always, a
repair factory present, and the metric record emitted. A `--plan-graph`
variant goes through `run_plan_graph_feature_worktree` (`feature_run.py:1141`),
which **rejects** controller-owned keys (`allowed_paths`,
`verification_argv`, `candidate_only`, … — `:1195-1213`) — those flow
through `PlanGraphFeatureRunBinding` instead — and demands the full
seven-phase schema, all six review-ledger guards, and a repair factory
(`:1177-1223`).

Create `tests/test_pruner_runtime_config.py` (asserts over the spec
constants via `parse_backend_spec(...).backend_id == "codex-exec"`,
`RoleProfile.backend_id`, the launcher's options mapping — `candidate_only`,
prefix-form writable paths, `allow_dirty_baseline=False` — **without ever
invoking an executor factory**, so no codex binary or network is needed),
`tests/fixtures/test_pruner_pilot/` (planted corpus: two genuine tests, one
assert-free, one tautological, one mutation-vacuous) and
`tests/test_pruner_pilot.py` — **fully deterministic**: enumeration →
scoring → selection over the fixture, asserting the candidate set is exactly
the planted vacuous tests, then *mechanically applying* the candidate list
and asserting the fixture suite still passes. No model invocation lives in
`tests/`; the live codex quarantine run is an operator runbook step. Also
create `scripts/dev/tp_calibration.py`: runs the full pipeline over the
TP-01 module allowlist, emits the top-20 ranked candidates for operator
labeling, and computes **precision@5** against the labels.

## RUNBOOK

```sh
# 0. PREWORK (human-owned): tp_gate.py committed green; author + commit
#    experiments/run_test_pruner_build_graph.py; run the all-codex smoke
#    FeatureRun; only proceed when smoke is green.
export TEST_PRUNER_CODEX_EXECUTABLE=/path/to/codex

# 1. Freeze and gate the subject (decomposition committed at HEAD)
python3 scripts/approve_plan.py prepare \
  docs/automation_helpers/test-pruner-decomposition.json \
  --repository . --output-directory /tmp/tp-approval

# 2. Operator attestation (schemas/plan-operator-approval.schema.json).
#    Required statement template — the operator attests to what they can
#    check: base commit SHA; tp_gate green locally with the announced
#    exclusion of tests/test_relax_gate_timeout_classification; smoke-run
#    run-id; quarantine-only mode; reviewer-of-record by name.

# 3. Issue the receipt
python3 scripts/approve_plan.py issue --repository . \
  --subject /tmp/tp-approval/subject.json \
  --gate-evidence /tmp/tp-approval/gate-evidence.json \
  --operator-approval /tmp/tp-approval/operator.json \
  --receipt /tmp/tp-approval/receipt.json

# 4. Run the graph through the PRE-COMMITTED build launcher
python3 scripts/run_plan_graph.py run --repository . \
  --approval-receipt /tmp/tp-approval/receipt.json \
  --decomposition docs/automation_helpers/test-pruner-decomposition.json \
  --graph-attempt-id test-pruner-001 \
  --launcher experiments.run_test_pruner_build_graph:launch \
  --run-root logs/runs --lineage-id test-pruner

# 5. RECOVERY: the launcher persists its registration; on a blocked node use
#    scripts/plan_graph_recover.py with that registration, or
#    run_plan_graph.py run --resume; budget ops via
#    run_plan_graph.py budget extend|reset. Node caps: gate limit 5,
#    infra limit 3 (plan_graph_budget.py defaults). Abort rule: operator
#    discards the graph branch and unmerged candidates; partially-merged
#    candidates are reverted by the integration owner before any re-run.

# 6. CALIBRATION (after the graph integrates, before any production run):
#    python3 scripts/dev/tp_calibration.py --labels <operator-labels.json>
#    Operator hand-labels the top-20 ranked candidates. Gate:
#    precision@5 == 100% on the labeled set. Until it passes, the routine
#    runs in report-only mode (evidence + unapplied patch, no quarantine
#    branch). Post-quarantine deletion additionally requires re-running
#    mutation scoring on affected modules with module-level mutation score
#    unchanged — any drop proves the test was not useless and blocks it.

# 7. LIVE PILOT (operator-invoked, not a unit test):
#    python3 experiments/run_test_pruner_feature.py --target <allowlist>
#    Report lands under logs/runs/<run-id>/; the policy doc cites it by
#    sha256 digest. Nothing under docs/ carries run output.
```

## BUDGET

Four nodes × $3–5 (README's FeatureRun figure) plus reviewer/repair loops:
~$12–20, plus one smoke run (~$2–4), plus mutation compute bounded by the
module allowlist's recorded exhaustiveness proof. The graph-level
functionality gate (`tp_gate.py` + contracts check) runs once at
finalization (~50 s). Wall-clock: TP-01 is the long pole (mutation fixtures);
`max_parallelism=2` overlaps TP-01/TP-02. If the smoke run's per-node cost
exceeds the README figure by >2×, stop and re-estimate before dispatching
the graph.

## RISKS

1. **First-of-kind codex integration** (mixture layer has no production
   caller; PlanGraph-bound path never run on codex). Contained by the
   pre-work smoke run; the graph does not dispatch until it is green.
2. **Attribution blind spots.** Subprocess-driven tests (~30 files here) are
   structurally invisible to tracing; they are hard-excluded as
   `out-of-scope`, and the report publishes the out-of-scope count so
   coverage claims stay honest.
3. **Confinement is detection, not prevention.** A codex seat can write
   outside its grant; the git-delta check fails the attempt afterwards, and
   `candidate_only` + human review are the real boundary. Never present the
   sandbox as the safety story.
4. **Gate technicality shaping**: the four-node shape still reflects the
   approval gate's base-commit intent rules (nodes only `create`). Accepted
   consciously this round; recorded here so the shape isn't mistaken for a
   design preference.
5. **Detector neutering pressure.** AC-DET-1 binds to the committed baseline
   snapshot with a published FP count, so the node cannot pass by weakening
   the detector into silence.
6. **Fixture self-reference.** `tests/fixtures/**` and the pruner's own
   tests are a versioned exclusion root enforced in the selector and pinned
   by a test; planted vacuous fixtures can never become candidates.
7. **Known-red module.** `tp_gate.py` excludes
   `tests.test_relax_gate_timeout_classification` only when pytest is
   absent, and announces it; installing pytest re-includes it automatically.
   The exclusion is named in the attestation statement.
8. **Cost overrun.** BUDGET's 2× stop rule after the smoke run.

## OUT OF SCOPE

Scheduling/recurrence (no timer machinery exists), auto-merge or auto-delete
(quarantine-first, calibration-gated, candidate-only by policy), CI
integration, moving the runtime wiring into a `harness_labs/` routines
package (recorded as a follow-up once the routine proves out —
`experiments/` placement keeps it outside the layered import boundaries for
now), and application to Retinology (follows the pilot; the tools are
dependency-free precisely so they port).
