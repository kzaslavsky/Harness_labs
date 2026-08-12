# Recovery machinery inventory

Status: RB-01 baseline inventory (2026-08-12). This records the recovery
mechanisms present at the start of the retry-budget program, their operative
authority, and the bounded follow-up that owns any gap. It is an inventory,
not an authority grant.

| # | Mechanism | Current characterization | Follow-up |
|---:|---|---|---|
| 1 | FeatureRun abnormal recovery | Bounded `retry`, `adjust_plan`, or `stop` decisions are recorded in the FeatureRun audit. | RB-06 externalizes Tier-1 execution. |
| 2 | `RecoveryAgent` seam | The in-process seam exists, but `adjust_plan` has no production applicator. | RB-05 typed applicators; RB-06 coordinator. |
| 3 | Review recovery retry | Reconstructs the loop after a recovery decision. | RB-01 preserves inherited transfer state. |
| 4 | Review ledger | Holds finding identity, disposition, and cycle history. | No change. |
| 5 | Inherited obligations | Destination review loops can seed transferred findings. | RB-01 wires the production launch seam. |
| 6 | Transfer targeting | Finds the nearest unique downstream owner by path grant. | No cross-plan rename migration. |
| 7 | Transfer validation | Invalid owner, completed owner, and duplicate key are rejected. | RB-01 turns rejection into a resumable block. |
| 8 | Pending-transfer checkpoint | Verified child candidates and proof artifacts are retained before obligation mutation. | Introduced by RB-01. |
| 9 | Transfer-conflict block | A node-level block stops dependent launches and retains resume evidence. | Introduced by RB-01. |
| 10 | Resume invalidation closure | Explicit retry frontiers invalidate dependent nodes and reuse only custody-proven predecessors. | No change. |
| 11 | Repair predecessor validation | Finished predecessor journals and matching registration identity are verified read-only. | No change. |
| 12 | Blocker evidence reference | Resume requires an artifact reference recorded by the predecessor. | RB-04 adds a uniform escalation artifact. |
| 13 | Interrupted child reconciliation | PID/start-token liveness and sealed-manifest custody decide active allocations. | RB-02 reuses this evidence for ledger reconciliation. |
| 14 | `force_records` | This is the sole protocol-versioned operator channel for forced reconciliation. | Preserve as the explicit operator path. |
| 15 | Candidate seal receipt | Parallel child adoption requires candidate, verification, descriptor, and terminal-journal references. | No change. |
| 16 | Integration barriers | Serial completion records retain parent input and integrated candidate context. | No change. |
| 17 | FeatureRun verification repair | Controller-owned verification has its own bounded repair loop. | RB-03 classifies its failures. |
| 18 | Review mechanical limits | Review/fix cycles use mechanical and sensitive limits. | Remains node-local. |
| 19 | FeatureRun recovery limit | Abnormal FeatureRun-stage recovery is locally bounded. | Lineage accounting arrives in RB-02. |
| 20 | Graph retry behavior | Successor graph attempts currently receive fresh local allowances. | RB-02 lineage ledger closes this gap. |
| 21 | No-change recovery checks | Verification and review each independently detect no-progress/no-change states. | Consolidation is deferred; do not generalize in this program. |
| 22 | Budget views | Verification, review, and graph retries are three mutually blind budgets. | RB-02 introduces a ledger; unification remains deferred. |
| 23 | Terminal evidence payload | FeatureRun terminal data contains `review_fix`, including `transferred_findings`. | RB-01 accepts that payload in launcher evidence. |
| 24 | Recovery notification/escalation | No native graph block notification or standard escalation artifact exists yet. | RB-04 hook and escalation contract. |

Deferred consolidation is intentional: no generalized snapshot framework, budget
unification, or RecoveryAgent rewrite is part of RB-01.
