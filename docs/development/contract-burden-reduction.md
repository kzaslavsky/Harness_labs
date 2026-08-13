# Contract-burden reduction — living diagnosis and worklist

**Status:** living document — update statuses in place, append new evidence-bound findings; do not rewrite history (strike through, don't delete).
**Provenance:** orbit PlanGraph experiment (graph attempts `orbit-graph-orbit-exp-1` blocked / `orbit-graph-orbit-exp-2` succeeded, 2026-08-12), audit journals under `logs/runs/orbit-graph-orbit-exp-{1,2}/`; first live `ClaudeAgentSession` coordinator seat; cross-read against `RETRY_BUDGET_RECOVERY_AUTHORITY_PLAN.md` (RB-01–06 merged at `Impl-redo` `de0f3dc`).
**Operator framing (2026-08-12):** the harness is overengineered on the contract/blocker side and was underengineered on recovery; with delta-scoped retry merged, reduce regulatory burden starting with gates that do **not** demonstrably increase robustness.

## Admission test for relaxing a gate

A gate qualifies for relaxation when either:

1. **Defeated by mechanical compliance** — a run satisfied it with a semantics-free transformation, so it cannot be load-bearing; or
2. **Superseded** — the failure mode it guards is now covered by a stronger mechanism (retry-budget ledger, workspace-change receipts, grant enforcement).

Every entry below cites the run evidence that qualified it. New entries need the same.

## Ranked worklist

### 1. Verbatim-substring plan gates — remove

- **Where:** `harness_labs/plan_graph.py` `validate_plan_graph_plan` — node objective string, criterion ids, *and* full criterion statements must appear character-for-character in cited plan sections.
- **Evidence:** rejected a valid engineered plan at registration; satisfied afterwards by `assemble_decomposition` deterministically appending the strings (`experiments/run_orbit_plan_graph.py`), with zero semantic change. Defeated by mechanical compliance; every author will converge on the same workaround.
- **Action:** delete substring matching; keep the referential checks that carry real integrity (criteria/sections exist, every criterion assigned, dependency order, command-dependency validation).
- **Status:** landed (CB-02, commit `835a711f986ca3b806619bbe3662f5515db0963c`). `validate_plan_graph_plan` in `harness_labs/plan_graph.py` no longer matches objective/criterion text against cited sections; it retains only the referential checks. The mechanical `additions` normalization this workaround forced in `experiments/run_burden_plan_graph.py`'s `assemble_decomposition` is retired by CB-08.

### 2. Dispatch-time criterion vocabulary strictness — liberal parser

- **Where:** controller task dispatch; rejection `unknown task criterion: AC-01: physics.js is requireable…`.
- **Evidence:** the plan-graph handoff artifact hands the coordinator criteria as `id: statement` pairs; dispatch accepts only bare ids. Burned a coordinator turn in **both** graph attempts (exp-1 05:26:21, exp-2). No ambiguity is defended — ids are unique.
- **Action:** accept `id` or `id: anything`; strip to the id and validate that. Postel's-law fix, no policy change.
- **Status:** landed (CB-01, commit `578ff4b5e13735598bbf16a0953dba98ce3b8efa`). `_resolve_task_criteria` in `harness_labs/controller_kernel.py` accepts both `"<id>"` and `"<id>: <text>"` dispatch entries. The prompt-pinned "bare criterion ids" workaround this forced in `experiments/run_burden_plan_graph.py`'s `BASE_INSTRUCTIONS` (originally commit `169ffb1`) is retired by CB-08. **Reconfirmed 2026-08-12** in the flow-editor-authoring program (independent operator, independent launcher): `unknown task criterion: AC-FR20-1: Full flow-editor unit suite…` burned coordinator turns in at least attempts pg85, pg90, pg98, pg99. Two programs, two coordinators, same mechanical rejection.

### 3. Capability narrowing as frozen-authority violation — allow subsets

- **Where:** superseding-dispatch validation; rejection `superseding task changes frozen authority: required_capabilities`.
- **Evidence:** exp-1 coordinator tried to salvage a blocked build with a read-only audit task (`repo.read` ⊂ `repo.read, repo.write`) and was refused; node then blocked holding a gate-passing candidate.
- **Rationale:** frozen authority defends against privilege *escalation*; monotone-decreasing capability is safe by construction and downstream grant enforcement is unaffected.
- **Action:** permit strict-subset `required_capabilities` in superseding/repair dispatches; keep bans on widening and on details-schema changes.
- **Status:** landed (CB-01, commit `578ff4b5e13735598bbf16a0953dba98ce3b8efa`). `harness_labs/controller_kernel.py` now accepts a superseding/repair task whose `required_capabilities` is a strict subset of the superseded task's; widening and disjoint sets still raise `superseding task changes frozen authority`. The corresponding "required_capabilities and details schema unchanged" prompt pin this forced in `experiments/run_burden_plan_graph.py`'s `BASE_INSTRUCTIONS` is retired by CB-08.

### 4. Closed criterion-source enum — add plan provenance

- **Where:** `harness_labs/controller_kernel.py` `_criterion` — `source ∈ {operator, repository, coordinator}`, `ValueError` otherwise.
- **Evidence:** `"approved-plan"` crashed the kernel at exp-1 launch — even though an admitted PlanGraph is the most heavily attested provenance in the system. `tests/test_feature_run.py` itself uses `"approved-plan"` and passes only because it mocks `run_feature_worktree`; the canonical example teaches the crash.
- **Action:** add a `plan` source (or map binding criteria to it inside `run_plan_graph_feature_worktree` so callers cannot get it wrong); fix the test to exercise the real kernel path.
- **Status:** landed (CB-01, commit `578ff4b5e13735598bbf16a0953dba98ce3b8efa`). `_criterion` in `harness_labs/controller_kernel.py` accepts source `"plan"` alongside `operator`, `repository`, and `coordinator`, and the coordinator's `criterion_propose` tool schema in `harness_labs/controller_coordinator.py` offers it; `tests/test_feature_run.py` exercises the real kernel criterion path with a kernel-valid source. `experiments/run_burden_plan_graph.py`'s `_launch_node` now binds plan-graph criteria with `"source": "plan"` instead of the `"operator"` workaround (CB-08).

### 5. Clean-baseline requirement for repair dispatches — supersede via retry ledger

- **Where:** `ClaudeSemanticTaskExecutor` / `CodexSemanticTaskExecutor` writable-worker preflight: `writable worker requires a clean repository baseline`; `allow_dirty_baseline` is frozen constructor config the coordinator cannot grant at runtime.
- **Evidence:** the single most damaging blocker observed. Exp-1: worker produced a **gate-passing** `physics.js` with a placeholder report; the refused attempt left the tree dirty; every subsequent write dispatch was impossible; node blocked, graph blocked, candidate stranded.
- **Rationale for supersession:** the workspace-change receipt already attests exactly what the failed attempt changed; with delta-scoped retry (RB-01–06) the natural semantics are *retry-with-adoption* — within a node lineage, a repair's baseline is the prior attempt's receipted dirty state, classified as resumable work. Keep clean-baseline for first attempts.
- **Action:** ride on the RB ledger (semantics change, not a bare default-flip); converts the hand-patched `allow_dirty_baseline=True` + "adopt prior work" instruction (commit `169ffb1`) into mechanism.
- **Status:** landed (CB-05, commit `51fbf9303222a84815bf4bc18d609c23a4b386c3`). `harness_labs/feature_run.py` now attaches a per-dispatch `dirty_baseline_grant` to repair/review-fix executors only when an existing `workspace-change-receipt` in the run's evidence catalog truthfully covers every currently-dirty path, auditing the grant as `dirty_baseline_adoption_grant_supplied`; `ClaudeSemanticTaskExecutor`'s constructor-frozen `allow_dirty_baseline` is deprecated in favor of this receipted, per-attempt grant. Highest-value item; **massively reconfirmed 2026-08-12** by the flow-editor-authoring audit (see §Flow-editor audit below): the operator's launcher hand-implements candidate lineage by pinning `FR20_RECOVERY_WORKTREE` to a prior attempt's worktree and prompting "FIRST ACTION: byte-copy and verify every changed path from the retained pg97 FR-20 worktree… preserve every retained file byte-for-byte." Per-node hand-authored adoption prompts exist for FR-02A, FR-02B, FR-15, and FR-20. This is the RB retry-with-adoption mechanism, implemented as prose, per node, by hand, across 101 graph attempts.

### 6. Unreferencable failed dispatches — mint provenance for rejections

- **Where:** provenance validation; rejection `unknown provenance reference: task:impl-orbit-physics-repair`.
- **Evidence:** exp-1 coordinator could not even *cite the repair failure* in its next command's reason, because the rejected dispatch never minted a referencable identity — though the rejection is already a journal event.
- **Action:** let rejected/failed dispatches be referencable provenance (they exist in the hash chain regardless).
- **Status:** landed (CB-01, commit `578ff4b5e13735598bbf16a0953dba98ce3b8efa`). Provenance validation in `harness_labs/controller_kernel.py` accepts a reference that resolves to an existing audit-journal event, so a rejected `task.dispatch` — already a journal event — is citable by later commands without a new evidence namespace.

## Flow-editor authoring PlanGraph audit (2026-08-12)

**Subject:** `FLOW_EDITOR_AUTHORING_AND_NODE_EXECUTION_UX_PLANGRAPH.json` (15 nodes FR-00 → FR-30, Retinology repo), executing in `/Users/kirillzaslavsky/Documents/retinology-flow-authoring-plangraph-audit/`. At audit time the graph is on **FR-20** (integration certification), attempt **pg101**, live. Attempts pg2 → pg101: **101 whole-graph relaunches**, 232 run-log directories. FR-20 alone consumed at least 16 attempts (pg85–pg101); every terminal FR-20 attempt ended `blocked`.

**How each FR-20 attempt dies** (from node journals `logs/runs/*-FR-20/{events.jsonl,manifest.json,artifacts}`):

| Class | Attempts | Detail |
|---|---|---|
| Real defect, repair budget exhausted | pg89, pg90, pg96, pg97, pg99 | 1–4 failing tests out of ~733; one repair permitted; repair fixes test A and test B fails → `declared verification command still fails after repair budget` → node blocked |
| Environmental / flaky gate | pg85 (timeout 7200s), pg87, pg94, pg98 (SIGTERM), pg91, pg92, pg93 (`FAIL walk driver`, all pytest green) | live-browser walk drivers inside the "deterministic" verification command; failure charged against the same single repair budget |
| Contract dead-end | pg88 | coordinator finished implementation, then blocked itself: criteria require verification/review gates, but "this build segment is explicitly prohibited from dispatching verification-only tasks or rerunning that command" |
| Infra before session | pg95, pg100 | `coordinator segment build failed before session start`; crash with no manifest |

In pg99 the run view shows **all four acceptance criteria `satisfied`** and `run_status: succeeded` — then the node blocks on the verification stage and the 733/734-passing candidate is stranded with no lineage. The only recovery the harness offers is relaunching the whole graph; checkpoint replay makes FR-00–FR-15 free, but FR-20 restarts from FR-15's candidate unless the operator hand-carries the prior tree (see item 5 evidence).

### New worklist items from this audit

### 7. Scalar verification-repair budget — delta-scope it (companion to item 5)

- **Where:** `verification_repair_limit=2 if run_id == "FR-10" else 1` in the operator's launcher; harness treats the limit as a frozen scalar per node.
- **Evidence:** five attempts blocked with a single-digit failing-test count out of ~733. The budget has no relationship to failure size or to whether repairs are converging. The repair seesaw (pg99: repair fixes `test_fr20_node_run_uses_effective_lock_and_selected_identity`, then `test_node_run_uses_served_node_lock_and_selected_revision_identity` fails) is exactly one repair short of convergence, repeatedly.
- **Action:** make the repair budget delta-scoped on the RB ledger: a repair that strictly shrinks the failing set renews the budget; only non-monotone or stagnant repairs consume it. Blocked-by-verification nodes must be resumable in place (same candidate, same lineage) instead of forcing a graph relaunch.
- **Status:** landed (CB-04, commit `d8e5e8e89c9c8eceb27e771c359330363ec0a30a`). `harness_labs/plan_graph_budget.py` derives a stable per-test `failing_identifiers` set from pytest's `FAILED`/`ERROR` summary lines and the `RetryBudgetLedger` renews the repair budget when a repair strictly shrinks that set, charging the scalar limit only for non-monotone or stagnant repairs. This plus item 5 would have collapsed pg85–pg101 into a handful of attempts.

### 8. Environmental failures charged as repair failures — classify before charging

- **Where:** deterministic-verification stage; exit 124 (timeout), 143/-15 (SIGTERM), and browser walk-driver crashes all consume the repair budget and block the node.
- **Evidence:** seven of fourteen terminal FR-20 attempts died on timeout/SIGTERM/walk-driver failures with pytest fully green. A repair executor was dispatched to "fix" an environmental failure it cannot fix.
- **Action:** classify verification failures before charging: nonzero-exit-with-failing-tests → repairable; timeout/signal/driver-crash → retryable environment fault, re-run the gate without consuming repair budget (bounded retry count). Additionally allow a decomposed verification contract — the FR-20 command serializes full pytest + two runtime smokes + four live-browser walks + UI-graph gate + PHI scan into one 7,200-second `bash -lc` string, so any single flake voids the whole certification; per-gate argv with per-gate retry/repair semantics removes that coupling without weakening any gate.
- **Status:** landed in part (CB-03, commit `f30c67d2952fe0a7ff5c0e9b950f03ffa432b00a`), narrowed by review to the classification half. `classify_verification_failure` in `harness_labs/feature_run.py` now reads structured fields — `timed_out`, exit code 124, negative signal returncodes, and browser/driver-crash markers in otherwise pytest-green output — and classifies them `infrastructure_transient` with distinct rule ids, so they ride the RB ledger's existing free-retry class instead of burning repair budget as `indeterminate`. The decomposed-verification-contract half of this item (per-gate argv splitting a single serialized command) is not addressed by any node and remains open.

### 9. Cross-node finding-obligation transfer exists only as prose

- **Where:** operator launcher: "left is browser evidence, uniquely owned downstream by FR-20", "PlanGraph transfer it to the nearest unique downstream owner (FR-20)", plus a one-path grant "This one-path grant repairs the frozen ownership defect".
- **Evidence:** when a finding's only fix lives outside the discovering node's `allowed_paths`, the harness has no transfer mechanism; the operator routes obligations between nodes by editing prompt text, and patches path-ownership defects with hand-written single-path grants.
- **Action:** first-class obligation transfer: a blocked finding names a target node; the graph attaches it to that node's inherited findings with provenance. Related to item 3 (narrowing) — both are "the coordinator can see the right move but the contract has no verb for it."
- **Status:** open.

### 10. Build segment cannot dispatch verification-only work — dead-end when only verification remains

- **Where:** phase/segment dispatch policy; pg88 block reason quoted above.
- **Evidence:** implementation and the summary artifact were complete, no findings pending; the remaining criteria were pure gate criteria. The coordinator's only legal move was `run.block_request` — a mandatory dead-end followed by a full graph relaunch.
- **Action:** either let the kernel auto-advance to its own verification stage when the coordinator declares build-complete (it already owns the gate), or permit verification-scoped dispatches in the build segment. Nothing is defended by forcing the block.
- **Status:** landed in part (CB-06, commit `a944cf4bd314a377f5f7f8078d69ff7a16b5519e`), scoped by review to the plan-graph bound path. A run-contract criterion may declare deterministic-verification adjudication in `harness_labs/controller_kernel.py`; such a criterion is marked satisfied from the verification owner's passing command evidence rather than a coordinator claim, a coordinator claim that tries to satisfy it directly is rejected, and the plan-graph bound completion path accepts a build-complete request with gate-backed criteria still pending, reaching a successful terminal status only once the controller-owned command passes. This removes the pg88 dead-end for plan-graph bound runs specifically; the general build-segment dispatch-policy question outside that path is not addressed by any node and remains open.

**Also observed, tracked under existing items:** item 2 recurrences (four+ attempts burned turns on `unknown task criterion: AC-FR20-1: <full text>`); item 5 at scale (hand-maintained 1,100-line launcher whose main content is per-node adoption/recovery prose — pinned recovery worktrees, "copy its exact eleven changed paths", per-attempt defect pinning).

## Explicit keep-list (earned their cost this run)

- **Approval receipt / digest binding at admission** — the actual trust boundary; revalidated at graph start and cost nothing.
- **Post-hoc write-grant / workspace-scope enforcement** — does real per-attempt work.
- **Controller-owned deterministic verification** — caught the exp-2 orbit-ui failures that drove real repair cycles (r3, r4) to a passing candidate.
- **Hash-chained audit journals** — the entire diagnosis in this document was reconstructed from them.

## Counterweight — the one place needing *more* contract

The originating defect of the blocked run was an **absence** of contract: the semantic result floor is `minLength: 1`, so `"summary": "test"` passed the typed contract and only the coordinator's judgment refused it. If items 1–5 are relaxed, add a minimal deliverable-content gate at the executor boundary (length / anti-placeholder heuristic) so the coordinator is not the sole defense against hollow worker output.

**Status:** landed (CB-07, commit `3ce85867548848a28ec9b0413ea7f4a8260a1913`). A closed, deterministic placeholder-content rule set (sub-minimal length, known placeholder tokens such as "test"/"todo"/"n/a", single repeated token) is enforced at the shared semantic result boundary used by both the Claude and Codex executors, refusing placeholder worker output with an audited classified refusal while passing substantive results unchanged.

## Related smaller findings (tracked elsewhere)

- `--json-schema` structured output arrives as an undocumented internal `StructuredOutput` tool-use stream event — special-cased in `ClaudeAgentSession` (commit `ec53bd4`).
- Loopback MCP bridge `BrokenPipeError` traceback on teardown — benign; hardening task filed.
- Kernel-init crash before the git transaction owns state leaves stale worktree/branch/audit-dir requiring manual cleanup — candidate for launcher-level reconciliation, related to item 5's lineage semantics.

## Change log

- **2026-08-12** — document created from the orbit experiment diagnosis; all items open; RB-01–06 noted as merged baseline.
- **2026-08-12 (later)** — flow-editor-authoring PlanGraph audit (Retinology, FR-20, attempts pg2–pg101): items 2 and 5 independently reconfirmed at scale; items 7–10 added (delta-scoped repair budget, environmental-failure classification, obligation transfer, build-segment verification dead-end). Item 5 + 7 jointly identified as the dominant cost driver — 101 whole-graph relaunches with hand-carried candidate lineage.
- **2026-08-13** — contract-burden-relaxation PlanGraph program (CB-01–08) closes items 1–8 and the counterweight, each recorded above with its landing node and commit; item 8 lands only its classification half (the decomposed-verification-contract half is unaddressed). CB-08 also retires the launcher's own compliance workarounds in `experiments/run_burden_plan_graph.py` (`BASE_INSTRUCTIONS`' bare-criterion-id and frozen-capability prompt pins, the `"source": "operator"` criterion binding, and `assemble_decomposition`'s mechanical objective/criterion-statement normalization) now that CB-01 and CB-02 make them unnecessary. Items 9 and 10's general (non-plan-graph-bound) half remain open — no node in this program addresses them.
