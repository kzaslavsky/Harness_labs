# FRAME lens — CB-2 plan review summary (full report in session transcript)

Criticals: C1 CB2-07 premise false — tests/test_feature_run.py:939 drives the
direct path with gate criteria + argv to succeeded at base; plan mapped the
wrong half of item 10. C2 CB2-06 cannot express gate tuples —
plan_graph_contract.py _require_exact_keys rejects unknown run keys and
plan_approval.py caps timeouts; neither owned. C3 CB2-03's scaled-timeout
branch is a no-op vs the observed failure and would break receipt/digest
binding (keep-list). C4 CB2-03 cannot journal — PlanGraphAudit's typed surface
is closed, file owned only by CB2-05. C5 no node owns pre-existing test
modules (CB-1 did); first assertion-invalidating relaxation is unfixable
mid-run. C6 claims-pin lifecycle names a launcher that cannot run this plan
(regexes/RED_BASE/NODES are CB-1's); retirement check hard-wired to it.
Majors: M1 CB2-08 gate self-referential (owns checker + checked) — dual-phase
red evidence required. M2 reclaim must use append-only reconciliation idiom,
never delete journals. M3 AC-CB205-2 under-specified — use
registration.logical_graph_id, never mint. M4 scaled budget escapes
MAX_TIMEOUT_SECONDS (moot under exclusive slot). M5 AC-CB202-2 first clause
already true at base. M6 RED_BASE frozen in prose, pinned nowhere mechanical.
Refutations sustained: DAG same-file ordering sound as declared;
precondition-1 merge assertions verified at e605fff; CB2-02's exit-124 is
strictly an improvement downstream; CB2-01 complete at the executor sites.
