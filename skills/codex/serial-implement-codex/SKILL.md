---
name: serial-implement-codex
description: "Dispatch a durable sequential feature queue to implement-v13-codex with pause enforcement, explicit engine adoption, authorized blocker resume, planning-input forwarding, and verified transaction acknowledgment. Use for new or resumed Codex-native serial implementation queues."
---

# Serial Implement Codex

Own only `docs/development/serial_implementation_queue.json` in the recorded base
checkout. Delegate each feature's complete lifecycle to `$implement-v13-codex`; do
not inspect or implement its internal phases.

## Run the queue

1. Read [references/protocol.md](references/protocol.md) completely.
2. Use `scripts/serial_state.py inspect QUEUE` before mutation.
3. If `paused` is true and the operator explicitly says to start, proceed with, or
   resume the currently reported pending feature, invoke `dispatch --clear-pause`
   immediately. That instruction is sufficient authorization; do not ask again or
   reconsider whether it overrides the stored pause. Otherwise stop without
   changing the queue and present `pause_reason` and `run_directives`.
4. Before dispatch, use `add-input` for an operator-supplied plan or contract not
   already listed in the queue. A `seed_plan` is reviewed, never pre-approved.
5. Treat feature `codex_engine: "v13-codex"` as the explicit Codex opt-in. It is
   additive and leaves Claude's existing `engine` field unchanged. For an older
   queue without that field, adopt a foreign engine only on an explicit operator
   request; use the exact token printed by `inspect`. Adoption adds
   `codex_engine` to matching `pending` entries and never rewrites `engine`.
6. Do not spawn a coordinator before dispatch. Run `dispatch --output` first with
   the absolute base-worktree path; it rejects a detached or wrong-branch base
   before mutation and atomically persists the exact payload. Use `--clear-pause`
   only for the explicit authorization in step 3; pause clearance and dispatch are
   one queue transaction.
   Fresh dispatch also copies and verifies the bounded controller package beneath
   the run artifact directory. Execute only the returned
   `controller_entrypoint`; its manifest digest is part of the queue and payload.
7. When `dispatch_action` is `launch`, immediately invoke
   `$implement-v13-codex`'s `scripts/start_planning.py` with that file. The
   deterministic startup controller creates the worktree and branch directly,
   performs bounded preflight, embeds the resolved planning packet, and launches
   the planner. Run it in the foreground and retain parent-task ownership until
   the command is terminal; if the shell call yields a live session, wait on that
   same session rather than returning or starting a background process. Success
   must leave the checkpoint at `PLANNING/plan_validate/ready`; failure must leave
   both checkpoint and queue durably blocked and release the lease. On planner success the same executable
   process immediately enters `run_feature.py` and remains foreground until the
   queue item is terminal; there is no parent-owned transition between planning
   and feature execution. Read each flushed `controller.phase` JSON event as a
   checkpoint-derived phase report. The open shell proves only controller
   liveness, never that planning is still active. If 55 seconds pass without an
   explained phase event, run `serial_state.py wait QUEUE --timeout-seconds 0`
   and use that durable snapshot as the sole phase authority; until it is read,
   report only “controller session remains open; phase unknown.” Do not inspect source, read `CLAUDE.md`, or invoke
   `claude`/`claude -p` on this path. An active matching lease returns `reattach`
   and must never launch a second planner.
8. Invoke `$implement-v13-codex/scripts/run_feature.py DISPATCH.json` directly
   only to recover an existing successful-planner checkpoint whose startup
   process ended before this deterministic chain existed. Do not spawn an app-task coordinator
   or replace the executable controller with prose handoff.
   A migrated blocked run is different: after the migration command is committed
   and `resume` validates its receipt, the next dispatch is
   `resume_existing_run`. Execute only its run-owned `controller_entrypoint` with
   `--resume-existing-run`, the exact migration/package hashes, coordinator ID,
   and lease ID. Never pass it to `start_planning.py`.
9. External monitoring uses one `wait` snapshot followed by
   `wait --since FINGERPRINT`, reusing the fingerprint after timeouts. This
   passive observation boundary avoids repeated full-state and cache reads; it
   does not own lifecycle progress. Inspect full artifacts only on a changed or
   terminal packet. Process liveness is never evidence of a feature phase.
   Never start another feature while the current item is
   `in_progress` or `blocked`.
10. The deterministic `run_feature.py` controller settles terminal queue state:
   guarded `block` for a durable blocker, or `ack` after the immutable feature
   result and terminal transaction. Its model coordinator child never invokes
   `run_feature.py` or `serial_state.py` and cannot mutate the queue. The dispatcher
   verifies resulting state through `wait`; both outer-controller operations are
   idempotent after interruption.
11. Continue immediately with the next item. Preserve the final queue snapshot before
   removing a completed active queue.

Forward a JSON phase-flow specification only as an explicit planning input or
additive dispatch extension; never reinterpret it or derive queue state from it.
Reject orchestration-only debug or synthetic results regardless of their internal
success. Only the production `feature-result.v1.json` plus terminal feature
transaction may satisfy `ack`.

The dispatcher performs only read-only Git identity checks before dispatch. It
never rewrites unknown queue, feature, result, or transaction fields.

## Commands

```text
python3 scripts/serial_state.py inspect QUEUE
python3 scripts/serial_state.py wait QUEUE --timeout-seconds 0
python3 scripts/serial_state.py wait QUEUE --since SHA256 --timeout-seconds 55
python3 scripts/serial_state.py add-input QUEUE INPUT.json --index 0
python3 scripts/serial_state.py adopt QUEUE --from-engine solo \
  --token 'adopt-pending:solo:v13-codex'
python3 scripts/serial_state.py dispatch QUEUE --base-worktree-path /ABSOLUTE/BASE/WORKTREE \
  --coordinator-id AGENT_ID [--clear-pause] --output /ABSOLUTE/PATH/dispatch.v1.json
uv run --no-project --with 'jsonschema==4.26.0' python \
  /ABSOLUTE/PATH/implement-v13-codex/scripts/start_planning.py \
  /ABSOLUTE/PATH/dispatch.v1.json
uv run --no-project --with 'jsonschema==4.26.0' python \
  /ABSOLUTE/PATH/implement-v13-codex/scripts/run_feature.py \
  /ABSOLUTE/PATH/dispatch.v1.json
python3 scripts/serial_state.py block QUEUE --index 0 --coordinator-id AGENT_ID \
  --lease-id LEASE --blocker BLOCKER.json --resume-token TOKEN
python3 scripts/serial_state.py resume QUEUE --index 0 --token TOKEN \
  --evidence RESOLUTION.json --coordinator-id NEW_AGENT_ID --lease-id NEW_LEASE
python RUN_PACKAGE/implement-v13-codex/scripts/run_feature.py RESUME_DISPATCH.json \
  --resume-existing-run --expected-migration-sha256 SHA256 \
  --expected-package-digest SHA256 --coordinator-id NEW_AGENT_ID --lease-id NEW_LEASE
python3 scripts/serial_state.py ack QUEUE TRANSACTION FEATURE_RESULT
```

All mutating commands accept `--expected-revision`; use the revision returned by
the immediately preceding read when another editor may be active.
