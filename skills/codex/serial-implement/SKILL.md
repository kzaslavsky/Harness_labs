---
name: serial-implement
description: Run multiple feature descriptions sequentially through complete implement-v11 workflows with a durable, resumable queue. Use for serial-implement, an autonomous multi-feature queue, or continuation of an existing implementation queue.
---

# Serial implement

Run features one at a time through `$implement-v11`. Use
`docs/development/serial_implementation_queue.json` when present; otherwise create a
queue with `base_branch`, `features`, `current_index`, `started`, and `results`.

1. Parse a new queue or resume an existing one without discarding unknown fields.
   Select the first `pending` or `in_progress` feature and persist it as in progress.
2. Invoke `$implement-v11` with the queue's verified base branch and feature
   description. Do not duplicate or bypass its internal gates.
3. Re-read both queue and implementation checkpoint from disk after each run. Mark a
   feature done only with its observed success evidence and no active checkpoint.
4. On a blocker or failed repository invariant, mark the feature blocked, preserve
   the queue, report the evidence, and stop. Never skip to a later feature.
5. Before advancing, verify the branch, clean-tree, and worktree invariants required
   by project policy.

When all features complete, report a compact results table and validation evidence.
