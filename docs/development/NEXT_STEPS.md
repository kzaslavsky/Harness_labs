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

The dependency-free `harness_labs.attempts` module now provides the first
execution boundary: `TaskAttempt -> Executor -> TaskResult`. It validates attempt
references, result type, result identity, and terminal status. It is a primitive,
not a conforming harness; the production controller and vertical slice below
remain outstanding.

### Required slices

1. Build the smallest production vertical slice through the real entrypoint:
   dispatch, plan, implement, verify, review, integrate, report, and acknowledge.
   It must require no operator message after dispatch and must retain a verified
   live owner at every nonterminal checkpoint.
2. Add one end-to-end test with deterministic stub model outputs but the real
   controller, worktree, prompts, checkpoints, receipts, and queue. Synthetic
   marker flows and fabricated terminal artifacts do not satisfy this slice.
3. Define only the machine-readable contracts required by that working vertical
   slice, with a named production consumer and end-to-end assertion for each.
   Treat any prompt-owned transition or terminal handoff as unfinished
   controller work, and repair the failure class rather than adding another
   instruction-only guard.
4. Harden the working path with isolated worktree/branch creation, guarded commits,
   verified merge-back, and validated planner, worker, verifier, reviewer, and
   integrator boundaries.
5. Add policy-controlled writable-path, network, browser, and external-effect
   capability brokers. Preserve the working live Codex scheduler and
   controller-owned browser-command receipt seam.
6. Add schema-valid events, decisions, artifact hashes, and measured token,
   agent-time, tool-call, runtime, and cost budgets consumed by the production
   path.
7. After the uninterrupted path passes, add crash/resume, failed-gate, stale-base,
   conflict, scope, and budget tests.
8. Create a small versioned feature benchmark and establish an accuracy and cost
   baseline before optimization.

### Exit criteria

- A representative feature is implemented in an isolated worktree and merged
  into a disposable base branch only after all gates pass.
- The real production dispatch entrypoint reaches queue acknowledgment without an
  additional operator message or an ownerless nonterminal checkpoint.
- Killing and resuming the controller produces a valid, non-duplicated run.
- A failed verification or unresolved review finding prevents integration.
- Logs reconstruct the task tree, context references, decisions, costs, commits,
  and post-merge proof without secrets.
- Repeated benchmark runs report accuracy and efficiency components with stable
  denominators and documented variance.
