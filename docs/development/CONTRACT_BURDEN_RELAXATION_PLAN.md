# Contract-burden relaxation — PlanGraph program plan

**Status:** authored for PlanGraph approval (`plan-graph-plan/1` decomposition in
`docs/development/contract-burden-decomposition.json`).
**Provenance:** every node below cites `docs/development/contract-burden-reduction.md`
(the living diagnosis) — orbit experiment evidence (2026-08-12) and the
flow-editor-authoring PlanGraph audit (FR-20, graph attempts pg2–pg101, same day).
**Base:** the `contract-burden-relaxation` branch — Impl-redo `de0f3dc` (RB-01–06
delta-scoped retry, lineage ledger, recovery authority) merged with
`claude-p-adapters` `44bfef8` (`ClaudeAgentSession` coordinator seat).

## Program rules

1. **Red/green is the contract.** Every finding gets a dedicated test file that
   demonstrably **fails against the frozen base harness** and **passes on the
   candidate**. The controller-owned gate for each node runs
   `scripts/dev/red_green_check.py`, which extracts the base tree from Git,
   copies only the node's new finding tests into it, requires pytest to fail
   there, then requires the finding tests plus the node's regression targets to
   pass on the candidate. Finding tests must fail on base because of behavior,
   not ImportError: exercise existing entry points (`ControllerKernel`,
   `validate_plan_graph_plan`, `classify_verification_failure`, executor
   preflights) rather than symbols that do not exist at base.
2. **Relaxation never widens authority.** Each node's objective is to delete or
   supersede a gate that the diagnosis proved non-load-bearing, while keeping the
   adjacent checks that are load-bearing. The keep-list in the diagnosis
   (receipt/digest binding, write-grant enforcement, controller-owned
   deterministic verification, hash-chained journals) is out of bounds.
3. **This run is itself a recovery-system live test.** The graph registers with
   `automatic_recovery` (actions `resume`, `extend_budget`) and the RB
   `RetryBudgetLedger`; a blocked node exercises `scripts/plan_graph_recover.py`
   rather than a whole-graph relaunch.

## CB-01 — Kernel dispatch relaxations

Objective: Relax four mechanical dispatch-time rejections in the controller kernel — criterion-id parsing, capability narrowing, the criterion-source enum, and rejected-dispatch provenance — without weakening any authority the kernel actually enforces.

Diagnosis items 2, 3, 4, 6. Evidence: `unknown task criterion: AC-…` burned
coordinator turns in both orbit graph attempts and at least four flow-editor
FR-20 attempts (pg85/90/98/99); a read-only salvage dispatch (`repo.read` ⊂
`repo.read, repo.write`) was refused as a frozen-authority change; the
`"approved-plan"` criterion source crashed a live launch; a rejected repair
dispatch could not even be cited as provenance.

Acceptance criteria:

- AC-CB01-1: task.dispatch accepts acceptance_criteria entries of the form "<id>" or "<id>: <text>"; both resolve to the same declared criterion id, and an id unknown to the contract is still rejected.
- AC-CB01-2: a superseding or repair dispatch may declare required_capabilities that are a strict subset of the superseded task's capabilities; widening or disjoint capability sets are still rejected as frozen-authority violations, and details_schema remains frozen.
- AC-CB01-3: the controller kernel accepts criterion source "plan" alongside operator, repository, and coordinator, and tests/test_feature_run.py exercises the real kernel criterion path with a kernel-valid source instead of mocking past it.
- AC-CB01-4: a rejected task.dispatch mints a referencable provenance identity recorded in the audit journal, and a later command citing that identity passes provenance validation.
- AC-CB01-5: tests/test_relax_kernel.py fails against the frozen base harness and passes on the candidate, and the kernel and feature-run regression suites pass on the candidate.

## CB-02 — Plan-gate relaxation

Objective: Remove the verbatim-substring plan gates from PlanGraph plan validation while preserving every referential integrity check the validator performs.

Diagnosis item 1. Evidence: registration rejected a valid engineered plan until
`assemble_decomposition` mechanically appended objective and criterion strings
to the cited sections — zero semantic change, pure compliance transformation.

Acceptance criteria:

- AC-CB02-1: validate_plan_graph_plan accepts a decomposition whose cited plan sections describe each run without containing the run objective or the criterion statements verbatim.
- AC-CB02-2: the validator still rejects unknown plan-section keys, criteria assigned to no run or to an unknown run, unknown or cyclic dependencies, empty allowed_paths, and path intents outside allowed_paths.
- AC-CB02-3: tests/test_relax_plan_gates.py fails against the frozen base harness and passes on the candidate, and the plan-graph regression suite passes on the candidate.

## CB-03 — Verification failure classification

Objective: Extend deterministic verification failure classification so timeouts, signal terminations, and browser or driver crashes are explicitly classified, and environment-class failures never consume the verification repair budget.

Diagnosis item 8. Evidence: seven of fourteen terminal flow-editor FR-20
attempts died on timeout (exit 124), SIGTERM (143 / −15), or a browser
walk-driver crash with pytest fully green; each charged the single repair
allowance and blocked the node.

Acceptance criteria:

- AC-CB03-1: classify_verification_failure classifies a timed-out command result and a signal-terminated exit as environment_retryable with distinct rule identifiers, using the structured timed_out and exit_code evidence rather than output text alone.
- AC-CB03-2: the deterministic verification loop retries environment_retryable failures within a bounded, audited environment-retry allowance without dispatching a repair executor and without consuming the repair budget.
- AC-CB03-3: product-classified failures consume the repair budget exactly as before, and the classification of every failure is recorded in the audit journal.
- AC-CB03-4: tests/test_relax_verification_classes.py fails against the frozen base harness and passes on the candidate, and the feature-run regression suite passes on the candidate.

## CB-04 — Delta-scoped verification repair budget

Objective: Delta-scope the verification repair budget so a repair attempt that strictly shrinks the observed failure evidence renews the repair allowance, while non-improving repairs consume it and the loop remains bounded by a hard audited cap.

Diagnosis item 7. Evidence: five FR-20 attempts blocked with a single-digit
failing-test count out of ~733 under `verification_repair_limit=1`; the pg99
seesaw (repair fixes test A, test B fails, one repair short of convergence)
repeated across attempts.

Acceptance criteria:

- AC-CB04-1: the verification loop extracts a stable failing-identifier set from each failed command result, and a repair whose rerun produces a strictly smaller non-empty failing set does not count against the declared repair limit.
- AC-CB04-2: a repair whose rerun produces an equal, larger, or non-comparable failing set consumes the repair limit as before, and total repair dispatches are bounded by a hard cap recorded in the audit journal.
- AC-CB04-3: a two-step convergence in which the first repair fixes one failing test and surfaces another, and the second repair fixes the remainder, completes with a declared repair limit of one on the candidate harness and blocks on the base harness.
- AC-CB04-4: tests/test_relax_delta_repair.py fails against the frozen base harness and passes on the candidate, and the feature-run regression suite passes on the candidate.

## CB-05 — Retry-with-adoption for writable dispatches

Objective: Let a repair or superseding writable dispatch adopt the receipted dirty baseline left by a failed prior attempt in the same run, converting the constructor-frozen allow_dirty_baseline escape hatch into an audited runtime adoption grant.

Diagnosis item 5 — highest value. Evidence: orbit exp-1's gate-passing candidate
was stranded behind `writable worker requires a clean repository baseline`; the
flow-editor launcher hand-implements adoption in prose ("FIRST ACTION:
byte-copy every changed path from the retained pg97 FR-20 worktree…") across
101 graph attempts.

Acceptance criteria:

- AC-CB05-1: a writable executor preflight accepts a dirty baseline when the controller supplies an adoption grant naming the prior attempt's workspace-change receipt and every dirty path is covered by the receipted change set or the task's writable paths.
- AC-CB05-2: a dirty baseline without an adoption grant, or with dirty paths outside the receipted set, is still refused with the existing clean-baseline message.
- AC-CB05-3: verification-repair and review-fix dispatches launched by the feature run supply the adoption grant automatically from the prior attempt's workspace-change receipt, and the grant is recorded in the audit journal.
- AC-CB05-4: tests/test_relax_adoption.py fails against the frozen base harness and passes on the candidate, and the executor and controller-live regression suites pass on the candidate.

## CB-06 — Gate-backed criteria adjudication

Objective: Let the controller kernel adjudicate gate-backed acceptance criteria from its own deterministic verification outcome, so a build coordinator is never forced to block a completed implementation merely because gate evidence remains pending.

Diagnosis item 10. Evidence: flow-editor pg88 — implementation and summary
complete, no findings pending, yet the coordinator's only legal move was
`run.block_request` because the remaining criteria required the verification,
browser, and review gates the build segment is prohibited from dispatching.

Acceptance criteria:

- AC-CB06-1: a run-contract criterion may declare deterministic-verification adjudication, and the kernel marks such a criterion satisfied from the verification owner's passing command evidence rather than from a coordinator claim.
- AC-CB06-2: the plan-graph bound completion path accepts a coordinator completion request when every non-gate criterion is satisfied and gate-backed criteria are pending, and the run reaches a successful terminal status only after the controller-owned command passes.
- AC-CB06-3: a coordinator claim that attempts to satisfy a gate-backed criterion directly is rejected by the kernel.
- AC-CB06-4: tests/test_relax_gate_criteria.py fails against the frozen base harness and passes on the candidate, and the kernel and feature-run regression suites pass on the candidate.

## CB-07 — Semantic deliverable floor

Objective: Add a minimal deterministic deliverable-content gate at the semantic task executor boundary so placeholder worker output is refused mechanically instead of relying on coordinator judgment alone.

The diagnosis counterweight. Evidence: orbit exp-1's originating defect —
`"summary": "test"` passed the typed contract (`minLength: 1`) and only the
coordinator's judgment refused it, which then cascaded into the clean-baseline
dead-end.

Acceptance criteria:

- AC-CB07-1: a structured worker result whose summary or deliverable fields are placeholder content — sub-minimal length, a known placeholder token such as "test", "todo", or "n/a", or a single repeated token — is refused at the executor boundary with an audited classified refusal.
- AC-CB07-2: the floor is a closed deterministic rule set in harness_labs/deliverable_floor.py with no model judgment, and substantive worker results pass unchanged.
- AC-CB07-3: tests/test_relax_semantic_floor.py fails against the frozen base harness because the placeholder result is accepted there, passes on the candidate because it is refused, and the executor regression suite passes on the candidate.

## Program acceptance

The program is complete when all seven node gates pass, the graph-level
functionality command (the full harness test suite) passes on the final
integrated candidate, and each node's red/green artifact records the base
failure and candidate success for its finding tests.
