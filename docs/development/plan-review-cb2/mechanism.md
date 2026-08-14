# MECHANISM lens — CB-2 plan review summary (full report in session transcript)

Measured: full suite 42s at base; full red+green gate cycle 2.2s.
Criticals: M1 CB2-07 red infeasible (no restriction exists on base; spike then
retire). M2 CB2-05 reclaim destructive against possibly-live successor —
gate on (no allocation event) AND (liveness probe clear) AND (flock held);
rename, never delete; journal the reclamation. M3 CB2-08 violates rule 1 —
state the exemption, use explicit verification_argv override, require the
extended check to exit 1 against the pre-node tree (dual-phase red evidence).
Majors: M4 red gate proves ">=1 FAILED", not which — require red-tail nodeids
to match the AC enumeration; one test method per defect class. M5 base
classification of a timeout verdict is tail-length-dependent (product at short
tails, indeterminate at 1200-char tails; both charge node_gate_limit) — red
must assert "not infrastructure_transient". M6 program-rule budget numbers
contradict the checked-in CB-1 launcher; the CB-2 runner must be named as
frozen program infrastructure with RED_BASE recorded; base_commit(registration)
!= RED_BASE — say so. M7 gate_digest hashes verification_argv only — gate
tuples all collide at gate_digest(()); digest must be total over the declared
shape (registration/reserve/resume call sites enumerated). M8 the 3-way sink
join is first exercised at graph functionality (no repair path) — sink gate
must include the full suite. M9 exclusive slot needs no request timeout field
(preferred); scaling would need FeatureRunRequest plumbing across unowned
files.
Minors: M10 exit-124 downstream safe (verified; free env retries capped at 2 —
third consecutive timeout means recover, not repair). M11 verdict-mapping
alternative recorded. M12 frozen-harness resume assumption holds; lineages
must resume under the semantics they started with. M13 retirement-check
regexes are brittle (landed \(CB- misses CB2-; items 8/10 are "landed in
part", not "open").
Red-feasibility refutations: TypeError in test method or unittest setUp counts
as FAILED (verified empirically); module-scope raises are collection errors;
parallel-graph stub tests run in ~2s; classify_verification_failure importable
at base.
