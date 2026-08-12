# Retry-budget lineage, tiered recovery authority, block escalation — staged program plan

**Date:** 2026-08-11 · **Status:** pending operator approval · **Target branch:** worktree branch off latest `Impl-redo` tip, merged back on completion
**Provenance:** motivated by the FR-10 launch audit (pg11–pg34) and the two retry postmortems; revised through three external review rounds (Sol critiques #1–#3, all findings adopted — scope-down decisions recorded below).

## Review-round decisions (recorded)

Three findings were answered with deliberately scoped-down v1 mechanisms; each states what it does NOT build:

- **Plan-version transitions — simplified.** No migration engine. A changed plan does not resume old attempts; it re-registers under the same lineage with a **plan-version transition record** (small, typed: predecessor/successor plan hashes, node correspondence by *identical node_id only*, per-node `budget_carryover`, authorizing decision). Renamed/removed nodes don't map — their budget history stays in the lineage as history; work reuse happens via git candidates as today, not checkpoint reuse. `resume()` continues to require an unchanged plan hash. `revise_acceptance`/`revise_functionality` execute *only* via this route. Not built: node-rename migration, cross-plan-version checkpoint reuse, invalidation-closure transforms.
- **Authority root — registration-embedded allowance.** v1 trust boundary stated plainly: all local processes share the operator's OS identity; the defended threat is **accidental self-widening by a well-intentioned agent, not an adversarial local process**. Given that, a grant subsystem (dedicated subcommand, env marker, action-class consumption machinery) is disproportionate — dropped. The recovery allowance lives **inside the operator-approved registration**, which is already digest-bound and immutable:
  ```json
  "automatic_recovery": {
    "max_extra_node_launches": 2,
    "max_structural_decisions": 2,
    "allowed_actions": ["retry_same_plan", "transfer_ownership"]
  }
  ```
  The recovery agent consumes the bounded allowance (consumption = ledger events, checked against the registration) but cannot modify it — any change to the block changes the registration digest, i.e. is an ordinary audited operator re-registration. Human-tier delegation ("full autonomy") = the operator listing human-tier actions in `allowed_actions` at registration. Decision records reference the registration digest as their authority. Not built: cryptographic signing, OS privilege separation, grant artifacts/subcommands, autonomy-mode flags.
- **One source of truth — the ledger is itself append-only.** The lineage ledger is `run_root/.plan-graph-budgets/<lineage_id>.jsonl`: an append-only event log (reservations, transitions, attempt records, decisions, allowance consumptions) folded into state on open, appended under flock. The decision log is a filtered view of it. No multi-file atomic commit problem; corruption → quarantine + `operator_intervention_required`, rebuildable by scanning lineage attempt journals. Not built: intent/commit protocol across multiple stores.

Straight adoptions from review: serial stage chain (no parallel stages sharing files); every stage fail-closed and **safe deployed alone** — no stage ships a block whose only resolution is a later stage; reservations only in `run()` immediately before launch (`resume()` gets a read-only verdict); reconciliation reuses PID/start-token liveness + seal-proof rules, never directory inference; lock order graph-lock → budget-lock; structured verification counts + globally unique invocation ids imported from child evidence (never prose), duplicate imports are no-ops; pre-classifier failures are `indeterminate` and ledger semantics never reinterpret retroactively; transfer conflicts are node-level recoverable blocks with `candidate_verified_pending_transfer` checkpointed before obligation advancement can throw; terminal statuses backed by structured `status_flags` with compatibility tests; gate identity labeled `gate_identity_v1_incomplete` with ambiguous changes defaulting to carryover or block, never reset; hook argv as JSON array with full output detachment; typed action payloads with applicators only for actions that have real v1 execution paths.

## Context

Audited FR-10 failure (24 launches, pg11–pg34): bounded child repairs became unbounded graph-level retries because (a) `verification_repair_limit` is a local variable of one FeatureRun, (b) each `PlanGraph.resume()` successor gets a fresh allowance with no cumulative accounting, and (c) a fresh registration wipes the slate entirely. The operator's supervising agent polls the process to detect blocks. Blocks are binary — there is no tier where an LLM resolves mechanical plan alterations autonomously, so everything stalls on the operator.

**Governing principle:** autonomous mode may delegate all operator-delegable decisions to the recovery agent — including acceptance/contract changes — when explicitly and prospectively authorized at registration. Deviations are durable and reviewable. No authority beyond the operator's; no bypassing platform security, credentials, or external authorization.

**Execution environment:** all work happens in an **isolated git worktree** on a **new branch cut from the latest `Impl-redo` tip** (`git worktree add <path> -b <branch> Impl-redo`) — never in the primary working tree, which holds uncommitted dashboard/test edits and untracked `experiments/*` embedded repos. Stage gates run inside the worktree. On completion, merge back to `Impl-redo` (standing rule: never strand work on side branches); pushing remains the operator's call.

Live layer: the `harness_labs/` package — NOT `skills/codex/implement-v13-codex/` (deprecated).

## Stages (serial: RB-01 → RB-02 → RB-03 → RB-04 → RB-05 → RB-06)

### RB-01 — Obligation-transfer preservation + machinery inventory
*Paths: `harness_labs/feature_run.py`, `harness_labs/review_fix.py`, `harness_labs/plan_graph.py`, `harness_labs/plan_graph_audit.py`, `docs/development/`, tests.*
- Wire the production seam: `run_plan_graph_feature_worktree` (feature_run.py:829) binding fields → the four `review_*` options on `run_feature_worktree`; `ReviewFixResult.transferred_findings` → terminal payload → launcher stdout evidence → `_transferred_findings` (plan_graph.py:1061). Today this seam closes only in tests.
- Fix review recovery retry (feature_run.py:648-659): rebuild `ReviewFixLoop` **with** inherited obligations/targets/origin/frozen flags — today a recovery retry silently drops them.
- Transfer-conflict handling: replace the finalize-failed path (plan_graph.py:871-880) with a node-level recoverable block. Checkpoint records `candidate_verified_pending_transfer` (child's verified candidate + proofs bound) *before* obligation advancement runs; on conflict the node blocks, dependents don't launch, the graph attempt terminates blocked (synchronous runner semantics preserved), and resume's normal invalidation closure governs staleness.
- Write `docs/development/recovery-machinery-inventory.md` from the completed 24-mechanism characterization (RecoveryAgent seam unimplemented with dead `adjust_plan`; nearest-unique-owner transfer targeting; three mutually-blind budgets; duplicated no-change recovery; `force_records` as sole protocol-versioned operator channel; consolidation follow-ups).
- Gate: `pytest tests/test_feature_run.py tests/test_review_fix.py tests/test_plan_graph.py tests/test_plan_graph_observability.py`.

### RB-02 — Lineage ledger with atomic reservations (fail-closed)
*Paths: new `harness_labs/plan_graph_budget.py`, `harness_labs/plan_graph.py`, `scripts/run_plan_graph.py`, `schemas/`, new `tests/test_plan_graph_budget.py`.*
- `plan_lineage_id`: operator-supplied or generated at `register` (`--lineage-id`), persisted in the registration JSON. The ledger is keyed by lineage — re-registration inherits history; plan hashes + gate digests are versions within it (`plan_sha256` cannot key the ledger: editing `verification_argv` would change the hash and silently mint a fresh ledger).
- Append-only ledger `run_root/.plan-graph-budgets/<lineage_id>.jsonl` (design above); schema `schemas/retry-budget-ledger.json` + fixture; fold on open; flock per lineage; lock order graph-lock → budget-lock.
- Reservation state machine `reserved → started → completed | abandoned`, unique reservation ids; reserved **in `run()` immediately before the launcher call**; `resume()` computes a read-only verdict and refuses (fail-closed `PlanGraphError`, CLI exit 3) when the frontier is exhausted. Crash reconciliation reuses the existing liveness/custody rules (`reconcile_interrupted_attempts` dispositions).
- Attempt taxonomy: graph launches, gate invocations, repair dispatches, structural decisions — separate counters. Gate invocations/repairs imported only from structured child evidence (arrives RB-03; until then recorded as launch-level `indeterminate`).
- `BudgetConfig(node_gate_limit=5, finding_key_limit=3, infra_limit=3, config_policy_limit=1, structural_decision_limit=2)`; CLI budget flags. `failure_keys` from `finding_obligations` when present (wired by RB-01), else a stable digest of the failure reason. `tokens_total` imported from child summaries when present, null otherwise — never fabricated.
- **Fail-closed boundaries:** unchanged-plan successors proceed under ledger rules; changed-plan re-registration under an existing lineage → hard error ("unsupported until authority validation lands"); no decision refs accepted in this stage. Gate-change detection (argv-digest v1, labeled `gate_identity_v1_incomplete`) → block; ambiguous → carryover or block, never reset.
- **Operator relief valve (ships with the blocks it can cause):** `run_plan_graph.py budget extend|reset --lineage-id --node [--launches N] [--accept-gate-change --carryover full|reset] --reason` — writes an operator-attributed ledger event. Every terminal condition RB-02 can create (budget exhaustion, gate-change block) is relievable by this command from day one.
- Gate: `pytest tests/test_plan_graph_budget.py tests/test_plan_graph_observability.py` — cross-successor accumulation, cross-registration inheritance via lineage, reservation crash/concurrency cases, refusal fail-closed, changed-plan rejection, extend/reset relief round-trip.

### RB-03 — Failure classification + structured child counts
*Paths: `harness_labs/feature_run.py`, `harness_labs/plan_graph.py` (evidence import only), tests.*
- `classify_verification_failure` → `product | infrastructure_transient | harness_or_configuration | policy_violation | indeterminate`; matched rule id + evidence excerpt recorded. Timeout/selector/browser failures default `indeterminate` unless a specific transient rule matches (they can be product defects). Budget effects: product + indeterminate → product budget; infra → `infra_limit` (default 3); config/policy → cap 1 then escalate (they recur deterministically — retrying without a config change is pure churn).
- `infrastructure_transient` re-runs verification without repair dispatch (`env_retry_limit=2` keyword); no `repair_limit` consumption.
- Thread `DeterministicVerificationResult` counts (`command_attempts`/`repair_attempts`, feature_run.py:340) + per-invocation globally unique ids + classification through the terminal payload (feature_run.py:775-790) → `FeatureRunOutcome.evidence` → ledger import; duplicate-id imports are no-ops.
- Packet-class failures visible only at the PlanGraph layer (grant escape, no-repository-change rejection, pre-child launch failure) → `harness_or_configuration`/`policy_violation` from `outcome.evidence`.
- Gate: `pytest tests/test_feature_run.py tests/test_plan_graph_budget.py`.

### RB-04 — Centralized blocked transition + escalation artifact + notification hook
*Paths: `harness_labs/plan_graph.py`, `harness_labs/plan_graph_audit.py`, `scripts/run_plan_graph.py`, `schemas/`, tests.*
- `PlanGraph._transition_to_blocked(...)`: single path for all blocked finalizations (plan_graph.py:823-831, :846-861, budget/gate blocks) — checkpoint, escalation artifact (before finalize — the journal seals), events, budget state, finalize, in that order, tested once.
- Escalation `plan-graph-block-escalation/1` (`schemas/block-escalation.json` + fixture): identity + lineage; per-node reason/tier/classification/open obligations/candidate commit/evidence refs; budget state (all four counters + tokens_total read-through); `significance_guidance` (acceptance-criteria text for the recovery agent's tier classification); `resume_directive_template` with `blocker_evidence_ref` = this artifact (journal-recorded → satisfies `_has_recorded_artifact`, plan_graph_audit.py:269 — the escalation artifact IS the resume authority token); `decision_request` fields for decision-required blocks; paths incl. decision-log view. Stable copy `run_root/<graph_run_id>/escalation.json`; size limit 4 MB — oversized payloads externalize node detail to per-node artifact refs.
- Hook: `--on-block-argv '["prog","arg",...]'` (JSON array). **Fire-and-forget with full detachment**: own process group (`start_new_session=True`), cwd=run_root, stdin `/dev/null`, stdout/stderr → `run_root/<graph_run_id>/on-block-hook.log` (destination recorded in CLI output JSON) — never inheriting the caller's pipes. Environment inherited as-is (the operator launched the CLI with whatever model/runtime config the coordinator needs). Escalation path as final argv element; spawn attempt + pid journaled in output JSON; the CLI never waits; exit codes unchanged.
- Structured `status_flags` (`complete`, `success`, `resumable`, `deviated`) added to result payload/CLI output with compatibility tests for existing consumers (dashboard, exit codes, resume eligibility).
- Through RB-04 this program delivers **notification + recovery protocol**; Tier-1 recovery becomes operational only at RB-06.
- Gate: `pytest tests/test_plan_graph_observability.py tests/test_dashboard_api.py` + escalation round-trip test (template → `resume()` succeeds).

### RB-05 — Registration-embedded recovery allowance + typed action applicators + plan-version transitions
*Paths: new `harness_labs/plan_graph_authority.py`, `harness_labs/plan_graph_budget.py`, `harness_labs/plan_graph.py`, `scripts/run_plan_graph.py`, `schemas/`, new `tests/test_plan_graph_authority.py`.*
- Schemas + fixtures: `recovery-decision.json`, `plan-version-transition.json`; `automatic_recovery` block added to the registration schema. Fail-closed unknown versions.
- Authority = the registration's `automatic_recovery` block (see review-round decisions): allowed action list + bounded allowances, operator-approved at registration, digest-immutable. Decision validation checks the proposed action against `allowed_actions` and remaining allowance (ledger consumption events vs registration bounds). Changing autonomy = re-registering — an ordinary audited operator act.
- Typed decision actions with applicators: each carries mutable target, expected prior digest, payload, validation rules, resulting version, invalidation consequences, required re-verification. v1 applicators: `resume` (existing directive path), `extend_budget` (ledger event, bounded by `max_extra_node_launches`), `transfer_ownership` (obligation reassignment via existing transfer machinery + re-verification requirement on the receiving node), `ratify_gate_change` (gate_lineage event + explicit `budget_carryover`). Plan-revision actions (`revise_acceptance`, `revise_functionality`, `accept_contract_deviation`) execute only via the **plan-version transition record** consumed by `register --lineage-id <existing> --transition <file>`, and only when listed in `allowed_actions`; changed-plan registration becomes supported *here*, never before.
- Tier logic: Tier-1 = actions in `allowed_actions` within allowance → agent-resolvable; Tier-2 = everything else → terminates `requires_human`. Prospective human-tier delegation = the operator listing those actions at registration. Every human-tier action emits an individual decision record regardless.
- Terminal statuses `completed_with_deviations` / `completed_under_full_autonomy` / `externally_blocked` / `operator_intervention_required` (corrupt state), all expressed through `status_flags` + a structured deviation summary in the final payload — a deviated success is never reported as a clean `succeeded`.
- Gate: `pytest tests/test_plan_graph_authority.py tests/test_plan_graph_budget.py tests/test_plan_graph_observability.py` — allowance bounds enforced, agent cannot exceed or alter them (digest check), carryover semantics, transition-record validation, deviation statuses, per-deviation records.

### RB-06 — External recovery coordinator (Tier-1 becomes operational)
*Paths: new `scripts/plan_graph_recover.py`, tests.*
- Standalone coordinator, launched by the hook (or operator/cron): reads escalation → classifies vs `significance_guidance` → issues decision record within the registration's `automatic_recovery` allowance → **re-invokes the CLI as a fresh top-level process** for resume — never nested inside a waiting parent. Iteration cap + repeat-decision guard (mirrors feature_run.py:1135); meta-budget enforced by the ledger. LLM-backed via the executor-factory seam pattern (`VerificationRepairExecutorFactory`, feature_run.py:271); decision vocabulary extends the existing `RecoveryDecision` enum (`retry|adjust_plan|stop` + `transfer_ownership|ratify_gate_change|extend_budget|escalate_human`) so the dormant in-process seam (feature_run.py:1038) can later adopt it.
- Terminates `requires_human` / `externally_blocked` precisely; never fabricates success.
- Gate: end-to-end tempdir run — block → hook spawn → coordinator decision → fresh resume → exhaustion → allowance-bounded extension → deviation terminal status; once with a minimal allowance and once with human-tier actions delegated in the registration.

## Program verification
1. Full `python3 -m pytest tests/` green before each stage merges (245 today + new).
2. After RB-06: manual end-to-end per allowance variant; inspect ledger fold, decision-log view, deviation summary, gate_lineage after a `verification_argv` edit, transition record after a plan revision.
3. `/runtime-smoke` against each stage's diff.

## Postmortem traceability

| Postmortem finding | This program |
|---|---|
| "No native recovery event fired"; recovery = manual successor launches (both postmorts) | Hook + escalation with valid resume authority token; decision records make the chain journal-causal (RB-04/06) |
| Bounded child retries → unbounded graph retries (FR-10 pg11–pg34) | Lineage ledger enforced in `resume()` AND `run()`; re-registration inherits (RB-02) |
| Everything stalls on operator; supervisor polls | Tier-1 autonomous resolution under registration allowance; decision log for end-of-plan review (RB-05/06) |
| Env/packet failures conflated with product; recurred across successors | Five-way classification with per-class caps (RB-03) |
| Zero-token summaries concealed retry scale (both) | `tokens_total` read-through in ledger + escalation (full aggregation stays open) |
| Retinology rec #1 (successor imports candidate/ledger/classification/tokens without rerunning implement) | Candidate + obligation import exists (`repair_selection`); classification + tokens added to escalation. No-rerun-implement = delta-scoped rescope (next program) |
| Retinology recs #4, #5 (gate split; verify exact open keys) | Next program; ledger `failure_keys` + wired obligations (RB-01) are its carrier |
| Retinology recs #2, #3, #6, #8 | Follow-ups listed in the inventory doc (RB-01); #6 partially mitigated by packet-class classification |

## Out of scope
- Verifier byte-hash gate identity (`gate_identity_v1_incomplete` until then) + `allowed_paths ∩ gate_files` validation.
- Node-local vs program gate split / delta-scoped verification slice (next program).
- Node-rename plan migration; cross-plan-version checkpoint reuse; cryptographic authority; OS privilege separation.
- Unifying the three local budgets; merging duplicate no-change recoveries; wiring the in-process RecoveryAgent seam.
- Token aggregation beyond read-through import. Deleting `skills/codex/implement-v13-codex/`.
