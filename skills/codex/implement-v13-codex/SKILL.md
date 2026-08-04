---
name: implement-v13-codex
description: Implement or resume exactly one repository feature through a durable Codex-native phase machine with hashed planning inputs, adversarial plan review, bounded implementation workers, runtime and UI verification, code review/fixes, full certification, documented context reconciliation, guarded Git handoff, merge proof, crash recovery, and a migration path to JSON-defined child phases. Use for implement-v13-codex feature runs, Codex-native autonomous implementation, matching in-progress v13-codex checkpoints dispatched by serial-implement-codex, or work on its phase-harness certification layer.
---

# Implement v13 Codex

Implement one feature. Keep orchestration state on disk; never treat chat text or a child exit code as phase completion.

Before acting, read [references/protocol.md](references/protocol.md), [references/phase-contracts.md](references/phase-contracts.md), and [references/repository-gates.md](references/repository-gates.md). When changing phase execution, context assembly, prompt compilation, debug certification, or subprocess orchestration, also read [references/json-phase-flow.md](references/json-phase-flow.md). Use the scripts and schemas in this package as the deterministic control plane.

## Preflight

1. Before dispatch or durable run-state creation, run the controller preflight.
   For JSON phase flows use `scripts/run_phase_flow.py preflight --flow FLOW --run-dir NEW_PRIVATE_DIR`.
   `scripts/run_exec.py` performs the equivalent production check for
   `jsonschema`, schema validity and local references, semantic inputs, timeout,
   supervisor, working directory, and Codex identity before creating a receipt
   or launching a child. Normative role schemas are compiled deterministically
   by `scripts/response_schema.py` into provider transport schemas. The receipt
   binds the canonical source path, source hash, transport hash, and compiler
   version; final output must satisfy both transport and normative validation.
   Recursive transport validation requires `required == properties`, closed
   objects, explicit typed constants/enums, valid arrays, and the certified
   keyword subset. It binds the receipt and child to frozen prompt and
   transport-schema snapshots. Preflight failure does
   not consume an attempt.
   If the current Python lacks `jsonschema`, run every production control-plane
   script with this exact launcher:

   ```text
   uv run --no-project --with 'jsonschema==4.26.0' python /absolute/path/to/implement-v13-codex/scripts/SCRIPT.py ...
   ```

   Never run `uv sync`, create a project `.venv`, or run `uv` without
   `--no-project` to satisfy controller dependencies. Verify both the base and
   feature worktrees remain unchanged after preflight; any new `uv.lock` or
   other repository file blocks dispatch.
   Require valid flow/schema support, a private auth
   file, the exact Codex executable identity, and every declared model name.
   The first child is the live auth/model/quota probe because the CLI exposes no
   exact quota-headroom API. Park immediately if it is rejected.
2. For a fresh serial dispatch, immediately run
   the `controller_entrypoint` from the dispatch's read-only run-owned package
   as `scripts/start_planning.py DISPATCH.json`. It directly creates the recorded
   worktree and branch, initializes the checkpoint and transaction, resolves
   planning inputs, and launches the read-only planner. No agent runs before
   dispatch and no model participates in Git setup.
3. The planner baseline is always `AGENTS.md`,
   `docs/development/NEXT_STEPS.md`, and `docs/development/INDEX.md`, even when
   explicit planning inputs or a `seed_plan` were dispatched. The startup
   controller embeds the complete resolved baseline and declared planning inputs
    into the planner prompt, with a 384 KiB total limit, before launch. Do not
   automatically read `CLAUDE.md`, installed skills, broad architecture documents,
   pitfalls, or source files before launching the planner; after launch the planner
   may inspect only task-directed source.
4. Never invoke `claude`, `claude -p`, or a Claude agent. This is a Codex harness.
5. Refuse a task mismatch with an existing checkpoint. Never take over a foreign runner or feature run.
6. Create the tracked PHI-free decision record before the first decision; link it from the plan and repository registries.

Do not infer or import arbitrary plans from sibling checkouts. A custom plan enters only as an explicit `seed_plan` planning input and remains subject to source binding and plan review.

## Phase loop

At each detail, re-read the checkpoint and validate its revision. Use `scripts/run_exec.py` for every model role. Receipt IDs include both coarse phase and detail:

`<feature_run_id>:<phase>:<phase_detail>:<role>:<cycle>:<attempt>`

Accept a role only when its receipt is terminal-success, contains `thread.started` and `turn.completed`, has no terminal error event, and its final JSON passes schema plus semantic validation. Advance one detail at a time with `feature_state.py transition`.

Valid plan-review findings, including critical findings, must complete
`review_dispatch` and proceed through `review_collect`, `revise`, and
`revised_plan_validate`. Severity alone never authorizes blocking at
`review_dispatch`; that detail may block only on a terminal failed reviewer
receipt. Block after revision only when the revised plan still contains a
validated unresolved contract or safety conflict.

The caller keeps `start_planning.py` in the foreground until its planner receipt
is terminal. A terminal planner failure must durably transition the checkpoint to
`blocked` and settle the serial queue and lease before exit; terminal success
advances it to `PLANNING/plan_validate/ready`. Never
return from the parent task while the planner receipt is merely prepared, spawned,
released, or running.

After terminal planner success in a serial run, `start_planning.py` immediately
enters `scripts/run_feature.py` in the same foreground Python process. It owns
one resumable, context-bounded coordinator thread and advances this phase loop at
`plan_validate` until the queue item is terminal.
Do not substitute an app-task handoff for that durable controller.
The coordinator must keep the feature worktree as its cwd and receive the
dispatch's absolute base worktree as an explicit additional writable root so it
can write base-local run artifacts and settle the serial queue.
When acting as that coordinator child, execute phase details directly; never
invoke `run_feature.py`, `start_planning.py`, or `serial_state.py`, and never
mutate the serial queue or lease. The production controller marks coordinator
subprocesses and rejects recursive controller entry and queue writes before state access.
A schema-valid coordinator `blocked` result is a complete controller request:
`run_feature.py` atomically persists the blocker at the current checkpoint detail
before settling the serial queue. Never require the coordinator child to mutate
the checkpoint as a separate half of that transition.
Do not invoke `run_exec.py` inside the coordinator sandbox. Write child specs
beneath the run artifact directory and return the coordinator `invoke` action.
For two or three independent roles, return one invocation-batch manifest so the
outer controller launches them concurrently. The first coordinator turn reads the
installed contract once; later turns resume its exact thread with a compact prompt
containing artifact paths only. It reads the durable checkpoint, plan, input
manifest, and prior child-result file from disk. A changed returned thread identity
is a controller failure.
`run_exec.py` disables Codex's internal `multi_agent` feature for every child;
all fan-out and waiting belong to the outer controller, never a child role.

Run the complete phase sequence in [references/phase-contracts.md](references/phase-contracts.md):

1. `PLANNING`
2. `PLAN_REVIEW`
3. `IMPLEMENTING`
4. `RUNTIME_SMOKE`
5. `REVIEWING`
6. `COMMITTING`

Use Sol-medium for planners/reviewers/reviser/UI roles and Terra-medium for implementation workers, REVIEWING code fixers, REVIEWING repair designers, and duplicate judgment. Every spawned agent and Codex process must use `low` or `medium`; high reasoning is forbidden. Preflight rejects a model, phase, role, or reasoning mismatch before launch. Preflight model availability and park rather than substitute silently.

Performance targets are observational unless an operator explicitly declares a
separate hard limit. A missed target records evidence and continues; it must not
stop, block, retry, cancel, or change phase state. Never derive a hard limit from
a benchmark, desired duration, monitoring threshold, or the word `bounded`.

## Planning inputs and decisions

- Pass the planning-input manifest path and hash to planner, reviewers, reviser, and workers.
- Require `plan.v1.json.input_acknowledgements` to cover every required input ID, hash, and role.
- Require `plan.v1.json.review_lenses` to contain the three mandatory lenses in canonical order, followed by zero-to-two materially different blast-radius lenses. Preserve this order through review and triage.
- Normalize a seed plan into the authoritative JSON; never copy it through as approved.
- Revalidate governing-input hashes before implementation and before merge. A change invalidates planning/review.
- Parallel children return decision objects; only the coordinator serially appends validated decisions to the tracked record. Preserve superseded decisions and parked ownership.
- For the three adversarial plan reviewers, set `schema_path` to
  `schemas/plan-review.schema.json`; `run_exec.py` verifies its exact bytes and
  snapshots it into the run artifact directory. Bind the exact task and reviewer
  through each invocation spec's `expected` object. Never hand-author a reviewer
  output schema.

## Writers and review

The controller injects this execution environment into the planner, coordinator,
and every brokered child prompt: macOS with BSD userland under zsh. Use `rc` for
exit status and never assign zsh's read-only `status`; use `rg --files` instead
of GNU `find -printf`; run optional `rg` discovery separately and handle its
no-match exit 1 explicitly without hiding failures from required assertions.

- Derive `implementation-partition.v1.json` with `implementation_partition.py` even when the DAG is linear. Give each implementation worker exactly one manifest group (step IDs, allowed paths, dependencies, and targeted tests), use at most three groups, and record any effort/write-path bound exception. Workers perform no Git operations.
- Derive changed paths from the actual tree through the repository-approved Git agent; never trust a stale checkpoint file list as the staging source.
- Review all tracked and untracked changes independently through the plan-declared lenses. Give each reviewer only the immutable diff, its lens charge, and applicable requirements, with scope expansion prohibited.
- Deterministically propose duplicate groups. A bounded Terra-medium judge decides only whether candidate findings are the same issue; merge confirmed duplicates at the higher severity.
- Fix every critical finding. If there are fewer than ten critical findings, add medium findings in stable lens/finding order until the queue reaches ten. Defer all remaining medium and low findings to tech debt.
- Before any repair, create `review-closure-ledger.v1.json` with one ordered closure group per independent critical issue (group only technically dependent fingerprints). Each group records the originating reviewer role, `implementation` or `architectural` complexity, exact acceptance behavior, adversarial test evidence, every attempted strategy and rejection, and escalation history.
- The originating reviewer role writes the failing adversarial closure test before the fixer runs. Its `author_test` invocation uses `workspace-write` with one to four normalized repository-relative `allowed_write_paths` naming only supplemental test files; the broker rejects any other tree mutation. Design review and targeted review remain mutation-protected even when their sandbox is workspace-write. The fixer may not author or approve that test. Architectural groups require a read-only Terra-medium repair design and separate approval by the originating Sol-medium reviewer before code editing.
- For every architectural group, the immutable adversarial test, repair design, and design review each emit the same closed repair-effect contract. The controller dispositions every governed effect as `must_persist`, `must_remain_absent`, or `must_remain_unchanged` and rejects any mismatch before `ready_for_fix`; prose approval never overrides this deterministic gate and a rejected design consumes no fixer attempt. Malformed role output may persist controller-owned failure checkpoint, blocked queue, failure summary, and failure event state, while success result/receipt, integration, dispatcher acknowledgement, and base Git mutation remain forbidden.
- The closure-test result names every immutable assertion, exact test node and
  structured `test-command/1`, source path/hash, governed artifact, lifecycle
  effect, and expected disposition. Python/pytest commands bind the absolute
  interpreter and runtime hash from `capability-manifest/2`; exact generic
  non-pytest argv remain byte-preserved. The controller writes a
  `repair-assertion-map/2` artifact and
  runs its contradiction solver before every designer or fixer. Unknown
  effects, changed sources, wrong active subjects, and incompatible assignments
  suppress the model call.
  A legacy active closure without this map cannot advance to a new model call;
  `review_closure.py backfill-assertion-map` accepts only a run-owned,
  source-hashed artifact with a distinct verifier receipt and preserves the
  legacy closure-test fields and history.
- Fresh planning writes one source-hashed `capability-manifest/2` from the real
  host Seatbelt broker. It binds the broker identity and certified Python/pytest
  runtime and proves repository reads, repository-write denial, and test-runner
  scratch writes on the host path. The manifest is availability evidence, not
  per-invocation authorization. Injected probes are
  `simulation_only` and cannot certify. Reviewer scratch is controller-created
  per receipt, outside repository writable roots, passed only through the child
  environment, hashed at terminal, and removed.
- Use the canonical repair-design result schema; the schema-bound process output is the design artifact, so the designer remains read-only. For a normal first-attempt route, return one `closure-program/1` manifest and let `closure_driver.py` advance test, design, approval, fix, targeted review, routine rejection, and configured escalation routing without coordinator turns between stages. Only an enumerated unresolved judgment returns to the coordinator.
- A graph-aware closure binds every declared read/write surface and immutable
  test node to current repository source hashes. Its scheduler uses only
  run-owned configured ready-age and retry-penalty values, so a rejected cluster
  cannot starve unrelated dependency-ready work. Legacy unbound ledgers remain
  readable but may not reorder, skip, or batch until a reviewed graph is bound.
- A multi-closure repair is opt-in, contains at most three closures, and must be
  one connected dependency component with the exact union write set, no
  excluded-fingerprint overlap, and distinct originating reviewers. Select the
  deterministic transitive affected test set; unrelated closed peers need no
  reviewer Boolean and are not reopened.
- After a graph-aware fix, `repair_gates.py` runs `forbidden_access`,
  `pre_communication_output_bound`, `process_evidence`,
  `capability_manifest`, and `production_certification` in that order. The last
  gate executes each exact selected command once through the controller-owned
  host broker and writes a hash-bound receipt consumed by reviewers. A failure
  suppresses targeted review and records its exact gate class.
- `closure_driver.py` owns routine redesign, retry, configured escalation, and
  next-ready routing. Coordinator judgment is limited to
  `novel_contract_choice`, `ambiguous_dependency_decomposition`,
  `semantic_conflict_resolution`, and `integration_risk_judgment`.
- Coordinator limits exist only when an explicitly authorized run-owned
  configuration supplies them; benchmarks never create defaults. Configured
  phase, closure, and turn boundaries write a hash-bound rollover summary and
  launch a fresh thread, which must acknowledge the checkpoint, ledger,
  dependency graph, package, and summary hashes exactly.
- Single-closure repair remains the default; use the explicit connected batch contract for the bounded exception. A Terra-medium code fixer reads the ledger from disk, acknowledges the complete attempt-history hash, and uses a strategy family not previously rejected for the group. After the deterministic affected-component gates, each originating reviewer performs its independent targeted disposition.
- Three rejected repair strategies, including pre-fixer architectural design rejections, trigger `escalation_required`, not another automatic design or run failure. Preserve every rejected design receipt, hash, family, reviewer receipt, and evidence in `design_rejections`; design rejection consumes no fixer attempt but still consumes the bounded strategy budget. Escalation must reassign the fixer/designer, decompose the finding, or explicitly block for operator resolution with the accumulated evidence. Retry counts are cost signals and never substitute for finding-level evidence. Unresolved noncritical findings may defer only when required tests pass.
- A digest-bound disposable-test compatibility resolution is valid only with explicit operator authority and an exact immutable source hash, role, mutation path/operation/bytes, mandatory fingerprint-rejection postcondition, and false production/caller-claim selectability. Any missing or broadened field is rejected before reopening design.
- When the exact fixture bytes are shared by production or unrelated invocations, a digest alone is not an invocation discriminator. Reopening design then requires a controller-issued attestation bound to the exact test node and source digest, minted only by the controller, single-use for one invocation, invisible to roles, unavailable to callers and production selection, and fail-closed on absence or mismatch. Caller, payload, environment, scenario, and test-process claims cannot substitute for that attestation.
- If an unchanged fixture has no lawful controller-owned minting boundary, the attestation contract is still contradictory. An operator may then authorize only the exact fixture contract to use a controller-owned anonymous capability channel. The resolution must bind the original test source hash and exact node, leave ordinary dispatch, marker source, and assertions unchanged, keep the capability single-use and role-invisible, forbid caller/production selection, and fail closed on absence, reuse, or mismatch.
- Operator contract resolution preserves all pre-resolution rejected-design and fixer-attempt provenance but establishes each as a historical baseline. The bounded design and fixer budgets restart from zero under the new governing contract, permitting a fresh independently reviewed design and bounded implementation attempts; only post-resolution rejections consume the new budgets.
- New operator resolution data is a generic run-owned
  `operator-resolution-profile/1`; reusable controller code contains no
  repository fixture literals. The profile binds the exact repository, feature,
  closure, test node, test source, and assertion map. Resume requires
  controller-only anonymous-pipe mint/transport/single-use consumption proof
  plus absence, reuse, mismatch, caller-selectability, and ordinary-production
  rejection. One profile hash activates the design and fixer baselines exactly
  once while all old rejection arrays remain append-only.
- When an independent assertion-map verifier proves that a legacy immutable
  test cannot establish every effect in its canonical contract, fail closed.
  The operator may authorize only a source-bound supplemental immutable test
  through `resolve-legacy-assertion-conflict`. Preserve the original test,
  design, attempts, and blocked verifier result append-only; clear only the
  active test bindings, restart the test/design budgets once, and require the
  originating reviewer to author the supplemental assertion map before any
  new Terra-medium fixer attempt.
- Run one fresh terminal integration review against the final tree. A later code edit invalidates that review, smoke B, UI, and full-suite certification.

## Context reconciliation

Before final gates, create `context-reconciliation.v1.json` and validate it with `feature_state.py validate-reconciliation`.

- Update `AGENTS.md`, `docs/development/NEXT_STEPS.md`, and
  `docs/development/INDEX.md` before final gates. Reconciliation must prove each
  changed from its planning-input hash to its recorded output hash.
- Run reconciliation with the actual changed-path manifest. Disposition every
  declared living/reconcile-if-affected input and every touched module context;
  a touched module context absent from planning blocks.
- Update an affected living document in the same feature. Do not churn an unaffected document.
- Treat ADRs and immutable contracts as verify-only unless a reviewed successor/amendment is explicitly in scope.
- Archive authoritative plan JSON and its deterministic Markdown mirror under `docs/archive/YYYY-MM/`; register both and link them from the run manifest.
- Validate that the archived Markdown plan links the dispatch's exact recorded
  decision file with `scripts/validate_plan_decision_link.py PLAN.md DECISION.md`.
- Run the repository's normal documentation-link gate first. Only when a
  pre-harness historical missing plan link is the sole completion blocker may
  `scripts/resolve_required_doc_link.py REPO DOCUMENT BROKEN_TARGET --apply` be
  used; zero or multiple exact-basename/Q-number-plan candidates remain blocked.

Missing reconciliation, plan archive, registry linkage, or decision-record validation blocks completion.

## Certification and terminal transaction

Run repository gates exactly as declared, including runtime smoke B, live Playwright/Puppeteer walks for UI changes, the certification interpreter's complete suite, PHI/security checks, boundary tests, and documentation checks. Required skips are failures.

Use direct controller Git for deterministic setup and a Codex Git role only where
repository policy requires judgment. Never invoke Claude tooling. Main-branch
merge policy remains authoritative.

Advance the base-local transaction through:

`prepared → feature_committed → manifest_committed → merge_prepared → merged → cleanup_complete → feature_result_written`

Retain the checkpoint until cleanup is proven. Write immutable `feature-result.v1.json` only after merge, manifest-on-base, cleanup, and base invariants pass. The serial dispatcher alone advances `dispatcher_ack` while atomically marking the queue item done.

## Recovery

- Reconcile prepared/spawned/running receipts by PID, process group, and start fingerprint.
- Resume explicit Codex thread IDs only; never use `--last`.
- On controller-process recovery, derive the next coordinator turn from this feature run's complete contiguous succeeded coordinator receipts and require one unchanged thread ID across them. Resume at exactly the next turn; never reset the counter or create a replacement coordinator thread.
- Reuse valid terminal receipts as idempotency tokens.
- Revalidate a validator-only failed receipt in place only when its original
  semantic-contract digest and complete artifact hash manifest already exist
  and every terminal, invocation, schema, and semantic check now passes. Append
  recovery provenance and advance its revision without relaunching the child.
- Require blocker-class-specific resolution evidence before resuming a blocked run.
- After the serial dispatcher validates a blocked feature's token, identity, surviving checkpoint/transaction hashes, and nonempty resolution evidence, the production controller may reopen only that same checkpoint phase/detail at `ready`. It appends the dispatcher authorization digest and resolution evidence to checkpoint history; no unauthorized `blocked -> ready` edge exists.
- Recover terminal progress from the base-local transaction and run IDs, never the newest manifest.
- Never force-remove a worktree with unknown files.

Before returning to a serial dispatcher, the deterministic `run_feature.py`
controller settles the queue atomically. A model coordinator returns a validated
blocker or completion result but never invokes `serial_state.py` itself. The outer
controller performs guarded block or acknowledgment with the recorded queue path,
coordinator ID, and lease, then reads back terminal state. Never rely on a parent
task waking from a message to perform either transition.

Return only after verified completion or a durable blocker. Include the feature result, decision record, archived plans, manifest, reconciliation receipt, gate evidence, merge receipt, and any parked decisions.

## Controller package and migrated-run recovery

Every new serial dispatch copies only the two bounded controller packages
(scripts, schemas, prompts, references, manifests, and skill metadata) beneath
the run artifact directory. `controller-package-manifest.v1.json` binds every
relative path, SHA-256, and executable mode to one package digest. Queue feature
state, dispatch, checkpoint, transaction, and process receipts carry that digest;
controller and child launch reject global-package substitution or byte/mode
drift.

A legacy active run may adopt a certified package only through the run-owned
`controller_package.py migrate-run` command. It holds migration authority and
then queue, immutable dispatch, checkpoint, transaction, closure-ledger, and
journal authority in that fixed order. Queue bytes are constructed only by
`serial_state.cas_migrate_feature_locked`; ledger changes use
`review_closure.cas_save_ledger`; every mutable document has revision and hash
CAS. The original `dispatch.v1.json` is never rewritten.

The `prepared` journal and every prefix through `validated` are a forward
recovery plan, not launch authority. Rerun the same command with the same
proposal, witnesses, and authorization to acknowledge an already-written
post-image or perform the next missing CAS. Only a fully read-back `committed`
journal is a migration receipt.

After token-gated serial resume, `prepare_dispatch` returns only
`dispatch_action=resume_existing_run`. Its sole consumer is the run-owned:

```text
python RUN_PACKAGE/implement-v13-codex/scripts/run_feature.py RESUME_DISPATCH.json \
  --resume-existing-run \
  --expected-migration-sha256 SHA256 \
  --expected-package-digest SHA256 \
  --coordinator-id ID --lease-id ID
```

`start_planning.py` rejects that action. `run_feature.py` revalidates the
committed migration under launch exclusion, runs every registered production
response schema through the production compiler/preflight, consumes the resumed
lease once, reopens only blocked `REVIEWING/fix`, and uses a fresh migrated
coordinator receipt namespace. No attempt identity or child exists before that
deterministic gate passes.

## JSON phase-harness boundary

Treat the JSON-driven phase harness as an inner child-execution subsystem, not a
replacement for this skill's feature lifecycle. A validated flow specification
may own child phase order, prompt templates, declared context, model, reasoning,
sandbox, output schema, and child receipt policy. The feature checkpoint,
planning-input contract, repository gates, Git transaction, merge proof, and
feature result remain authoritative outside that subsystem.

Derive debug mode only from an absent or empty context catalog. Debug mode uses
an isolated empty workspace, a neutral child protocol, no repository or Git
operations, and a protocol-distinct result that can never acknowledge a serial
queue item. A nonempty catalog derives project mode and freezes required context
bytes and hashes before the first dependent child.

Do not claim skill-discovery isolation from prompt wording or
`--ignore-user-config`. For an empty-context certification, use
`scripts/run_phase_flow.py` with `examples/debug-flow.json`; it combines a
private `HOME` and `CODEX_HOME`, disabled skill/plugin/project-doc ingestion, a
sterile working directory, and fail-closed JSONL inspection. It rejects any
command execution, document-discovery evidence, unexpected file change,
nonempty stderr, terminal error, or reused thread. Project-mode execution is
not enabled by this debug runner.

## Phase-flow debug certification

Run the JSON-derived 32-unit debug catalog without repository, planning, skill,
or Git context:

```text
python3 scripts/run_phase_flow.py preflight --flow examples/debug-flow.json --run-dir /private/isolated/run
python3 scripts/run_phase_flow.py start --flow examples/debug-flow.json --run-dir /private/isolated/run
python3 scripts/run_phase_flow.py start --flow examples/debug-flow.json --run-dir /private/isolated/run --stop-after 4
python3 scripts/run_phase_flow.py resume --run-dir /private/isolated/run
python3 scripts/run_phase_flow.py verify --run-dir /private/isolated/run
python3 scripts/run_phase_flow.py inspect --run-dir /private/isolated/run
```

The run directory must be new, private, and outside the repository. The
controller freezes the exact flow, derives mode from the context catalog, and
creates a fresh real Codex thread per unit. A terminal `debug-result.json` is
orchestration-only evidence and cannot acknowledge a serial feature.

## Legacy synthetic parity fixture

`scripts/run_synthetic_flow.py` preserves the pre-JSON coordinator behavior for
regression comparison. It accepts the production dispatch payload emitted by
`serial_state.py` unchanged (including additive extension fields),
starts a fresh real `codex exec` for every detail, and accepts a detail only
when the child-authored marker, final schema output, JSONL events, hashes, and
production process receipt agree. `start --stop-after N`, `resume`, and
read-only `verify` cover pause and crash-recovery behavior. A synthetic result
uses its own protocol and cannot be mistaken for a merge-bearing feature result.

It is not an empty-context debug runner: its child prompt names this skill, and
Codex may ingest installed skill documents. Do not use its mechanically passing
result as evidence of zero reads. See
[references/synthetic-flow.md](references/synthetic-flow.md) for its historical
payload and commands.
