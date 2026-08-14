# NECESSITY lens — CB-2 plan review summary (full report in session transcript)

Verdict: program 30-40% larger than the verified defect set. Criticals:
C1 CB2-04 red unreachable (discovery frozen cycle>=2; deferred excluded from
fix_keys; loop exits succeeded before limit) and ACs mutually incompatible —
verify-then-delete. C2 CB2-02's classifier half duplicates CB-03 machinery
(feature_run.py:451-462 already structured-first); fix is script-only.
C3 CB2-03 conflates outer (1800s, never fired) with inner (--timeout 700,
killed CB-06) budgets; inner is digest-bound argv (gate_digest) — mandate
exclusive gate slot, delete scaled-timeout branch.
Majors: M1 CB2-07 3/4 ACs already true on base (pairing legal on direct path;
real rejection is verify-segment schema composition at feature_run.py:553).
M2 AC-CB206-2 mislocated (ledger import spends no budget) — re-aim at per-gate
classification + per-gate strict-subset renewal in _verify_with_recovery.
M3 edges CB2-02→03 and 02→06 violate the plan's own edge rule. M4 per-gate
repair must re-verify full tuple from gate 1 (keep-list). M5 no distinct-id
minting; resolve logical id from predecessor's persisted registration binding.
Minors: m1 drop "reported reclaimable" escape; m2 split AC-CB205-3; m3 one
absolute_cycle_limit knob not a toggle (moot after CB2-04 deletion); m4 shared
claims helper, not two copies; m5 CB2-08 headroom wording.
Smallest-change audit: CB202-2, CB203-1, CB206-2, CB207-1/2/3 satisfiable
without fixing anything; CB204-1 vs CB204-3 contradictory.
