# Contract-burden relaxation — PlanGraph program plan

**Status:** authored for PlanGraph approval (`plan-graph-plan/1` decomposition in
`docs/development/contract-burden-decomposition.json`). Revised after a
three-lens adversarial Opus review (FRAME / NECESSITY / MECHANISM); the lens
reports and the adjudication are committed under
`docs/development/plan-review-cb/` and bound into the approval as referenced
artifacts.
**Provenance:** every node cites `docs/development/contract-burden-reduction.md`
(the living diagnosis) — orbit experiment evidence (2026-08-12) and the
flow-editor-authoring PlanGraph audit (FR-20, graph attempts pg2–pg101, same day).
**Base:** the `contract-burden-relaxation` branch — Impl-redo `de0f3dc` (RB-01–06
delta-scoped retry, lineage ledger, recovery authority, PG-00–07 parallel
scheduler contracts) merged with `claude-p-adapters` `44bfef8`
(`ClaudeAgentSession` coordinator seat).

## Program rules

1. **Red/green is the contract, enforced mechanically.** Every finding gets a
   dedicated, self-contained test file that demonstrably **fails against the
   frozen base harness** and **passes on the candidate**. The controller-owned
   gate for each node is `scripts/dev/red_green_check.py`, which extracts the
   frozen base tree from Git, copies only the node's finding test file into it,
   and requires the red phase to be a genuine behavioral failure: pytest must
   exit with code 1 with at least one test reported FAILED and zero collection
   or runtime errors — an ImportError, usage error, or empty collection is
   rejected as red evidence. The green phase then requires the finding tests
   plus the full harness suite to pass on the candidate. Finding tests must
   therefore import only symbols that exist at the base commit and must be
   single-file self-contained.
2. **Relaxation never widens authority.** Each node's objective is to delete or
   supersede a gate that the diagnosis proved non-load-bearing, while keeping the
   adjacent checks that are load-bearing. The keep-list in the diagnosis
   (receipt/digest binding, write-grant enforcement, controller-owned
   deterministic verification, hash-chained journals) is out of bounds.
3. **This run is itself a recovery-system live test.** The graph registers with
   `automatic_recovery` (actions `resume`, `extend_budget`) on the RB
   `RetryBudgetLedger`; the runner persists the registration, keys the ledger to
   a lineage-stable run root so budget state survives across attempts, and
   imports each node's structured verification evidence into the ledger. Nodes
   run with `verification_repair_limit=3` (the program that indicts
   `repair_limit=1` does not ship with it). A blocked node is recovered through
   `scripts/plan_graph_recover.py` against the persisted registration rather
   than by whole-graph relaunch. Known accepted gap: no node-level
   `RecoveryAgent` is wired; abnormal-outcome recovery below the graph level is
   out of scope for this program.
4. **Green means the whole suite.** The full harness test suite measures ~40
   seconds, so every node's green phase runs it in addition to the node's
   finding tests. A node that breaks any consumer of the files it touches —
   inside or outside its allowed paths — fails its own gate immediately, not at
   a terminal graph-level check it cannot repair.

## Dependencies and parallelism

Edges exist only where nodes share owned files or one node's mechanism consumes
another's. The serial-topological launcher executes this DAG in order today;
the parallel ready-set scheduler merged with PG-00–07 can exploit the width
later.

- `CB-01` (controller kernel + coordinator tool schema) — root.
- `CB-02` (plan validation) — root; shares no file with any other node and is
  parallel-eligible with the entire program.
- `CB-03` ← CB-01: shares `tests/test_feature_run.py` ownership.
- `CB-04` ← CB-03: shares `harness_labs/feature_run.py`; delta-scoping consumes
  CB-03's classification rules.
- `CB-05` ← CB-04: shares `harness_labs/feature_run.py`.
- `CB-06` ← CB-01 and CB-05: shares `harness_labs/controller_kernel.py` with
  CB-01 and `harness_labs/feature_run.py` with the CB-03…CB-05 chain.
- `CB-07` ← CB-05: shares `harness_labs/controller_live.py` and
  `harness_labs/claude_task_executor.py`. Parallel-eligible with CB-06 (disjoint
  paths).
- `CB-08` ← every other node: retires the program's own workarounds and closes
  diagnosis statuses, which is only truthful once the mechanisms exist.

## CB-01 — Kernel dispatch relaxations

Objective: Relax four mechanical dispatch-time rejections in the controller kernel and its coordinator tool schema — criterion-id parsing, capability narrowing, the criterion-source enum, and rejected-dispatch provenance — without weakening any authority the kernel actually enforces.

Diagnosis items 2, 3, 4, 6. Evidence: `unknown task criterion: AC-…` burned
coordinator turns in both orbit graph attempts and at least four flow-editor
FR-20 attempts (pg85/90/98/99); a read-only salvage dispatch (`repo.read` ⊂
`repo.read, repo.write`) was refused as a frozen-authority change; the
`"approved-plan"` criterion source crashed a live launch; a rejected repair
dispatch could not be cited as provenance. The criterion-source enum is
duplicated in `controller_coordinator.py`'s criterion_propose tool schema, so
the node owns both sites — a kernel-only relaxation would accept a value no
live coordinator can send.

Acceptance criteria:

- AC-CB01-1: task.dispatch accepts acceptance_criteria entries of the form "<id>" or "<id>: <text>"; both resolve to the same declared criterion id, and an id unknown to the contract is still rejected.
- AC-CB01-2: a superseding or repair dispatch may declare required_capabilities that are a strict subset of the superseded task's capabilities; widening or disjoint capability sets are still rejected as frozen-authority violations, and details_schema remains frozen.
- AC-CB01-3: criterion source "plan" is accepted by the kernel and offered by the coordinator criterion tool schema alongside operator, repository, and coordinator, and tests/test_feature_run.py exercises the real kernel criterion path with a kernel-valid source instead of mocking past it.
- AC-CB01-4: provenance validation accepts a reference that resolves to an existing audit-journal event, so a rejected task.dispatch — which is already a journal event — becomes citable by later commands without a new evidence namespace.
- AC-CB01-5: tests/test_relax_kernel.py fails behaviorally against the frozen base harness and passes on the candidate together with the full suite.

## CB-02 — Plan-gate relaxation

Objective: Remove the verbatim-substring plan gates from PlanGraph plan validation while preserving every referential integrity check the validator performs.

Diagnosis item 1. Evidence: registration rejected a valid engineered plan until
`assemble_decomposition` mechanically appended objective and criterion strings
to the cited sections — zero semantic change, pure compliance transformation.

Acceptance criteria:

- AC-CB02-1: validate_plan_graph_plan accepts a decomposition whose cited plan sections describe each run without containing the run objective or the criterion statements verbatim.
- AC-CB02-2: the validator still rejects unknown plan-section keys, criteria assigned to no run or to an unknown run, unknown or cyclic dependencies, empty allowed_paths, and path intents outside allowed_paths.
- AC-CB02-3: tests/test_relax_plan_gates.py fails behaviorally against the frozen base harness and passes on the candidate together with the full suite.

## CB-03 — Verification failure classification

Objective: Classify timed-out and signal-terminated verification commands as infrastructure_transient from their structured evidence, so environment faults receive the free bounded retries that classification already earns instead of consuming the repair budget as indeterminate failures.

Diagnosis item 8, narrowed by review: the base harness already retries
`infrastructure_transient` failures free of repair budget; the actual gap is
that `classify_verification_failure` reads only output text, so a timeout
(`timed_out=True`, exit 124) or SIGTERM (negative returncode) classifies
`indeterminate` and burns the budget. Evidence: seven of fourteen terminal
flow-editor FR-20 attempts died on timeout/SIGTERM/walk-driver crashes with
pytest fully green. No new classification vocabulary: the RB ledger's closed
class set (`plan_graph_budget._CLASS_LIMITS`) treats an unknown classification
as a fatal registration error, so the extension rides the existing class.

Acceptance criteria:

- AC-CB03-1: classify_verification_failure classifies a command result with timed_out true, exit code 124, or a negative signal returncode as infrastructure_transient with distinct rule identifiers derived from the structured fields rather than output text.
- AC-CB03-2: a browser or driver crash marker in otherwise pytest-green output classifies as infrastructure_transient rather than product, and genuine assertion failures still classify product.
- AC-CB03-3: tests/test_relax_verification_classes.py fails behaviorally against the frozen base harness and passes on the candidate together with the full suite.

## CB-04 — Delta-scoped verification repair budget

Objective: Delta-scope the verification repair budget on the RB ledger's stable failure-key substrate so a repair attempt that strictly shrinks the observed failing set renews the repair allowance, while non-improving repairs consume it and the loop keeps its existing hard bound.

Diagnosis item 7. Evidence: five FR-20 attempts blocked with a single-digit
failing-test count out of ~733 under `verification_repair_limit=1`; the pg99
seesaw (repair fixes test A, test B fails, one repair short of convergence)
repeated across attempts. Review binding: the ledger already derives stable
per-finding keys (`plan_graph_budget._failure_keys`) and meters repair
dispatches per class; delta-scoping extends that substrate rather than building
a parallel accounting system, and the loop's existing bound
(`repair_limit + 4` iterations) is generalized, not tripled.

Acceptance criteria:

- AC-CB04-1: the verification loop derives a stable failing-identifier set from each failed command result, and a repair whose rerun produces a strictly smaller non-empty failing set does not count against the declared repair limit.
- AC-CB04-2: a repair whose rerun produces an equal, larger, or non-comparable failing set consumes the repair limit as before, the loop's total iteration bound still holds, and every renewal decision is recorded in the audit journal with the compared failing sets.
- AC-CB04-3: a two-step convergence in which the first repair fixes one failing test and surfaces another, and the second repair fixes the remainder, completes with a declared repair limit of one on the candidate harness and blocks on the base harness.
- AC-CB04-4: tests/test_relax_delta_repair.py fails behaviorally against the frozen base harness and passes on the candidate together with the full suite.

## CB-05 — Retry-with-adoption for writable dispatches

Objective: Let a repair or superseding writable dispatch adopt the receipted dirty baseline left by a failed prior attempt in the same run, converting the constructor-frozen allow_dirty_baseline escape hatch into an audited runtime adoption grant bound to the prior attempt's workspace-change receipt.

Diagnosis item 5 — highest value. Evidence: orbit exp-1's gate-passing candidate
was stranded behind `writable worker requires a clean repository baseline`; the
flow-editor launcher hand-implements adoption in prose ("FIRST ACTION:
byte-copy every changed path from the retained pg97 FR-20 worktree…") across
101 graph attempts. Review binding: the grant's coverage test is the receipted
change set alone — writable paths do not admit a dirty baseline, or the grant
degenerates to the allow_dirty_baseline flag it replaces. All three executor
construction sites are owned (claude, codex/live, agent_mixture role profiles).

Acceptance criteria:

- AC-CB05-1: a writable executor preflight accepts a dirty baseline exactly when the controller supplies an adoption grant naming the prior attempt's workspace-change receipt and every dirty path is covered by that receipted change set.
- AC-CB05-2: a dirty baseline without an adoption grant, or with any dirty path outside the receipted change set, is still refused with the existing clean-baseline message, including when the dirty path is inside the task's writable paths.
- AC-CB05-3: verification-repair and review-fix dispatches launched by the feature run supply the adoption grant automatically from the prior attempt's workspace-change receipt, and the grant is recorded in the audit journal.
- AC-CB05-4: tests/test_relax_adoption.py fails behaviorally against the frozen base harness and passes on the candidate together with the full suite.

## CB-06 — Gate-backed criteria adjudication

Objective: Let the controller kernel adjudicate gate-backed acceptance criteria from its own deterministic verification outcome on the plan-graph bound dispatch path, so a build coordinator is never forced to choose between blocking a completed implementation and dishonestly claiming a gate result it cannot run.

Diagnosis item 10, scoped by review to the plan-graph bound path: the bound
schema strips execution to a single implement segment whose instructions
prohibit dispatching "a verification-only task" (`feature_run.py` bound
segment), and the kernel requires every criterion satisfied before completion —
so a criterion whose statement is a gate result can be satisfied before the
gate only by an untruthful claim. Evidence: flow-editor pg88 blocked exactly
there with implementation complete and no findings pending.

Acceptance criteria:

- AC-CB06-1: a run-contract criterion may declare deterministic-verification adjudication, and the kernel marks such a criterion satisfied from the verification owner's passing command evidence rather than from a coordinator claim.
- AC-CB06-2: the plan-graph bound completion path accepts a coordinator completion request when every non-gate criterion is satisfied and gate-backed criteria are pending, and the run reaches a successful terminal status only after the controller-owned command passes.
- AC-CB06-3: a coordinator claim that attempts to satisfy a gate-backed criterion directly is rejected by the kernel.
- AC-CB06-4: tests/test_relax_gate_criteria.py fails behaviorally against the frozen base harness and passes on the candidate together with the full suite.

## CB-07 — Semantic deliverable floor

Objective: Add a minimal deterministic deliverable-content floor at the shared semantic result boundary used by both the Claude and Codex executors, so placeholder worker output is refused mechanically instead of relying on coordinator judgment alone.

The diagnosis counterweight. Evidence: orbit exp-1's originating defect —
`"summary": "test"` passed the typed contract (`minLength: 1` in the shared
raw-output schema in `controller_live.py`) and only the coordinator's judgment
refused it, which then cascaded into the clean-baseline dead-end. Review
binding: the floor lives at the shared boundary (raw-output schema and
`validate_semantic_result`), covering both executors; a Claude-only bolt-on
leaves the hole open for codex workers.

Acceptance criteria:

- AC-CB07-1: a structured worker result whose summary or deliverable fields are placeholder content — sub-minimal length, a known placeholder token such as "test", "todo", or "n/a", or a single repeated token — is refused at the shared result boundary for both the Claude and Codex semantic executors, with an audited classified refusal.
- AC-CB07-2: the floor is a closed deterministic rule set with no model judgment, and substantive worker results pass unchanged through both executors.
- AC-CB07-3: tests/test_relax_semantic_floor.py fails behaviorally against the frozen base harness because a placeholder result is accepted there, passes on the candidate because it is refused, and the full suite passes on the candidate.

## CB-08 — Workaround retirement and diagnosis closure

Objective: Retire the program's own compliance workarounds from the experiment launcher and close the corresponding statuses in the living diagnosis, so the program does not leave in place the pins and mechanical normalizations its own nodes made unnecessary.

Review finding (FRAME): the runner hard-codes the exact workarounds the program
deletes — the bare-criterion-id and frozen-capability pins in
BASE_INSTRUCTIONS, the `"source": "operator"` criterion binding, and the
verbatim-substring normalization in assemble_decomposition — and the diagnosis
document's statuses are owned by no node. This node runs last and edits no
harness code.

Acceptance criteria:

- AC-CB08-1: the experiment launcher no longer pins bare criterion ids or frozen required_capabilities in coordinator instructions, binds plan-graph criteria with source "plan", and no longer appends objectives or criterion statements to plan sections mechanically.
- AC-CB08-2: every diagnosis item resolved by a program node records its landing node and commit in place of its open status, and scripts/dev/check_workaround_retirement.py passes as the node's deterministic gate.

## Program acceptance

The program is complete when all eight node gates pass, the graph-level
functionality command (the full harness test suite) passes on the final
integrated candidate, each node's red/green artifact records a behavioral base
failure and candidate success for its finding tests, and the retirement gate
proves the program's own workarounds are gone. Known accepted constraints:
approval pins the host python3 identity (approve and run in one session), and
re-running a graph attempt under the same run id requires clearing that
attempt's node worktrees and branches first.
