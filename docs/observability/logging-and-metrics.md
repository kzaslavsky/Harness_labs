# Logging and Metrics Contract

Status: normative

Logging exists to reconstruct behavior and improve the harness. It must support
debugging, evaluation, optimization, and audit without becoming a store for
secrets or uncontrolled prompt transcripts.

## Run layout

```text
logs/runs/<run-id>/
├── events.jsonl       # append-only ordered events
├── decisions.jsonl    # run-scoped material decisions
├── checkpoint.json    # atomically replaced resumable state
├── summary.json       # final metrics and outcome
├── manifest.json      # terminal hashes and artifact inventory
├── .audit.lock        # sole-writer advisory lock
└── artifacts/         # bounded evidence referenced by hash
```

Event and decision records MUST validate against the schemas in `schemas/`.
Sequence numbers are monotonic within a run. Timestamps use UTC RFC 3339.
Artifacts record a relative path, media type, size, and SHA-256 digest.

The executable attempt runner uses the `harness-audit-event/1`,
`harness-audit-checkpoint/1`, and `harness-audit-manifest/1` contracts. Every
event includes a run identity, contiguous sequence, UTC and monotonic time,
actor/parent identity, attempt/session/backend identity where applicable,
duration, evidence classification, evidence artifacts, and the preceding event
hash. The event hash is computed over canonical JSON. The terminal manifest
binds the event-chain head, the final checkpoint, and a complete artifact
inventory.

Writes are fail-closed, append-only for events, `fsync`-backed, and protected by
a sole-writer lock. Checkpoints and artifacts are atomically published. Run
directories are mode `0700`; event, checkpoint, manifest, lock, and artifact
files are mode `0600`.

The treasure scenario additionally records:

- resolved authorization and task-attempt contracts;
- normalized model events, tool calls/results, child lifecycle, and durations;
- Codex executable path/digest, arguments, thread/turn identities, command
  receipts, stdout/stderr, and process/workspace termination proof;
- exact oMLX HTTP request/response bodies and non-secret transport settings;
- exact app-server JSONL in both directions; and
- final task results and child follow-up results.

Raw transport artifacts are intentionally retained for this PHI-free
verification scenario. Never enable that capture for credentials, secrets,
sensitive personal data, or unredacted third-party content. Authentication
files, authorization headers, environment dumps, and raw model reasoning are
not recorded.

Verify a run, or terminalize a nonterminal run after its controller disappeared:

```sh
python3 -m scripts.audit_run verify logs/runs/<run-id>
python3 -m scripts.audit_run recover logs/runs/<run-id> \
  --reason "controller process disappeared"
```

Recovery validates the existing chain and checkpoint before writing a recovery
event, clears active child/session state, and produces an `interrupted`
manifest. It does not silently replay model or tool actions. A printed
`head_hash` can be copied to an independent system as an external anchor;
without such an anchor, a privileged actor who can rewrite the entire run
directory can also regenerate its hash chain.

Long-lived foreground controllers SHOULD emit a compact `controller.phase`
event on each durable checkpoint transition. The event identifies the checkpoint
as phase authority and process state as liveness-only evidence; parent monitors
must not infer phase from an open shell session.

The local PlanGraph dashboard may read a colocated `liveness.json` lease to
classify a nonterminal controller as live. That lease is ephemeral operational
state, not an event, artifact, checkpoint field, manifest input, or summary
metric. A dashboard may call a run live only after confirming a fresh local
heartbeat and matching process-start identity; an absent, remote, stale, or
PID-reused lease is respectively unavailable, remote-unverified, or stale.
Dashboard reads never repair, terminalize, or otherwise mutate a journal.

Runtime logs are ignored by Git. Accepted decisions with durable architectural
impact are promoted to `docs/decisions/` through a normal reviewed change.

## Event principles

Log state transitions, dispatches, results, verification, retries, failures,
budget changes, Git operations, and integration proof. Record actor, task,
phase, status, duration, evidence references, and relevant resource usage.

Feature runs record `git-worktree-transaction/1` receipts for worktree creation,
candidate commit, and optional integration. Write-capable workers record a
`workspace-change-receipt/2` containing the baseline commit, branch, declared
writable paths, baseline/final changed paths, worker-only delta, and file-state
hashes. Review/fix runs persist content-addressed `review-ledger/1` snapshots
and events for review, fix, verification, stopping, and terminal disposition.
These artifacts are content-addressed evidence referenced by the corresponding
audit event.

Every finalized run writes `summary.json` using `harness-run-summary/1`. Token
counts come from authoritative backend completion events, not model-authored
prose, and cached input remains separate. Cost is calculated only from an
explicit `ModelPrice` and recorded source; absent pricing creates an unpriced
usage record and makes `cost_complete=false`. Capability-broker executions
contribute duration and tool-call counts without inventing model-token usage.

The read-only dashboard projects verified `backend_transport` events into run,
phase, agent invocation, agent type, model, reasoning-effort, and backend
breakdowns. Total tokens are input plus output tokens; cached input is reported
separately and is not added a second time. “Peak observed input” is the maximum
`input_tokens` value from one backend invocation, not a claim about true context
window occupancy. Recorded dollar cost remains authoritative. When it is absent,
the UI may show a visibly approximate API-equivalent estimate for recognized
models using published input, cached-input, output, and long-context rates. The
estimate cites its price pages and excludes unobserved tool fees and cache-write
premiums. Unknown models remain unavailable rather than receiving an invented
rate.

Do not log credentials, access tokens, private keys, sensitive personal data,
full environment dumps, or unredacted third-party content. Raw model reasoning
is not a contract artifact; store concise decisions, inputs, outputs, and
evidence instead.

Every run artifact and summary metric MUST classify its evidence as one of:

- `production_lifecycle`: emitted by the real production entrypoint and controller;
- `component`: emitted by an isolated production component test;
- `synthetic`: emitted by a debug, marker, or orchestration-only flow;
- `fabricated_fixture`: constructed directly by a test.

Synthetic and fabricated evidence MUST NOT be aggregated with production feature
completion or used to claim lifecycle conformance.

## Decision threshold

Log a decision when an agent chooses among meaningful alternatives that affect
architecture, scope, quality, safety, cost, permissions, recovery, or integration.
Include the question, selected option, alternatives, rationale, evidence,
consequences, reversibility, actor, and time. Routine deterministic actions need
events, not decision records.

## Metric families

### Accuracy

- acceptance criteria passed / required;
- required gates passed / required;
- weighted evaluation-suite score;
- escaped defects by severity;
- valid review findings and reopened work;
- rework time and changed-line churn after review;
- false success claims and contract violations.

### Efficiency

- wall-clock and critical-path latency;
- summed agent execution time;
- input and output tokens by task and role;
- tool calls, failed calls, and redundant calls;
- retries by class and recovery time;
- agents spawned, useful parallelism, and coordination overhead;
- lines or artifacts changed, reverted, and superseded;
- time from verified candidate to integrated base commit.

### Reliability

- successful resume rate after interruption;
- deterministic replay or repeated-run variance;
- checkpoint corruption or stale-state rejection;
- merge conflict and base-advancement frequency;
- telemetry completeness and schema-validation rate.
- time from dispatch to the first real implementation worker;
- time spent at a nonterminal ready checkpoint without a verified live owner;
- premature parent exits and failed ownership handoffs;
- uninterrupted production-lifecycle completion rate.

## Accuracy × efficiency

The summary MAY publish a composite score:

```text
composite = normalized_accuracy * normalized_efficiency
```

The score is meaningful only when runs share the same versioned task suite,
acceptance criteria, environment, model/tool configuration, budgets, and scoring
method. Publish the component metrics and denominators with the composite.

Required quality gates remain hard constraints. Prefer Pareto comparisons: a
change is clearly better when it improves accuracy without increasing cost, or
reduces cost without reducing accuracy. Treat any apparent gain caused by weaker
tests, omitted work, hidden retries, or missing telemetry as invalid.

## Experiment discipline

Record the harness version, repository commit, configuration, task-suite version,
model/tool versions, randomization where controllable, and collection failures.
Use repeated trials for stochastic components. Compare medians and tail behavior,
not only best runs. Preserve failed runs in aggregates to avoid survivor bias.
