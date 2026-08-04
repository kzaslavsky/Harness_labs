# Phase contracts

## PLANNING

Details: `planner_prepare`, `planner_run`, `plan_validate`, `plan_render`.

Run one read-only Sol-medium planner. The fresh-dispatch launch target is under 60
seconds; record whether it is met and continue either way. Keep
`start_planning.py` in the foreground until its receipt is terminal. It consumes
the exact task plus the complete embedded
planning-input packet and may inspect only task-directed source itself. Bind the
exact task as a `const` in the attempt schema. Require `plan.schema.json`, acyclic
DAG, effort arithmetic, disjoint write sets, runtime contracts, tests, input
acknowledgements, and an ordered code-review lens plan derived from the blast
radius. The first three lenses are always L1/L2 contract boundaries,
security/privacy/destructive behavior, and correctness, in that order. The planner
may add zero-to-two materially different lenses; integration/consumer
compatibility is the standard optional lens when those surfaces are touched.
Terminal success advances to `plan_validate/ready`; terminal failure blocks with
receipt evidence. Render Markdown deterministically.

## PLAN_REVIEW

Details: `review_dispatch`, `review_collect`, `revise`, `revised_plan_validate`.

Run source-binding, necessity, and frame reviewers concurrently. Missing/invalid
review is a blocker. Valid findings always advance through `review_collect` and
one reviser over the immutable plan and all review hashes. Require one
evidence-backed disposition per finding. Critical severity does not block before
revision. Only a contract/safety conflict that remains explicitly unresolved in
the validated revised plan blocks at `revised_plan_validate`.

All three reviewers set `schema_path` to `schemas/plan-review.schema.json`;
`run_exec.py` verifies and snapshots it. Task and reviewer identity are bound
through `spec.expected`. Do not hand-author reviewer schemas.

`IMPLEMENTING/implementation_worker`, `REVIEWING/code_fixer`, and
`REVIEWING/repair_designer` use `gpt-5.6-terra` with medium reasoning. Every
planner, reviewer, reviser, judge, UI role, controller, and other spawned process
also uses only low or medium reasoning; high reasoning is forbidden.

## IMPLEMENTING

Details: `strategy_validate`, `workers_dispatch`, `workers_collect`, `integration_validate`.

Derive one-to-three bounded implementation groups deterministically from the
validated DAG, including fully sequential DAGs. Each group is bounded to effort
12 and 18 write paths; if the three-group ceiling forces an overage, record the
exception in `implementation-partition.v1.json`. Dispatch each through `run_exec.py` with
`phase: "IMPLEMENTING"`, `role: "implementation_worker"`,
`model: "gpt-5.6-terra"`, and `reasoning: "medium"`. Every step is completed once,
changes remain inside declared ownership, dependencies precede consumers, and
targeted tests pass.

## RUNTIME_SMOKE

Details: `smoke_a_run`, `smoke_a_fix`, `smoke_a_rerun`.

Run every declared runtime/static contract. Missing configuration or a skipped required check fails. Permit at most two Terra-medium fixer cycles; inspect partial writer edits before resuming.

## REVIEWING

Details: `review_dispatch`, `ui_walk_plan`, `score`, `fix`, `rereview`, `review_finalize`.

Review the complete diff including new files once through every plan-declared lens. Lens reviews are independent and receive only the immutable diff, their lens charge, and directly applicable requirements; prompts prohibit scope expansion. For UI impact, require a synthetic PHI-free state graph, unique loopback server, Playwright/Puppeteer actions, screenshots, logs, assertions, and owned-process cleanup.

At `score`, group candidate duplicates deterministically, then use one Terra-medium judgment with only the candidate findings to decide whether they describe the same issue. The judge may not discover, rewrite, or reprioritize findings. Merge confirmed duplicates at the higher severity. Triage in stable lens and finding order: fix every critical finding; when there are fewer than ten critical findings, add medium findings until the fix queue reaches ten; send the remaining medium and every low finding to tech debt.

Create the durable review-closure ledger before repair. Each independent critical
group records its originating reviewer, complexity, acceptance behavior, closure
test, strategy history, and escalations. The originating Sol-medium reviewer
writes a failing adversarial test before the fixer runs. Architectural groups
first receive a read-only Terra-medium schema-bound design whose process output is
the immutable design artifact, independently approved by that reviewer. The
first attempt must also deterministically require the adversarial test, design,
and design review to agree on the complete closed repair-effect contract before
`ready_for_fix`. The adversarial test also produces a source-hashed assertion
map for its exact node and structured command. Python/pytest commands bind the
manifest's absolute certified interpreter and runtime hash; generic non-pytest
argv remain exact. Before design and again before fix, deterministically verify
the production-real capability manifest and solve all
assertion/effect assignments; a failed gate suppresses the model call. Reviewer
invocations request controller-created ephemeral scratch with the boolean
`ephemeral_scratch: true`; they never select or receive a scratch path in the
invocation contract. The executor derives a private path from the receipt
identity, validates that it is outside repository write authority, creates it
immediately before launch, and hashes/removes it before terminal receipt. Roles
never obtain repository write authority under the label of scratch. The sole
reviewer repository-write exception is `author_test`: it declares one to four
normalized repository-relative `allowed_write_paths` for supplemental test
files. The broker compares exact pre/post file state and rejects changes outside
that set. `design_review` and `targeted_review` retain the no-tree-mutation gate.
Scratch hashing accepts only symlinks whose fully resolved targets remain inside
the private scratch root (including pytest's internal `pytest-current` links);
broken, cyclic, or escaping links fail the terminal gate. The capability
manifest proves host availability and binds the broker/runtime identities.
After a fix, the controller—not a reviewer—executes each exact selected command
once through that broker in the ordered `production_certification` gate,
records output and scratch hashes/removal, and passes only the receipt path/hash
to targeted review.
Any
classification conflict rejects the design without consuming a fixer attempt;
Boolean or prose approval is insufficient. The normal
first-attempt path is a controller-owned closure program that advances test,
design, approval, fix, targeted review, and routine routing without a coordinator
turn between stages. Single-closure repair is the default; only the explicit
connected batch below is an exception. Each Terra-medium fixer attempt
must acknowledge all prior attempts and use a strategy family not previously
rejected. `rereview` is performed by the originating reviewer and returns
`fixed`, `not_fixed`, or `regression`. Legacy unbound ledgers rerun every
previously closed test; graph-aware repairs use the deterministic affected
component instead.
If the controller is interrupted during a fixer invocation, it terminalizes
and scratch-audits the process receipt before re-raising. Recovery may
atomically mark every latest running attempt sharing that verified invocation
as interrupted and return the complete ordered batch to `ready_for_fix`.
Partial, mismatched, or unverified evidence leaves the ledger unchanged and
blocks a new launch.
Three rejected repair strategies, including pre-fixer architectural design
rejections, require reassignment, decomposition, or operator escalation. Persist
rejected-design provenance in the ledger; design rejection consumes no fixer
attempt but does consume the bounded strategy budget. They do not automatically terminate the run. Unresolved
medium and low findings may move to tech debt only when required tests pass. At
`review_finalize`, run one fresh terminal integration review of the final tree.
Any later code edit invalidates that terminal review.

For source-bound closures, validate the acyclic code/test dependency graph
before scheduling. Advance unrelated dependency-ready closures around a rejected
cluster using configured ready age and retry penalty. Permit an explicit atomic
batch of at most three closures only inside one connected component, with the
exact union write set and independent originating reviewers. After the fix, run
the affected component's immutable tests and the earlier deterministic
policy/process gates before targeted model review. Do not request unrelated
closed-peer reviewer Booleans.

Routine redesign, retry, escalation routing, and next-ready selection remain in
the deterministic closure driver. When no continuation is pre-bound, the legal
routes `next_ready`, `retry_fix`, and `redesign` return to the outer controller
so a fresh source-hashed program can be bound. Missing that optimization edge
is not `routine_program_missing`; every unknown route remains fail-closed. A
coordinator turn requires an enumerated judgment reason. When run-owned coordinator limits are configured, roll to a
fresh hash-acknowledging context at phase, closure, and turn boundaries; reject
stale summary hashes and any attempt to resume the pre-rollover thread.

If an independent legacy assertion-map verifier returns `blocked` because the
immutable test omits governed effects, its result is not a valid backfill
receipt. After explicit operator authorization,
`review_closure.py resolve-legacy-assertion-conflict` archives the old test and
verifier bytes, preserves all design and attempt provenance, and reopens only
`test_required`. A supplemental test may already pass because a historical
repair was accepted, but its new source-hashed map must cover the full unchanged
effect contract and pass independent verification before another fixer.

## COMMITTING

Details: `smoke_b_run`, `smoke_b_fix`, `ui_walk_run`, `full_venv_run`, `full_venv_fix`, `final_gates`, `feature_commit`, `manifest_commit`, `merge_prepare`, `merge`, `cleanup`.

Smoke B always runs. Post-review edits loop back through targeted closure, smoke B, UI, and full certification without reopening unrelated broad lenses. Suite-fix counts are cost/escalation signals, not finding-closure limits. Validate context reconciliation; archive plan JSON/Markdown; validate decision records; run all repository gates. Require a valid fresh terminal integration review before commit. Use only the repository-authorized Git agent. Finish the durable transaction and feature result in protocol order.
