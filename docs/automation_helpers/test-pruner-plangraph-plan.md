# Useless-Test Pruner — PlanGraph Build Plan

Status: draft for review, 2026-08-14
Decomposition: `docs/automation_helpers/test-pruner-decomposition.json`
(protocol `plan-graph-plan/1`)

## OVERVIEW

Build the useless-test pruner — the routine selected in
`automation-routines-analysis.md` §4 — as a PlanGraph-developed,
FeatureRun-hosted maintenance routine. Two artifacts result:

1. **Build-time**: a `plan-graph-plan/1` decomposition (six runs, TP-01..TP-06)
   that PlanGraph executes to implement the pruner inside this repository,
   with every model seat on the Codex exec backend.
2. **Runtime**: the pruner itself — deterministic evidence tools
   (`scripts/dev/mutation_check.py`, `scripts/dev/mutation_score.py`,
   `scripts/dev/vacuous_test_check.py`, `scripts/dev/test_pruner_select.py`) plus an all-Codex FeatureRun wiring
   (`experiments/run_test_pruner_feature.py`) that turns a pruner report into
   a bounded, deletion-only, human-reviewed candidate branch.

The pruner's predicate, per the analysis note: a test is prunable when it
**kills zero mutants of the code it exercises** and/or is **statically
vacuous** (tautological assertions, no assertions, or fully neutralized by
mocking). Evidence, not model judgment, selects candidates; a model only
executes the bounded deletion and writes the explanation.

Design constraints inherited from repository contracts (`AGENTS.md`):
bounded work only, no generalized frameworks, every claim bound to a
verifiable artifact, deletion-only diffs for the routine's output, and one
accountable integration owner.

### Environment facts the plan depends on

- Python 3.11 stdlib only; **no pytest, coverage, mutmut, or cosmic-ray** is
  installed here, and the repository convention is dependency-free tooling
  (`scripts/dev/red_green_check.py` precedent). The mutation engine is
  therefore built on stdlib `ast` + `unittest`, not on an external tool.
- Two existing tests (`tests/test_relax_gate_timeout_classification.py`) fail
  in this container solely because they shell out to pytest. Verification
  commands in this decomposition use `unittest` exclusively and never gate on
  those two tests; TP-06's pilot runs a scoped suite.
- The full suite (481 tests) runs in ~40 s, which makes per-mutant scoped
  reruns affordable if mutants are budgeted (see TP-02).

## BACKENDS

All build-time and runtime model seats run on **Codex exec**. The single
exception is the human-side operator loop (plan authoring, operator
attestation, PR review), which is performed by the Claude session driving
this repository — an operator role, not a run backend.

Mechanism (all existing, no new abstraction):

- Provider grammar `codex:<model>[@reasoning]` and the `codex → "codex-exec"`
  provider map live in `harness_labs/graphrun/agent_mixture.py:34-41,77`.
- Worker seats: an all-codex mixture `{"*": "codex:gpt-5.6-terra@high"}`
  resolved by `build_role_profiles(...)`
  (`agent_mixture.py:184`, choosing `CodexSemanticTaskExecutor`), with
  `executables={"codex": <path>}` when the binary is not on `PATH`.
- Coordinator seat: `build_coordinator_session("codex:gpt-5.6-terra@high", ...)`
  (`agent_mixture.py:342` → `CodexAppServerSession`).
- Text-attempt fallback for simple seats: `CodexExecBackend`
  (`harness_labs/core/backends.py:64`), which already runs
  `codex exec --ignore-user-config --strict-config --ephemeral ...`.
- Precedent: the FeatureRun experiments
  (`experiments/run_trebuchet_feature.py`,
  `experiments/run_retinology_demo_feature.py`) already run coordinator and
  all workers on codex-exec. The PlanGraph experiments
  (`experiments/run_burden3_plan_graph.py:82-84`) are all-Claude; TP-05
  produces the first all-codex PlanGraph launcher by porting that wiring with
  `COORDINATOR_SPEC`, implementer, reviewer, and repair specs set to
  `codex:gpt-5.6-terra@{high,medium}`.

Seat map for the build (per node, via the TP-05-style launcher used to
execute this decomposition):

| Seat | Spec | Notes |
| --- | --- | --- |
| Node coordinator | `codex:gpt-5.6-terra@high` | resident `CodexAppServerSession` |
| Implementer workers | `codex:gpt-5.6-terra@medium` | `CodexSemanticTaskExecutor`, `sandbox: workspace-write`, writable paths = node `allowed_paths` |
| Reviewer workers | `codex:gpt-5.6-terra@high` | read-only sandbox |
| Verification-repair executor | `codex:gpt-5.6-terra@medium` | bounded by `verification_repair_limit=1` |

Runtime pruner seats (TP-05) use the identical map; the deletion implementer
additionally gets writable paths restricted to `tests/**`.

## GRAPH

```
TP-01 (mutation engine)  ──▶ TP-02 (mutant execution + report schema) ─┐
                                                                       ├─▶ TP-04 (selector + policy) ─▶ TP-05 (all-codex runtime wiring) ─▶ TP-06 (pilot + evidence)
TP-03 (static vacuity detector) ───────────────────────────────────────┘
```

TP-01/TP-03 are independent and may run in parallel lanes.

## TP-01 — Mutation engine core

Create `scripts/dev/mutation_check.py` (stdlib-only) with deterministic
mutant *enumeration*: given a target module path, produce an ordered list of
mutants, each a `(mutant_id, ast_transform, location)` record. Operator set,
deliberately small and classical: comparison-operator swap, boolean-operator
swap, arithmetic-operator swap, constant perturbation (`True/False`, small
ints, string→""), statement deletion for `return`/`raise`/`continue`, and
boundary off-by-one on slice/range. Enumeration is pure: no file writes, no
randomness, stable IDs (`<module>:<line>:<col>:<operator>`), so reports are
reproducible and diffable — required for evidence-grade artifacts.

Create `tests/test_mutation_check.py` covering: operator correctness on
fixture snippets, ID stability, enumeration determinism across two calls, and
refusal to mutate files outside the declared target root.

## TP-02 — Mutant execution, scoring, and report schema

Create `scripts/dev/mutation_score.py`, consuming TP-01's enumeration API
(a new file rather than a modification, because the approval gate validates
`modify` intents against the base commit — a file created by TP-01 does not
exist there): for each mutant, materialize the
mutated module in a temporary copy of the package tree (never in the
worktree), run a *scoped* `unittest` selection (the tests declared or
discovered as exercising that module) in a subprocess with a per-mutant
timeout, and classify `killed | survived | timeout | error`. Test→module
scoping uses import tracing (run the test once under an import hook, record
which target modules it imports), with an explicit fallback to
whole-directory attribution when tracing is inconclusive — attribution must
be recorded per test so the report is honest about its own precision.

Budgets are hard inputs, not defaults hidden in code: `--max-mutants-per-module`,
`--per-mutant-timeout`, `--total-deadline`. Exceeding a budget yields status
`budget-exhausted` in the report, never silent truncation.

Create `schemas/test-pruner-report.schema.json` (`test-pruner-report/1`):
per-test record with `test_id`, `attributed_modules`, `attribution_method`,
`mutants_run`, `mutants_killed`, `statuses`, `static_flags` (filled by
TP-03/TP-04), budgets, environment fingerprint, and target-commit SHA.
Create `tests/test_mutation_scoring.py` with a planted fixture: a small
module plus one real test (kills mutants) and one vacuous test (kills none);
assert the report classifies both correctly and validates against the schema.

## TP-03 — Static vacuity detector

Create `scripts/dev/vacuous_test_check.py` (stdlib `ast`): flags test
methods that (a) contain no assertion or raise-check at all, (b) assert
tautologies (`assertTrue(True)`, comparing a literal to itself,
`assertEqual(x, x)`), or (c) assert only against values produced entirely by
mocks/stubs configured inside the same test. Output is a JSON fragment in the
`static_flags` shape of the TP-02 schema. False positives are the failure
mode that destroys trust, so the detector must run against the repository's
current suite as part of its own verification and emit **zero flags** on a
reviewed allowlist snapshot; any flag it raises on the current suite is
either a genuine finding (recorded, not auto-deleted) or a detector bug to
fix before the node passes. Create `tests/test_vacuous_test_check.py`.

## TP-04 — Candidate selector and routine policy

Create `scripts/dev/test_pruner_select.py`: merges a mutation report and
static flags into a deletion-candidate list under the routine's policy
gates — a test is a candidate only if (mutation evidence: attributed mutants
were actually run and zero were killed, with no `timeout`/`error`/
`budget-exhausted` contamination on its attributed modules) **or** (static:
flagged tautological/assert-free), and the emitted list is capped at
`--max-candidates` (default 5), ranked by evidence strength. Selector output
embeds the full evidence chain per candidate.

Create `docs/automation_helpers/test-pruner-policy.md`: the static-plane
policy document — predicate, evidence requirements, budget (≤5 deletions per
run), deletion-only diff rule, quarantine-first option, human-review gate,
and success metrics (merge rate, escaped-defect rate, suite wall-time saved).
Create `tests/test_pruner_select.py`.

## TP-05 — All-codex runtime wiring

Create `experiments/run_test_pruner_feature.py`, modeled on
`experiments/run_trebuchet_feature.py` (already all-codex at FeatureRun
level): it runs the evidence tools deterministically (no model), then
dispatches one `run_feature_worktree(...)` (`harness_labs/featurerun/feature_run.py:567`)
whose implementer (codex-exec) receives the selector report as context and
performs only the deletions, with `allowed_paths` restricted to the candidate
test files, `verification_argv` running the full `unittest` suite plus
`scripts/check_repository_contracts.py`, `candidate_only=True` (merge stays
with the human), and the review/fix loop enabled with a codex-exec reviewer.
A `--plan-graph` variant exposes the same wiring as a PlanGraph launcher so
future fleet scheduling can dispatch it; the port of
`run_burden3_plan_graph.py`'s launcher to codex specs happens here.
Create `tests/test_pruner_runtime_config.py` asserting the wiring: every
seat spec resolves to provider `codex-exec`, deletion lane `allowed_paths`
never escape `tests/**`, and `candidate_only` is forced on.

## TP-06 — Pilot run and audit evidence

Create `tests/fixtures/test_pruner_pilot/` — a miniature package with a
planted suite: two genuine tests, one assert-free test, one tautological
test, one mutation-vacuous test (asserts on a mocked return). Create
`tests/test_pruner_pilot.py`: end-to-end over the fixture — enumeration,
execution, selection — asserting the candidate set is exactly the three
planted vacuous tests and that the two genuine tests survive; then run the
deletion step in a throwaway worktree and assert the fixture suite still
passes. Record the pilot's report JSON under
`docs/automation_helpers/pilot-report.json` as the reviewable evidence
artifact for this decomposition's acceptance.

## RUNBOOK

Executed by the human operator (Claude session assists but the attestation is
the operator's):

```sh
# 1. Freeze and gate the subject (decomposition must be committed at HEAD)
python3 scripts/approve_plan.py prepare \
  docs/automation_helpers/test-pruner-decomposition.json \
  --repository . --output-directory /tmp/tp-approval

# 2. Write operator attestation (schemas/plan-operator-approval.schema.json)
#    subject_sha256 = value printed by prepare

# 3. Issue the receipt
python3 scripts/approve_plan.py issue --repository . \
  --subject /tmp/tp-approval/subject.json \
  --gate-evidence /tmp/tp-approval/gate-evidence.json \
  --operator-approval /tmp/tp-approval/operator.json \
  --receipt /tmp/tp-approval/receipt.json

# 4. Run the graph with the all-codex launcher (TP-05 wiring; until TP-05
#    exists, the burden3 launcher ported to codex specs is used)
python3 scripts/run_plan_graph.py run --repository . \
  --approval-receipt /tmp/tp-approval/receipt.json \
  --decomposition docs/automation_helpers/test-pruner-decomposition.json \
  --graph-attempt-id test-pruner-001 \
  --launcher experiments.run_test_pruner_plan_graph:launch
```

Every node runs in its own worktree/branch, writes `logs/runs/<run-id>/`
journals, and leaves candidate commits; integration follows the AGENTS.md
merge gates.

## RISKS

1. **Attribution imprecision** (test→module mapping). Mitigated by recording
   `attribution_method` per test and disqualifying candidates whose evidence
   rests on fallback attribution. A candidate needs clean, traced evidence.
2. **Mutant runtime blowup.** Hard budgets with `budget-exhausted` status;
   the selector treats budget exhaustion as disqualifying, never as "survived".
3. **Schema drift**: the contract layer accepts `verification_gates`
   (`harness_labs/plangraph/plan_graph_contract.py:162-237`) but
   `schemas/plan-graph-plan.schema.json` is `additionalProperties: false`
   and omits it. This decomposition uses only `verification_argv`.
4. **Codex sandbox**: implementer seats need `workspace-write` limited to
   node `allowed_paths`; the mutation engine itself never writes to the
   worktree (temp-tree materialization), keeping the evidence tools runnable
   under read-only seats.
5. **Pruner self-reference**: the pruner's own tests are excluded from its
   candidate set by policy (a routine may not delete its own oracle).
6. **Baseline pollution**: the two pytest-dependent failures stay out of all
   verification argv; if the fleet later adopts pytest, the policy doc's
   environment fingerprint forces re-baselining.

## OUT OF SCOPE

Scheduling/recurrence (no timer machinery exists in the repo), auto-merge of
pruner output (candidate-only by policy), CI integration, application to the
Retinology repository (follows once this repo's pilot passes; the tools are
dependency-free precisely so they port), and any refactor of existing tests
beyond the planted fixtures.
