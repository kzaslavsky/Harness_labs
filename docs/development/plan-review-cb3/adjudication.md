# CB-3 plan — three-lens adversarial review, adjudication (2026-08-14)

Reviewers: three independent Opus agents (NECESSITY, FRAME, MECHANISM lenses)
against `CONTRACT_BURDEN_RELAXATION_3_PLAN.md` v1 at `035c1f3`, RED_BASE
`b49c194`. Per-lens reports in `necessity.md`, `frame.md`, `mechanism.md`.
Verdicts below are the coordinator's, with the deciding evidence.

## Accepted — plan surgery

1. **CB3-05 DELETED; item 19's escalation half withdrawn.** NECESSITY +
   MECHANISM, independently: the claimed defect does not exist. The kernel
   already answers `operator_input.requested` by setting `status = "blocked"`
   (controller_kernel.py:1320-1322); the dispatcher's `status != "running"`
   guard then SIGTERMs the coordinator session (rc 143) and the run reaches an
   orderly terminal `blocked` with the question journaled — exactly what the
   node promised to build. The CB2-08 attempt-3 journal shows this end state
   verbatim; `error_during_execution` is transport noise from the SIGTERM, not
   a session death mid-question. No operator-channel code exists anywhere, so
   the attended/headless distinction (AC-CB305-3) was vacuous. Living doc
   item 19 is corrected at closure: only the restoration half was real.
   (Precedent: CB2-04, CB2-07.)
2. **CB3-01 re-aimed at ref-shape resolution.** All three lenses: the
   command-rejection provenance the node proposed is already landed at
   RED_BASE (`rejected_task_dispatch_refs`, controller_kernel.py:555-559,
   1258-1262; green tests test_relax_kernel.py:480-536; living doc item 6
   marked landed). The *live* defect is different: all 9 `unknown provenance
   reference` failures in the CB-2 corpus cite refs to entities that exist in
   kernel state but are not resolvable — `task:<id>`, `system-result:<id>:<n>`,
   `decision:<id>` — plus 3 wrong-guess `command:<task-id>` refs proving the
   landed feature is undiscoverable. Re-aim: resolve refs to existing kernel
   entities and enumerate valid ref shapes in the rejection message. Dropped
   as incoherent/out-of-grant: the no-budget-charge AC (no budget ledger in
   the kernel; budgets live in plan_graph_budget/feature_run) and payload-
   digest supersede acceptance (supersede compares 7 frozen-authority fields
   against a task record; a digest cannot satisfy it).
3. **CB3-02 gains `feature_run.py` ownership; divergence confirmed as
   issuer/enforcer asymmetry across FOUR copies.** MECHANISM: issuer checks
   `changed_paths` only (agent_mixture.py:333-335; feature_run.py:1528-1601 —
   the copy that granted the CB2-03 specimen); enforcers check paths AND
   per-file sha256 (claude_task_executor.py:501-506 ≡ controller_live.py:
   589-594, byte-identical). Without owning `feature_run.py` the specimen red
   is unbuildable in-grant (NECESSITY). FRAME C2: `tests/test_relax_adoption.py`
   (densest grant-path assertions, incl. :667 asserting the generic message on
   an under-covering grant) and `tests/test_feature_run.py` added to owned
   paths. AC-CB302-1 reworded: the unified check is (receipt, workspace-state)
   at time-of-check, run identically at issue time and preflight; divergence
   between the two runs journals a typed workspace-drift refusal.
4. **CB3-03 re-aimed at the scheduler chokepoint; recency dropped.**
   NECESSITY: per-dispatch grant resolution already exists in
   `_controller_dirty_baseline_grant` (agent_mixture.py:286-306); CB-2 hit
   item 17 because its runner built executors directly and the dispatch
   chokepoint `controller_scheduler.py:123` (unowned in v1) never mints
   grants. Owned paths now: controller_scheduler.py, controller_live.py,
   agent_mixture.py + their tests. MECHANISM: the evidence catalog has no
   ordering (sha256 refs, no timestamps), so "latest receipt" is underivable —
   replaced with content-determined selection: any workspace-change receipt
   whose paths+content exactly cover the current dirty state (coverage is
   unambiguous because content must match disk). "Never union receipts" kept.
   The false eligibility claim on the feature_run path removed.
5. **CB3-04 rebuilt on git baselines, not receipt pre-images.** MECHANISM:
   receipts store kind/sha256/size only (`_path_state`,
   git_transaction.py:364-379) — pre-images do not exist, and the CB2-05
   specimen produced ZERO receipts (the out-of-grant refusal at
   claude_task_executor.py:275 precedes the receipt write at :290), so v1's
   receipt-gated restoration could never fire on the specimen it cited.
   Rework: the controller records the attempt-start baseline (clean tree at a
   known commit) at dispatch; on a failed attempt it restores via git
   (checkout of tracked modifications + removal of attempt-created untracked
   files), journaled per-path, gated on: attempt terminal failed, attempt
   started clean, no newer attempt started (controller-local bookkeeping —
   observable in owned paths), and no covering receipt exists (a covering
   receipt means adoption via CB3-03 is preferable to destroying the work).
   FRAME C5's keep-list concern (restoring away gate-passing floor-refused
   work) is accepted as a residual cost: without restoration that work is
   equally lost plus the node deadlocks; the adoption-preference gate
   minimizes the destructive case.
6. **CB3-06 shrunk to anchor screening + transfer preservation; receipt
   discharge deferred.** NECESSITY: screening machinery largely exists
   (review_fix.py:504-512) — the live gap is the guard tests `required_paths`
   only, never `file` (:388), so the CB2-02 finding shape escapes; and the
   `contract_violation`/`requires_disposition` escape feeds `open_required()`
   (:341-347) → blocked. FRAME C3: v1's annotation-only handling would break
   the landed item-9 transfer (review_fix.py:187 transfers `open` outcomes
   only; assertions in test_feature_run.py:1304, test_plan_graph.py:249) —
   screened out-of-grant findings must remain transfer-eligible while excluded
   from this node's fix_keys and cycle-limit arithmetic. The
   discharge-by-receipt verb (v1 AC-CB306-1) is deferred: no live specimen
   requires it once anchoring is screened, and "controller-owned" is
   undecidable on `EvidenceRecord` (no ownership field) without new schema
   (MECHANISM). Recorded in the living doc as the deferred half of item 18.
7. **DAG rebuilt; all same-file sibling overlaps ordered.** FRAME C1 +
   NECESSITY M1: non-disjoint sibling joins are a hard `PlanGraphError`
   (plan_graph.py:1782-1788). v2: roots CB3-01 ∥ CB3-02 (disjoint); CB3-03
   and CB3-06 both depend on CB3-02 and are mutually disjoint (may admit as a
   pair); CB3-04 depends on CB3-03; CB3-07 sink. `max_parallelism = 2` kept.
8. **Program rules corrected.** Measured baseline updated to the measured
   61.2 s suite (MECHANISM). FRAME M1: the CB-3 runner does not exist yet —
   precondition 3 now states it is authored before decomposition freeze,
   cloned from the CB-2 runner's final (pin-retired) shape with the evidence /
   path-grant / summary-floor pins restored from birth, and verified by
   operator inspection at approval (a plan AC cannot bind unowned
   infrastructure).

## Rejected / noted

- FRAME C6 (receipt-discharge freshness binding): moot — the verb is deferred
  entirely (surgery 6).
- MECHANISM's dispatcher-SIGTERM cosmetic noise (rc 143 `aborted_streaming`
  after an orderly block): real but not worth a node; recorded in the living
  doc's "related smaller findings".
- Lens self-refutations accepted as recorded in the three reports.
