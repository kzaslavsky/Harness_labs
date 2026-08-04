**Status:** Current

# Serial dispatcher protocol

## Ownership

The serial dispatcher is the sole queue writer. The feature coordinator owns its
checkpoint, artifacts, and terminal transaction through
`feature_result_written`. The dispatcher owns the final transaction transition to
`dispatcher_ack`.

All JSON updates use a sidecar lock, compare-and-swap on `state_revision`, a
same-directory temporary file, file `fsync`, atomic replacement, and directory
`fsync`. Unknown fields are retained. Queue feature `index` values are opaque.

## Queue contract

Existing queue fields remain authoritative:

- `base_branch`, `features`, `current_index`, `started`, and `results`;
- feature `index`, `description`, and `status`;
- statuses `pending`, `in_progress`, `done`, and `blocked`.

Codex metadata is additive: `protocol_version`, `queue_run_id`, `dispatcher`,
`state_revision`, feature `feature_run_id`, `runner`, `decision_key`,
`decision_record`, `attempt`, `resume_count`, deterministic worktree/artifact paths,
`dispatch_lease`, and the optional feature field `codex_engine`. The value
`codex_engine: "v13-codex"` opts that feature into this dispatcher without changing
the legacy `engine` field. Codex selects the namespaced override. `queue_identity` freezes base branch, protocol, dispatcher,
and queue run ID on first mutation.

New Codex features also bind `controller_package_protocol`,
`controller_package_version`, `controller_package_digest`, and
`controller_package_path`. A migrated feature additionally binds one
`controller_migration_id` and committed journal receipt. Legacy active features
without this identity cannot use the production `resume` command until the
explicit controller migration completes.

`planning_inputs` may occur at queue and feature level. Each is an object with a
stable `id`. Feature entries override queue entries with the same `id`; otherwise
order is retained. Legacy cumulative `run_directives` are not forwarded. Only
queue `active_run_directives` and the active feature's `run_directives` enter the
dispatch payload. The dispatcher does not discover, read, or reinterpret planning
documents.

Every dispatch requires an absolute `base_worktree_path` and forwards it and the
absolute `queue_path` in the payload. Before queue mutation, dispatch requires the queue to be inside that
worktree and requires the worktree to be attached to the queue's exact
`base_branch`; detached and wrong-branch worktrees are rejected. For a paused
queue whose first unfinished feature is pending, an explicit operator instruction
to start that feature authorizes `dispatch --clear-pause`. The command archives
the prior pause reason, clears the pause, and dispatches the feature in the same
locked compare-and-swap transaction. Without that flag, pause remains a hard stop.
Fresh dispatch must be persisted with `dispatch --output` and passed directly to
`implement-v13-codex/scripts/start_planning.py` in the foreground. No coordinator
is spawned before dispatch. The startup controller creates the worktree with direct
Git, embeds the resolved planning inputs, and launches the Codex planner. The
parent task remains attached until the planner receipt is terminal; it must not
return on a prepared, spawned, released, or running receipt. Terminal success
advances the checkpoint to `PLANNING/plan_validate/ready`, while terminal failure
blocks checkpoint and queue with durable evidence and releases the lease. On success, that same foreground Python process
immediately calls `run_feature.py` with the persisted dispatch; no parent task or
model owns this transition. The controller flushes `controller.phase` JSON
events sourced from the durable checkpoint at the planner handoff and later
checkpoint transitions. A live foreground process proves controller liveness
only. After one unexplained 55-second interval, the parent takes a zero-timeout
`serial_state.py wait` snapshot and treats it as the sole phase authority; before
that snapshot it may report only that the controller session is open and phase
is unknown. The controller never reads `CLAUDE.md` or invokes
Claude tooling. The under-60-second planner-launch target is observational: the
startup controller records a miss and continues. It is not a timeout or blocker.
`run_feature.py` launches fresh context-bounded coordinator turns from the
checkpoint and atomically settles the queue before it exits. An app-task label or
message is not a lifecycle owner and must not replace this controller.
Feature implementation workers use the implementation harness's exact
Terra-medium worker identity; all feature-harness agents and processes use only
low or medium reasoning.

## Hard stops

- `paused: true` stops before any migration, ID assignment, or revision change
  unless the current pending feature is atomically dispatched with the explicitly
  authorized `--clear-pause` flag.
- An active feature owned by another engine is never taken over.
- A pending feature is Codex-owned only when its original `engine` already names
  `v13-codex` or its additive `codex_engine` does. Legacy explicit adoption adds
  `codex_engine`; it never rewrites the legacy `engine`.
- The first blocked feature stops the queue until its stored resume-token hash
  matches the supplied token and nonempty resolution evidence proves the exact
  queue, feature, base, worktree, checkpoint, and transaction identity. Resume
  also hashes and parses the surviving checkpoint and transaction; both artifacts
  must repeat the recorded run IDs, feature index, and base branch.
- Invalid state, duplicate active features, or mismatched run IDs block; later
  features are never skipped.

Every new dispatch records a coordinator ID and lease ID. A repeated `dispatch`
must present both values and returns `dispatch_action: reattach`; it never grants a
second launch. Blocking releases that lease. Authorized resume installs a new lease
and permits exactly one resumed launch.

Fresh dispatch returns `launch` and the run-owned `start_planning.py`
`controller_entrypoint`. An explicitly migrated resume returns
`resume_existing_run` and the run-owned `run_feature.py` entrypoint. The latter
is selected once, consumed once, and is never routed through fresh planning.
Resume and dispatch hold the migration-authority lock outside the queue lock and
accept only a committed journal whose package, authority, coordinator, and lease
identity match. The original fresh dispatch remains byte-immutable.
After migration, runtime queue, checkpoint, transaction, and ledger authorities
may advance monotonically. A later durable block remains resumable under the same
committed migration: validation accepts those advances while the feature is
`blocked` or `in_progress`, but still requires the exact run, package, migration,
and revision identities recorded by the journal.

## Low-context supervision

Use `serial_state.py wait` as the dispatcher observation boundary. It watches the
queue plus allowlisted checkpoint, transaction, and feature-result fields and
returns a SHA-256 fingerprint. With `--since`, it blocks for at most 55 seconds
and emits only on a state change, terminal evidence, or timeout. Timeout packets
omit artifact summaries. The dispatcher must not poll by rereading full state or
coordinator history. A changed packet authorizes a targeted artifact read; it does
not itself authorize acknowledgment or another launch. This polling behavior is
for external observation of `run_feature.py`; it prevents repeated cache and
full-artifact reads and never drives the lifecycle.

The adoption confirmation phrase is
`adopt-pending:<source-engine>:v13-codex`. Store only its SHA-256 digest in the
audit record. Completed, active, and blocked entries are unchanged.

## Completion acknowledgment

The feature transaction progresses through:

```text
prepared -> feature_committed -> manifest_committed -> merge_prepared -> merged
-> cleanup_complete -> feature_result_written -> dispatcher_ack
```

`ack` requires the feature package's exact result and transaction protocols,
matching queue/feature/base identity, the ordered terminal transaction history,
and the dispatched transaction/result paths. It verifies the referenced manifest,
merge receipt, clearance report, and cleanup proof as local regular files and checks
transaction-provided SHA-256 values when present. It writes the queue first, then
the transaction. If interrupted between those writes, rerunning `ack` recognizes
the already-completed queue item and finishes the transaction transition without
duplicating the result.

The deterministic `run_feature.py` controller invokes guarded `block` for a
validated coordinator blocker and `ack` after `feature_result_written`. The model
coordinator child cannot invoke `serial_state.py`, mutate the queue, or release a
lease. The outer controller reads back terminal state; the dispatcher verifies it
through the low-context wait boundary, without a parent-task wakeup.

Synthetic and JSON phase-harness debug results are orchestration evidence only.
Their protocols are deliberately distinct from `feature-result.v1.json`; `ack`
must reject them even when every child phase succeeded.

## Resume records

When a feature blocks, retain its current blocker fields in `blocked_history`.
Resume requires `resume_token_sha256` plus resolution evidence. A successful
resume retains `feature_run_id`, increments `attempt` and `resume_count`, records
the authorization digest and evidence, and clears only current blocker fields.
