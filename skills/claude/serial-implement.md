---
description: Run a durable queue of features sequentially through implement-v11
argument-hint: [--base <branch>] <one feature per line>
---

# Serial implement

Run feature descriptions one at a time using `/implement-v11`. Store durable queue
state in `docs/development/serial_implementation_queue.json` when that convention is
present; otherwise create it with `base_branch`, `features`, `current_index`,
`started`, and `results`.

1. On a new queue, parse the optional base branch and non-empty feature lines; on a
   resume, preserve existing fields and select the first `pending` or `in_progress`
   feature.
2. Mark one feature `in_progress`, persist the queue, and invoke `/implement-v11`
   with the queue's base branch.
3. Re-read the queue and implementation checkpoint from disk after every feature.
   Mark it `done` only when the implementation workflow reports its required success
   evidence and leaves no active checkpoint.
4. On a checkpointed blocker or failed repository invariant, mark the feature
   `blocked`, preserve the queue, report the evidence, and stop. Never skip ahead.
5. Before moving on, verify the required branch, clean-tree, and worktree invariants
   defined by repository policy.

When all features complete, report a compact results table and validation evidence.
Do not call a queue complete merely because a subagent stopped responding.
