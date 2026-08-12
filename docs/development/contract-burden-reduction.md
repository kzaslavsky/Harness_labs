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
- **Status:** open.

### 2. Dispatch-time criterion vocabulary strictness — liberal parser

- **Where:** controller task dispatch; rejection `unknown task criterion: AC-01: physics.js is requireable…`.
- **Evidence:** the plan-graph handoff artifact hands the coordinator criteria as `id: statement` pairs; dispatch accepts only bare ids. Burned a coordinator turn in **both** graph attempts (exp-1 05:26:21, exp-2). No ambiguity is defended — ids are unique.
- **Action:** accept `id` or `id: anything`; strip to the id and validate that. Postel's-law fix, no policy change.
- **Status:** open (prompt-pinned workaround in `BASE_INSTRUCTIONS`, commit `169ffb1` — remove the pin once fixed).

### 3. Capability narrowing as frozen-authority violation — allow subsets

- **Where:** superseding-dispatch validation; rejection `superseding task changes frozen authority: required_capabilities`.
- **Evidence:** exp-1 coordinator tried to salvage a blocked build with a read-only audit task (`repo.read` ⊂ `repo.read, repo.write`) and was refused; node then blocked holding a gate-passing candidate.
- **Rationale:** frozen authority defends against privilege *escalation*; monotone-decreasing capability is safe by construction and downstream grant enforcement is unaffected.
- **Action:** permit strict-subset `required_capabilities` in superseding/repair dispatches; keep bans on widening and on details-schema changes.
- **Status:** open.

### 4. Closed criterion-source enum — add plan provenance

- **Where:** `harness_labs/controller_kernel.py` `_criterion` — `source ∈ {operator, repository, coordinator}`, `ValueError` otherwise.
- **Evidence:** `"approved-plan"` crashed the kernel at exp-1 launch — even though an admitted PlanGraph is the most heavily attested provenance in the system. `tests/test_feature_run.py` itself uses `"approved-plan"` and passes only because it mocks `run_feature_worktree`; the canonical example teaches the crash.
- **Action:** add a `plan` source (or map binding criteria to it inside `run_plan_graph_feature_worktree` so callers cannot get it wrong); fix the test to exercise the real kernel path.
- **Status:** open (experiment works around it with `source="operator"`).

### 5. Clean-baseline requirement for repair dispatches — supersede via retry ledger

- **Where:** `ClaudeSemanticTaskExecutor` / `CodexSemanticTaskExecutor` writable-worker preflight: `writable worker requires a clean repository baseline`; `allow_dirty_baseline` is frozen constructor config the coordinator cannot grant at runtime.
- **Evidence:** the single most damaging blocker observed. Exp-1: worker produced a **gate-passing** `physics.js` with a placeholder report; the refused attempt left the tree dirty; every subsequent write dispatch was impossible; node blocked, graph blocked, candidate stranded.
- **Rationale for supersession:** the workspace-change receipt already attests exactly what the failed attempt changed; with delta-scoped retry (RB-01–06) the natural semantics are *retry-with-adoption* — within a node lineage, a repair's baseline is the prior attempt's receipted dirty state, classified as resumable work. Keep clean-baseline for first attempts.
- **Action:** ride on the RB ledger (semantics change, not a bare default-flip); converts the hand-patched `allow_dirty_baseline=True` + "adopt prior work" instruction (commit `169ffb1`) into mechanism.
- **Status:** open — highest value, needs RB integration design.

### 6. Unreferencable failed dispatches — mint provenance for rejections

- **Where:** provenance validation; rejection `unknown provenance reference: task:impl-orbit-physics-repair`.
- **Evidence:** exp-1 coordinator could not even *cite the repair failure* in its next command's reason, because the rejected dispatch never minted a referencable identity — though the rejection is already a journal event.
- **Action:** let rejected/failed dispatches be referencable provenance (they exist in the hash chain regardless).
- **Status:** open — minor.

## Explicit keep-list (earned their cost this run)

- **Approval receipt / digest binding at admission** — the actual trust boundary; revalidated at graph start and cost nothing.
- **Post-hoc write-grant / workspace-scope enforcement** — does real per-attempt work.
- **Controller-owned deterministic verification** — caught the exp-2 orbit-ui failures that drove real repair cycles (r3, r4) to a passing candidate.
- **Hash-chained audit journals** — the entire diagnosis in this document was reconstructed from them.

## Counterweight — the one place needing *more* contract

The originating defect of the blocked run was an **absence** of contract: the semantic result floor is `minLength: 1`, so `"summary": "test"` passed the typed contract and only the coordinator's judgment refused it. If items 1–5 are relaxed, add a minimal deliverable-content gate at the executor boundary (length / anti-placeholder heuristic) so the coordinator is not the sole defense against hollow worker output.

## Related smaller findings (tracked elsewhere)

- `--json-schema` structured output arrives as an undocumented internal `StructuredOutput` tool-use stream event — special-cased in `ClaudeAgentSession` (commit `ec53bd4`).
- Loopback MCP bridge `BrokenPipeError` traceback on teardown — benign; hardening task filed.
- Kernel-init crash before the git transaction owns state leaves stale worktree/branch/audit-dir requiring manual cleanup — candidate for launcher-level reconciliation, related to item 5's lineage semantics.

## Change log

- **2026-08-12** — document created from the orbit experiment diagnosis; all items open; RB-01–06 noted as merged baseline.
