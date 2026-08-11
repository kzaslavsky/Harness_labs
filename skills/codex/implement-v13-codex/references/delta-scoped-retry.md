# Delta-Scoped Retry

A delta-scoped retry is an authorized resume of a blocked feature run that
imports the last verified candidate commit and the frozen review ledger, then
re-verifies only the residual delta — the open closure fingerprints and their
bound verification slice — instead of re-running implementation and the full
gate sequence. It exists to stop terminal blocks late in a run (review-cycle
exhaustion, certification blockers) from forcing a whole-run relaunch that
rebuilds already-verified work.

## When it applies

Only when all of these hold:

1. The checkpoint is `blocked` at a position **at or after `REVIEWING/fix`**.
   Earlier positions have no candidate or ledger to import; use the ordinary
   same-detail resume.
2. The review-closure ledger exists and has at least one open closure
   (`status != closed`), and every open closure carries at least one
   `immutable_test_nodes` entry. Without a bound verification slice the delta
   cannot be re-verified and the freeze fails closed.
3. The worktree HEAD equals the verified candidate commit named in the scope.
   The controller proves this with `git rev-parse HEAD` before reopening; the
   candidate is imported by proof, never by mutation.

## The frozen scope document

`review_closure.py freeze-delta-scope LEDGER CANDIDATE_SHA OUTPUT` writes an
`implement-v13-codex/delta-resume-scope/1` document
(schema: `schemas/delta-resume-scope.schema.json`) containing:

- `candidate_commit_sha` — the verified candidate the retry resumes onto.
- `ledger_path` / `ledger_sha256` / `ledger_state_revision` — the frozen
  ledger identity. Any later ledger byte drift invalidates the scope.
- `open_closures` and `open_fingerprints` — exactly the finding keys that
  remain open. The retry must close these and nothing else.
- `verification_slice.test_nodes` / `.commands` — the union of the open
  closures' immutable test nodes: the node-local gate slice re-run for
  re-verification.

`review_closure.py validate-delta-scope LEDGER SCOPE` re-derives the open set
from the on-disk ledger and fails closed on hash drift, fingerprint drift, a
foreign `feature_run_id`, or an empty slice.

## Authorization and transition flow

1. The operator freezes the scope from the surviving ledger and resolves the
   blocker as usual.
2. `feature_queue_state.py resume ... --delta-scope SCOPE.json` performs the
   ordinary token/identity/artifact-hash resume validation, additionally binds
   the scope (protocol, run identity, ledger containment under the base
   checkout, ledger byte hash), and stores it inside the feature's
   `resume_authorization.delta_scope`.
3. `run_feature.py` finds the blocked checkpoint with a delta-scoped
   authorization, revalidates the scope against the on-disk ledger, verifies
   the worktree HEAD equals `candidate_commit_sha`, and calls
   `feature_state.py resume-delta`, which rewinds the checkpoint to
   `REVIEWING/fix` at `ready` in one CAS transition. The blocked-history entry
   records `resume_mode: delta_scoped`, the origin position, the candidate
   sha, and the frozen ledger hash; the scope itself is persisted on the
   checkpoint as `delta_resume_scope`.
4. The coordinator prompt for the resumed run instructs the coordinator to
   close exactly the recorded open fingerprints through the closure machinery
   and to re-verify with the recorded verification slice. The full
   `COMMITTING` gates still run once, after every open finding closes — the
   delta scope narrows the retry entry, not the final bar.

## What it deliberately does not allow

- Resuming from a position before `REVIEWING/fix` (no candidate to import).
- A scope frozen against a ledger that has since changed (byte-hash bound).
- A retry when the worktree HEAD is not the named candidate commit.
- Skipping final certification: `COMMITTING` gates are unchanged.
