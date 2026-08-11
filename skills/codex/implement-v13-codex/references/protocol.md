# Protocol

## Authority

Conversation text is non-authoritative. The checkpoint owns feature phase state; process receipts own child completion; the feature transaction owns commit/merge/cleanup recovery; the immutable feature result is the dispatcher's completion input.

A schema-valid coordinator `blocked` result is a complete block request. The
deterministic controller atomically sets the current checkpoint detail to
`blocked`, persists the exact blocker and history entry, and then settles the
feature queue. The coordinator is not required to perform a separate checkpoint
mutation; split ownership can leave a closure ledger blocked while the feature
controller continues launching coordinator turns.

All JSON state writes use a same-directory temporary file, file `fsync`, atomic replacement, directory `fsync`, an advisory lock, and compare-and-swap revision. Preserve unknown compatibility fields.

Each non-legacy run is bound to one read-only run-owned controller-package
manifest digest. Normative response schemas remain semantic authority; the
provider receives a deterministic compiled transport schema. Process receipt
v2 stores both hashes and a closed terminal cause. Structured strict-schema 4xx
failures are `response_schema_transport_rejected`, diagnostics from stderr stay
separate, and the retry table forbids cross-model retry for that class.

## Phase-harness separation

A JSON phase flow may control child order, prompt compilation, declared context,
model, sandbox, schema, and process evidence. It is subordinate to the feature
checkpoint and cannot advance repository gates, the feature transaction, or the
feature queue. Debug flow results use a distinct protocol and are never feature
completion evidence. See [json-phase-flow.md](json-phase-flow.md).

## Planning-input contract

Planning inputs are an allowlist. Queue defaults and feature inputs merge by stable `id`. Supported roles are `governing`, `background`, `acceptance`, and `seed_plan`. Supported revisions are `latest_on_base`, `exact_sha256`, and explicit `snapshot`. External snapshots require affirmative authorization, are copied mode 0600 into the local artifact root, and retain their source hash. The automatic baseline is always `AGENTS.md`, `docs/development/NEXT_STEPS.md`, and `docs/development/INDEX.md`, including when an explicit seed plan or other planning input was dispatched. `CLAUDE.md` is never a Codex planning input.

The planner must acknowledge every required input in `plan.v1.json`. Prompt
construction embeds the complete resolved fixed baseline and declared inputs once,
in manifest order, with stable ID, role, path, and hash delimiters. The total raw
input content is capped at 256 KiB and a limit violation blocks before child
launch. The child receives this packet through its frozen prompt and must not
reread it or load installed skills. It may perform only task-directed source
inspection after launch. This rule prevents accidental cross-checkout context
injection and keeps startup bounded.

Before final gates, `AGENTS.md`, `docs/development/NEXT_STEPS.md`, and
`docs/development/INDEX.md` must each have an `updated` reconciliation entry
whose output hash matches the current worktree and differs from its planning
input hash.

## Benchmark and reasoning policy

Benchmarks, performance targets, desired durations, and monitoring thresholds are
observational data. Missing one records evidence and continues. They do not
authorize timeout, termination, cancellation, retry, blocking, failure, or a
checkpoint transition. A numeric hard limit exists only when the operator or a
named safety contract explicitly declares that exact limit; never infer one from
a benchmark or from a requirement that only startup, fan-out, context, or another
specific dimension be bounded.

Implementation workers, repair code fixers, and repair designers use
`gpt-5.6-terra` with `medium` reasoning. A model, phase, role, or reasoning
mismatch fails before resolving or launching Codex. Every spawned process or
agent uses `low` or `medium`; high reasoning is forbidden. Generic phase-flow schemas and debug/synthetic
CLI choices remain limited to `low` and `medium` because their phase units are
controllers and certification probes, not feature implementation workers.

## Fresh planner startup

`start_planning.py` is the fresh-launch controller. It accepts only a persisted
`dispatch_action: launch` payload with an absolute base-worktree path. In order,
it verifies the base identity, runs direct `git worktree add -b`, initializes
durable state, resolves and embeds the input manifest, binds the exact dispatched
task as a `const` in the frozen attempt schema, and calls `run_exec.py`. The
planner-launch target is under 60 seconds. The controller always writes the
observed duration and whether the target was met, then continues without changing
control flow. The planner process has a separately named 3,600-second process-leak
safety ceiling; it is not the startup target. The caller runs this controller in
the foreground through terminal receipt handling. Success advances to
`PLANNING/plan_validate/ready`; failure writes durable evidence, blocks the
checkpoint and feature queue, and releases the lease before exit. No coordinator agent is spawned
before dispatch, no model is used for worktree setup, and neither `claude` nor
`claude -p` is permitted anywhere in the Codex control path.

After planner success, `start_planning.py` immediately enters
`scripts/run_feature.py` with the persisted dispatch payload in the same foreground
Python process. That controller launches one resumable context-bounded coordinator thread, treats durable state
as authoritative, and does not exit until it has settled the queue to `blocked` or
`done`. App-task messaging is observational and cannot own this transition.
After 55 seconds without a checkpoint-backed phase event, the parent reports
only that the controller session remains open and the phase is unknown, then
reads a zero-timeout queue snapshot. Process liveness is never phase evidence;
the durable checkpoint is the sole phase authority.
The coordinator remains rooted in the feature worktree and receives the recorded
absolute base worktree as an explicit additional writable root, because its
controller artifacts and feature queue are base-local. Preflight rejects a missing,
relative, or non-directory writable root before launching Codex.
The coordinator is explicitly a child of `run_feature.py`; its prompt forbids
invoking `run_feature.py` or `start_planning.py`. The supervised child also sets a
controller-owned environment marker, and `run_feature.py` refuses recursive entry
on that marker before reading or mutating run state.
The coordinator also never invokes `run_exec.py` from its sandbox. It writes one
child spec or a two-to-three-spec invocation batch beneath the run artifact
directory and returns an `invoke` action. The outer `run_feature.py` process
validates the request, executes a batch concurrently, and persists every receipt
or launch error. After the bootstrap turn, the controller resumes that exact
coordinator thread with a compact prompt containing paths to the dispatch,
checkpoint, plan, planning-input manifest, artifact directory, and prior
child-result file. A returned thread-ID change is a controller failure.
Every `run_exec.py` child launch and resume disables Codex's internal
`multi_agent` feature. Child roles cannot spawn or wait on hidden collaborators;
the outer controller is the sole fan-out and join owner.

When the foreground controller is restarted after an authorized blocker, it
derives the next coordinator turn only from the feature run's contiguous,
terminal-success coordinator receipts. Every recovered receipt must name the
same explicit thread ID; the controller resumes that thread at the next turn and
fails closed on a gap, identity change, or turn-1 collision.

## Review-lens contract

The plan owns the ordered review lenses. Every plan begins with
`l1_l2_contract_boundary`, `security_privacy_destructive_behavior`, and
`correctness`, in that order, and may append zero-to-two materially distinct
blast-radius lenses. `integration_consumer_compatibility` is optional when the
changed surface has callers, schemas, persistence, APIs, migrations, or
backward-compatibility risk.
The planner prompt includes the validator's exact canonical lens charges and the
rule that recommended parallelism is forbidden above a 0.60 critical-path share;
these are generated from the same constants used by semantic validation.

Initial code reviews are independent and bound to the same immutable diff.
Each reviewer receives only that diff, its lens charge, and directly applicable
requirements, and must not expand scope. Deterministic grouping may propose
duplicates; one Terra-medium call receives only each candidate group and may
answer only whether its members are the same issue. Confirmed duplicates retain
the highest severity.

Triage is deterministic in declared lens order and reviewer finding order: all
critical findings enter the fix queue; if there are fewer than ten critical
findings, medium findings fill the queue to ten; all remaining medium and low
findings enter tech debt. Before repair, deterministically create a closure
ledger. Group only technically dependent fingerprints and tag each group as
`implementation` or `architectural`. The originating reviewer writes a failing
adversarial closure test through `author_test`, with one to four normalized
repository-relative `allowed_write_paths` that name only its supplemental test
files. Exact pre/post tree comparison rejects any mutation outside that set;
design and targeted reviewers remain mutation-protected. Architectural groups require a read-only repair design
whose schema-bound process output is the immutable artifact, plus independent
approval before edits. The adversarial test, design, and independent design review
each carry a closed repair-effect contract that classifies every governed
lifecycle effect as `must_persist`, `must_remain_absent`, or
`must_remain_unchanged`. The controller compares those classifications before
`ready_for_fix`; mismatch returns the closure to design and consumes no fixer
attempt, regardless of prose approval. For malformed role output, canonical
controller-owned failure checkpoint, blocked queue, failure summary, and failure
event persistence is permitted; success result/receipt, integration, and
dispatcher acknowledgement remain absent, and base Git state remains unchanged.
An operator resolution augments the ledger without rewriting the immutable
originating test evidence. The normal first-attempt route is one controller-owned
`closure-program/1`, so test, design, approval, fix, targeted review, and routine
routing run from ledger state without coordinator turns between stages.
Single-closure repair remains the default; the explicit connected batch below is
the only bounded exception. Each Terra-medium fixer acknowledges the hash of all
prior rejected approaches. Reusing a rejected strategy family is invalid.
When no continuation program is pre-bound, the legal routes `next_ready`,
`retry_fix`, and `redesign` return to the outer controller for a newly bound,
source-hashed program. Missing that optimization edge is not
`routine_program_missing`; every unknown unbound route remains fail-closed.
Targeted closure by the originating reviewer reports `fixed`, `not_fixed`, or
`regression`. Legacy unbound ledgers rerun every previously closed test;
graph-aware repairs use the deterministic affected component. Three rejected
repair strategies, including architectural designs
rejected before a fixer starts, trigger reassignment, decomposition, or operator
escalation. Preserve their receipts, hashes, families, and review evidence in the
ledger; a design rejection consumes no fixer attempt but does consume the bounded
strategy budget. The counter does not itself close or fail the finding. Other unresolved findings may enter tech debt only if required tests
pass. A fresh terminal integration review must bind to the final tree and is
invalidated by any later code edit.

An operator may reconcile a sandbox-versus-immutable-fixture contradiction with
a digest-bound disposable-test compatibility profile only when the resolution
fixes the exact source hash, verifier role, mutation path, operation and bytes,
requires complete role-view fingerprint rejection, and is explicitly unavailable
to production selection and caller-controlled claims. The controller rejects a
missing or broader profile before returning the closure to design.

If production and fixture invocations share those exact bytes, the digest profile
does not establish invocation identity. A further operator resolution must bind
the profile to a controller-issued, single-use invocation attestation naming the
exact immutable test node and source digest. The attestation must be
controller-minted, role-invisible, unavailable to caller or production
selection, and fail closed on absence or mismatch. Caller, payload, environment,
scenario, and test-process claims are not trusted invocation attestations.

When the unchanged fixture cannot receive such an attestation through any
controller-owned boundary, the contract remains impossible. A separate operator
resolution may revise only that exact fixture to launch through a
controller-owned anonymous capability channel. Bind the authorization to the
original test source hash and exact node; keep ordinary dispatch, marker source,
and assertions unchanged; keep the capability single-use and invisible to role
subprocesses; prohibit caller and production selection; and fail closed on
absence, reuse, or mismatch.

An operator resolution changes the governing contract rather than erasing prior
evidence. Preserve every historical design and fixer rejection, record their
counts as resolution baselines, and charge only subsequent rejections to the
fresh bounded design and fixer budgets.
Pre-resolution design rejections remain immutable provenance but become the
baseline for the newly authoritative operator contract. They do not immediately
re-exhaust the new contract's design budget; only designs rejected after that
resolution count toward its bounded escalation threshold.

Graph-aware repair ledgers use `repair-dependency-graph/2` to source-bind code
surfaces, immutable tests, and dependency reasons. Scheduler limits are explicit
run-owned configuration, not benchmark-derived defaults. Dependency-ready
closures are ordered by ready age and then bounded retry penalty, so a blocked
or repeatedly rejected cluster delays only its descendants. A
`repair-batch/2` contains at most three closures and is valid only for a
connected component, exact union write set, disjoint excluded fingerprints, and
independent originating reviewers. Its selected commands are closed
`test-command/1` objects. Python/pytest commands bind the certified absolute
interpreter; exact generic non-pytest argv remain byte-preserved.

The post-fix controller selects the transitive affected component. Before
targeted review it records, in order, `forbidden_access`,
`pre_communication_output_bound`, `process_evidence`, `capability_manifest`,
and `production_certification`. The controller-owned certification gate runs
each selected command exactly once through the manifest-bound host broker and
writes broker, policy, runtime, command, output, and scratch hashes. Reviewers
consume that receipt and do not rerun Seatbelt. Model reviewers disposition
only the batch findings; deterministic gate evidence replaces unrelated
closed-peer Booleans.

Routine redesign, retry, configured escalation, and next-ready transitions are
closure-driver state changes. Coordinator judgment is restricted to the four
enumerated reasons in the coordinator result schema. When an authorized
run-owned configuration supplies turn and input-token-slope limits, phase,
closure, and turn boundaries produce `coordinator-rollover/2`. Provider-owned
terminal usage in process receipts is the sole accounting authority; absent
usage is `unknown`, never zero, and blocks configured-limit arithmetic. The summary
hashes checkpoint, ledger, graph, receipts, decisions, unresolved judgments,
and the package digest. Recovery never resumes the prior segment's thread; the
fresh thread must echo the exact summary hashes before judgment.

Before any repair model, the controller validates the source-hashed immutable
assertion map and solves its canonical effect assignments. The exact test source
hash, node, command, repository identity, closure, and feature must still match.
One effect cannot carry incompatible dispositions. Contradictions are
deterministic blockers and consume no model call.

`review_closure.py resolve-legacy-assertion-conflict` is the only recovery path
when an independent verifier proves that a legacy test cannot establish its
canonical effects. It requires operator authority, the exact run-owned blocked
verifier result path/hash, the unchanged effect contract, and a hashed
authorization. It archives the superseded test and reopens only the test/design
cycle; it cannot bless the rejected map or delete historical attempts.

Planning records `capability-manifest/2` from the host Seatbelt broker. The
manifest binds broker path/hash, probe-policy hash, and a certified
Python/pytest runtime; it proves host availability but is not per-invocation
authority. Repository write denial and ephemeral test-runner scratch are
separate capabilities; simulated evidence cannot satisfy the gate. Exact
selected commands are authorized only by the controller-owned
`production_certification` receipt. Per-invocation
scratch is controller-derived outside repository authority, private and empty
at launch, transported through the supervised child environment,
content-hashed at terminal, and then removed.
This scratch authority is distinct from the bounded `author_test` repository
write set; scratch never expands `allowed_write_paths`.
The terminal audit records internal symlinks only when their resolved target is
beneath the private scratch root; pytest's internal current-run link is valid,
while broken, cyclic, and escaping links are terminal failures. Capability
probing binds the exact Python/pytest runtime identity; the later
`production_certification` gate runs each selected command through the bound
broker and proves terminal scratch behavior.

All new operator resolutions are generic run-owned profiles. They bind the
active repository/feature/closure/test/assertion subject and carry an executable
controller-only anonymous-pipe proof. Absence, reuse, mismatch, caller
selection, role visibility, and production selection fail closed. The ledger
records the profile hash and activates one design baseline and one fixer
baseline exactly once for that hash while retaining all historical arrays.

## Blockers

Record `blocker_class`, `resume_condition`, and `resolution_evidence`. Operator/architecture blockers require a matching resolution token. Repeated-failure blocking is permitted only after the closure ledger proves three distinct rejected strategies and an explicit escalation action; a raw retry count is insufficient. Gate failures require a new successful receipt on the same corrected tree. State corruption never auto-restarts over an unknown tree.

Once the feature queue controller has validated the blocked feature's token, exact run
identity, surviving checkpoint and transaction hashes, and nonempty resolution
evidence, `run_feature.py` records that dispatcher authorization in checkpoint
history and reopens only the same phase/detail at `ready`.

One additional `blocked -> ready` transition exists: a **delta-scoped retry**
([references/delta-scoped-retry.md](delta-scoped-retry.md)). When the blocked
position is at or after `REVIEWING/fix` and the authorization carries a frozen
`implement-v13-codex/delta-resume-scope/1` document, the controller verifies
the worktree HEAD equals the scope's verified candidate commit and that the
frozen review ledger is byte-identical, then rewinds the checkpoint to
`REVIEWING/fix` at `ready` in one CAS transition. The retry must close exactly
the recorded open closure fingerprints and re-verify with the recorded
verification slice; it never re-runs implementation, and `COMMITTING` gates
still run once after every open finding closes. No other `blocked -> ready`
transition is valid, and the ordinary resume never skips a detail.

## Process receipts

`run_exec.py` uses a gated wrapper:

1. Preflight the validator, schema and supported local references, semantic
   inputs, timeout, supervisor, working directory, and Codex identity without
   creating an attempt receipt.
   Preflight also rejects every `const` or `enum` schema node without an explicit
   `type`, matching the Codex structured-output requirement that local
   `jsonschema` validation alone does not enforce.
   Compile every scalar `spec.expected` field into the attempt schema as a typed
   top-level `const`; an absent field, incompatible type, or conflicting
   const/enum fails before the child exists. Preserve the source schema hash
   separately from the bound attempt schema hash.
   When the current Python lacks `jsonschema`, invoke control-plane scripts with
   `uv run --no-project --with 'jsonschema==4.26.0' python`. Do not create or
   sync a project environment. A new `uv.lock` or any other preflight-created
   repository file is a dispatch blocker.
2. Freeze prompt and validated schema attempt artifacts; use the prompt for child
   stdin and the schema for both validator and child, then persist `prepared`
   without PID and run the gated lifecycle.
3. Accept only terminal, schema-valid, semantically valid output and hash its
   prompt, schema, executable, stdout, stderr, output, child spec, and exit
   record. Process receipt `/3` derives provider usage only from the bound
   terminal event stream and records missing usage as explicit unknown/nulls.

Wall timeouts may terminate only the recorded group after fingerprint verification. Nonempty stderr is evidence, not failure by itself. JSONL silence is not an idle-timeout signal.

Fresh calls use `-C` and `--sandbox`. Resume calls name an explicit thread ID,
set subprocess cwd, and restore sandbox policy through configuration. Never use
`--last`. A terminal-success receipt is revalidated and reused without launch;
a nonterminal receipt is reconciled against PID, process group, start
fingerprint, and the wrapper's durable exit record. A controller interruption
fingerprint-verifies the owned group, performs bounded TERM/KILL, reaps the
supervisor, audits and removes scratch, and terminalizes the receipt before
re-raising. Only verified whole-batch interruption evidence may atomically
return every matching running closure attempt to `ready_for_fix`; partial or
unverified evidence blocks without ledger mutation or relaunch.

A failed receipt may become `succeeded` without a new attempt only when its sole
failure is the historical unavailable-`jsonschema` error and it already carries
the original semantic-contract digest plus a complete matching artifact hash
manifest. Revalidation requires zero exit, no timeout, complete success events,
matching invocation evidence, and current schema and semantic validity. It uses
compare-and-swap to append recovery provenance and increment the revision once;
all mixed, missing, changed, or invalid evidence remains terminal.

Plan-review roles set `schema_path` to the checked-in
`schemas/plan-review.schema.json`. `run_exec.py` rejects byte differences before
creating an attempt and snapshots the verified schema into the run artifacts.
The coordinator binds the exact task and reviewer in `spec.expected`; it does not
synthesize a schema per run.

## Terminal recovery

The base-local transaction survives checkpoint and worktree loss. Every edge carries evidence such as commit SHA, manifest hash, merge ancestry, or cleanup proof. A crash after merge but before queue update resumes from matching transaction/run IDs. The feature result is written once; the dispatcher acknowledges it once.

Controller migration is a separate forward-only transaction. Its lock order is
migration authority, queue, immutable original dispatch, checkpoint,
transaction, closure ledger, journal. A prepared/validated journal never
authorizes resume or child launch. The committed journal plus matching authority
read-back, package digest, coordinator, and lease authorize exactly one
`resume_existing_run` dispatch to the run-owned `run_feature.py`; the fresh
planning entrypoint rejects it.

The deterministic `run_feature.py` controller settles the model
coordinator's terminal outcome. The model coordinator never invokes
`feature_queue_state.py`, mutates the queue, acknowledges a feature, or releases a lease;
the supervised-child marker makes those calls fail closed. `run_feature.py` uses
the dispatch payload's absolute `queue_path`, coordinator ID, and lease to invoke
guarded block or acknowledgment and reads the queue back before exiting.
