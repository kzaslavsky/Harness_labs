# CB-2 plan — three-lens adversarial review, adjudication (2026-08-13)

Reviewers: three independent Opus agents (NECESSITY, FRAME, MECHANISM lenses)
against `CONTRACT_BURDEN_RELAXATION_2_PLAN.md` at `e605fff`. Per-lens summaries
in `necessity.md`, `frame.md`, `mechanism.md`. Verdicts below are the
coordinator's, with the deciding evidence.

## Accepted — plan surgery

1. **CB2-04 DELETED; item 15 withdrawn.** NECESSITY C1 argued the red scenario
   (all-resolving disjoint-key cycles hitting the ceiling) is unreachable:
   discovery freezes after cycle 1, new-scope findings become `deferred`, and
   the loop exits `succeeded` on empty fix keys before the limit check. The
   coordinator probed the blocked run's own review ledger
   (`cb-graph-cb-exp-1-attempt-2-CB-05`, artifact `a70c5353…`): the two
   cycle-3 blockers were **first raised in cycle 1 and recurred** — the ceiling
   bound on genuine non-convergence, exactly as designed. The item-15 burden
   claim was a mis-diagnosis; withdrawn in the living doc.
2. **CB2-07 DELETED; item 10 general half closed by evidence.** All three
   lenses: no red phase exists. Decisive: `tests/test_feature_run.py:939`
   (`test_gate_backed_criterion_is_satisfied_by_verification_not_claim`) drives
   the DIRECT `run_feature_worktree` path with gate criteria + argv to
   `succeeded` at RED_BASE; `completion_failures` excludes gate-backed criteria
   unconditionally. The residual is a composition recipe (verify-free segment
   schema), recorded by CB2-08's doc closure.
3. **CB2-03 re-aimed at the exclusive gate slot; scaled-timeout branch
   deleted.** NECESSITY C3 + FRAME C3: the inner `--timeout 700` killed CB-06,
   it lives inside the digest-bound `verification_argv` which runtime must
   never rewrite (keep-list); the outer budget never fired. FRAME C4 + MECH M9:
   slot journaling needs `plan_graph_audit.py`; slot plumbed to the
   verification stage needs `feature_run.py` — both added to owned paths (DAG
   already serializes).
4. **CB2-02 halved to script-only.** NECESSITY C2 + FRAME M5: the adopted
   classifier already checks `timed_out`/exit-124 before text rules
   (`feature_run.py:451-462`); the whole fix is `red_green_check.py` exiting
   124 with top-level `timed_out`. MECH M5 (measured): base classification is
   tail-length-dependent (`product` or `indeterminate`) — red asserts "not
   `infrastructure_transient`", never `== product`.
5. **CB2-06 gains contract/approval ownership + digest totality.** FRAME C2:
   `plan_graph_contract.py` rejects unknown run keys and `plan_approval.py`
   caps timeouts — both now owned. MECH M7: `gate_digest` must become total
   over the declared verification shape or every gate-tuple node collides at
   `gate_digest(())`, blinding gate-change authority. NECESSITY M2: the ledger
   spends nothing on import — AC re-aimed at per-gate classification/retry and
   per-gate strict-subset renewal in `_verify_with_recovery`. NECESSITY M4:
   repair re-verifies the full tuple from gate 1 (keep-list).
6. **CB2-05 rewritten.** FRAME M3 + NECESSITY M5: no id minting — default from
   `registration.logical_graph_id`, resume resolves from the predecessor's
   persisted binding. MECH M2 + FRAME M2 + NECESSITY m1: reclaim is
   conjunctively gated (no allocation event, liveness probe clear, flock held),
   never deletes (rename + journaled invalidation via the existing append-only
   reconciliation), no "reported reclaimable" escape. ACs split per defect;
   one red test method per defect class.
7. **CB2-08 hardened.** FRAME C6 + M1, MECH M3: rule-1 exemption stated; gate
   is a dual-phase retirement check (exit 1 against RED_BASE tree, 0 on
   candidate) with explicit `verification_argv` override, plus the FULL SUITE
   as regression so the sink join is verified in a repairable node (MECH M8).
   Closure assertions require `landed (CB2-` + 40-hex sha + no residual open
   status per item. Claims-pin lifecycle rewritten: the pin retires from the
   inert CB-1 launcher only; the live CB-2 runner keeps its pin for the whole
   program (frozen harness), post-program operator step tracked in the doc.
8. **Every node owns the existing test modules for its owned sources**
   (FRAME C5) — CB-1 did this; CB-2's omission made any assertion-invalidating
   relaxation unfixable mid-run.
9. **Program rules extended** (MECH M4/M6/M10/M12, refutation 4): red-tail
   evidence pasted per node summary with FAILED nodeids matching the AC
   enumeration; red constructions inside test methods/`setUp` only, no pytest
   fixtures; measured baseline recorded (suite 42s, gate cycle 2.2s — 1400s is
   a hang detector, not a load estimate); third-consecutive-timeout = recover,
   don't repair; lineages resume under the semantics they started with.

## Rejected / refuted

- MECH M11's verdict-aware classifier mapping: rejected as gold-plating once
  exit-124 makes the existing structured rule fire (NECESSITY smallest-change
  wins); revisit only if verdict-field classification proves necessary live.
- FRAME's CB2-07 retarget (build-segment verification-scoped dispatch):
  rejected — the direct-path base test shows the dead-end is avoidable by
  schema composition; no mechanical rejection site remains to relax.
- NECESSITY's "drop `plan_graph_budget.py` from CB2-06": overridden by MECH
  M7's digest-collision proof; the file stays owned for `gate_digest`
  totality only.
- Lens self-refutations R1–R7 (FRAME), 1–6 (MECH), R1–R6 (NECESSITY) accepted
  as recorded: DAG same-file ordering is sound; precondition-1 merge
  assertions hold at `e605fff`; exit-124 downstream is strictly an
  improvement; CB2-01's red is reachable and well-grounded.
