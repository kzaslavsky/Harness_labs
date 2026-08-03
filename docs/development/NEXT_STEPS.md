# Next Steps

Status: proposed

## Current prototype slice

The composition layer now carries task-specific text directly from a parent
`ChildRequest` to the child `TaskAttempt`. The controller intentionally performs
no content selection or authorization yet. See
[`pass-through-child-context-plan.md`](pass-through-child-context-plan.md).

## Milestone 1 — minimal conforming harness

Build one end-to-end Codex feature harness that conforms to the architecture,
context, event, decision, checkpoint, Git isolation, and integration contracts.

### Required slices

1. Define the remaining machine-readable run, task, context, result, checkpoint,
   and integration schemas.
2. Implement a deterministic controller with bounded lifecycle transitions and
   atomic checkpoints.
3. Implement isolated worktree and feature-branch creation, guarded commits, and
   verified merge-back to a recorded base branch.
4. Implement bounded planner, worker, verifier, reviewer, and integrator roles
   with validated parent/child messages.
5. Emit schema-valid run events, decisions, artifact hashes, and final metrics.
6. Add crash/resume, failed-gate, stale-base, conflict, scope, and budget tests.
7. Create a small versioned feature benchmark and establish an accuracy and cost
   baseline before optimization.

### Exit criteria

- A representative feature is implemented in an isolated worktree and merged
  into a disposable base branch only after all gates pass.
- Killing and resuming the controller produces a valid, non-duplicated run.
- A failed verification or unresolved review finding prevents integration.
- Logs reconstruct the task tree, context references, decisions, costs, commits,
  and post-merge proof without secrets.
- Repeated benchmark runs report accuracy and efficiency components with stable
  denominators and documented variance.
