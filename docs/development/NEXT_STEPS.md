# Next Steps

Status: active

## Current prototype slice

The repository now has a deterministic command kernel, resident model
coordinator loop, selective `RunView`, content-addressed evidence catalog,
capability scheduler, repeated-role parallelism, bounded worker delegation, and
durable restart. Three materially different analysis/planning scenarios exercise
the same controller without scenario-specific kernel branches. See
[`hybrid-controller-coordinator.md`](hybrid-controller-coordinator.md).

## Semantic-plane backlog

The custody layer (receipts, CAS, journaled evidence) is ahead of the
machinery that helps agents succeed. Open gaps, in rough priority order:

1. **Worker→coordinator clarification channel.** A worker that discovers
   task ambiguity mid-attempt can only fail; the whole attempt burns and the
   coordinator re-dispatches with amended context. Design a bounded,
   journaled channel: the worker emits a structured "blocked-on-question"
   artifact, the attempt suspends, the coordinator's answer becomes part of
   the audited context of a resumed or fresh attempt. The controller stays
   the state authority; nothing free-form.
2. **Richer parent→child context handoff.** Decision 0003 deliberately kept
   `ChildRequest.context` a single frozen pass-through string and deferred
   selection, resolution, and packet mechanics. The string can carry JSON
   today, so this is workable — revisit once measured runs show what a
   selector/materializer should actually optimize.
3. **Cross-lane interface coordination in parallel PlanGraph.** Sibling
   lanes only meet at the join; two lanes that make individually-correct,
   jointly-incompatible interface decisions surface the conflict after both
   have finished. Candidate design: advisory interface-note artifacts
   published to the evidence catalog, readable by sibling lanes through the
   existing bounded artifact query — no lane gains authority over another.
   Instrument first: classify `integration_conflict` causes (textual merge
   conflict vs joint-verification failure vs ref race) and aggregate the
   rates in `run_metrics` before building the channel.
4. **Fixer-model escalation in the review/fix gate.** When findings
   repeatedly fail to close across review cycles (or a run blocks with
   "required findings remain open"), the fixer backend may simply be weaker
   than the reviewer that produced the findings. First measure: record the
   fixer backend spec in each ledger cycle entry and compare finding close
   rates by (reviewer backend, fixer backend) pair, separating fixer
   weakness from invalid or underspecified findings. If the asymmetry is
   real, add a deterministic, policy-owned escalation ladder for the fix
   stage (e.g. escalate after a finding survives N fix attempts, and no
   later than the penultimate cycle, since the last permitted cycle is
   review-only). Recovery after a blocked exit can already escalate for
   free: resume with a stronger fixer in the run's agent mixture, and
   journal that the mixture changed.

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

### Implemented hardening

- Durable dispatcher restart restores and validates the existing checkpoint,
  schema hash, evidence, and session history without replaying completed work.
- `run_feature_worktree(...)` owns isolated branch/worktree creation, scoped
  candidate commits, optional guarded merge, and read-back receipts.
- Live write workers require explicit writable paths and produce verified
  workspace-change receipts.
- Default hierarchy limits are depth `5` and direct subagents `5`. Other
  controller and coordinator limits are intentionally unbounded by default
  while representative runs are measured.

Still outstanding from the slices above are a shipped uninterrupted live
FeatureRun certification, failed-gate integration tests at the full entrypoint,
production handler bindings for the capability-broker contract, an authoritative
price catalog for every selected backend, and benchmark baselines.

The portable development-policy contract now supplies source-bound planning,
FRAME/NECESSITY/MECHANISM refutation, curated build handoff, and risk-shaped
review assignments. Segment exit-artifact gates make these enforceable
deliverables. Finalized runs write a hashed `summary.json`; missing backend
pricing remains explicitly unpriced rather than silently counted as zero cost.

PlanGraph parallelization PG-02 pins a graph child lane to its declared
immutable parent commit, seals its candidate without invoking an integration
transaction, binds execution to the allocation's branch and worktree, and
returns the versioned allocation-bound seal receipt required for subsequent
controller-owned adoption. Final graph integration remains graph-owned.

PG-05A reconciles interrupted active allocations from fresh child-owned
liveness without storing it in the graph checkpoint. A matching live
PID/start-token remains running. A dead child is adopted only when its verified
terminal manifest, closed child-request descriptor, allocation-bound seal, and
referenced verification/candidate evidence agree. Ambiguity blocks; a force
record is audited and late evidence after a forced block is quarantined. A
child seal never advances the graph staging head: join integration retains that
custody.

### PlanGraph PG-07 certification handoff

The certification order preserves the PG-00 root
`b152bbe6ccef887c1252e3b7d132e2a531be583b`, followed by the reviewed PG-05B
candidate `c66e196` and PG-06 candidate `7e348f3`. PG-07 records evidence only;
it does not change product behavior or the controller's full-suite command:

```sh
python3 -m unittest discover -s tests
```

The controller receives the integrated candidate plus this worker's non-socket
evidence handoff and must execute that unchanged full suite in its
socket-permitted authoritative environment. Before accepting the full-suite
result, it must provision a Chrome binary, set `DASHBOARD_E2E_CHROME` to its
absolute executable path, and run the browser gate separately; a skipped test
is not passing evidence:

```sh
test -x "$DASHBOARD_E2E_CHROME"
npm --prefix dashboard/plan-graph run build
python3 -m unittest tests.test_dashboard_e2e
```

The controller records a passing execution of that command as required
dashboard E2E evidence. A restricted worker's loopback-server `PermissionError`
does not permit the controller to weaken, skip, or replace this gate.

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
