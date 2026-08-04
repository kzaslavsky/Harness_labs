# Next Steps

Status: proposed

## Current prototype slice

The repository now has a deterministic command kernel, resident model
coordinator loop, selective `RunView`, content-addressed evidence catalog,
capability scheduler, repeated-role parallelism, bounded worker delegation, and
durable restart. Three materially different analysis/planning scenarios exercise
the same controller without scenario-specific kernel branches. See
[`hybrid-controller-coordinator.md`](hybrid-controller-coordinator.md).

## Milestone 1 — minimal conforming harness

Build one end-to-end Codex feature harness that conforms to the architecture,
context, event, decision, checkpoint, Git isolation, and integration contracts.

### Required slices

1. Bind live coordinator and worker backend configurations to the generic
   capability scheduler, including browser/UI-graph executors.
2. Extend the working analysis/planning controller through the full feature
   lifecycle without introducing prompt-owned transitions.
3. Implement isolated worktree and feature-branch creation, guarded commits, and
   verified merge-back to a recorded base branch.
4. Add writable-path, network, browser, and external-effect capability brokers.
5. Expand metrics and budgets to tokens, agent time, tool calls, and cost.
6. Add failed-gate, stale-base, conflict, scope, and integration recovery tests.
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
