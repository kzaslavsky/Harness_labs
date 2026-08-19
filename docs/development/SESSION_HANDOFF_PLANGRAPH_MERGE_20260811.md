# Session Handoff — PlanGraph branch merges + delta-scoped retry rescope

**Date:** 2026-08-11 · **Repo:** `<user-home>/Documents/harness_labs` · **Branch:** `Impl-redo` (ahead of `origin/featureRun` by 32, nothing pushed)

## Orientation: what is what in this repo

- **The live harness is the `harness_labs/` Python package** — `plan_graph.py`, `plan_graph_audit.py`, `feature_run.py`, `controller_*.py`, `review_fix.py`. Tests in repo-root `tests/`.
- **`skills/codex/implement-v13-codex/` is a DEPRECATED, non-functional template.** Do not build features there. It was never excised from any branch; it still sits in the tree and is slated for deletion. It contains an abandoned delta-scoped-retry draft (see below) that exists only as reference material.
- Two postmortems motivated all of this work:
  - `docs/development/plangraph-parallelization-run-defect-and-retry-postmortem.md` (this repo): 67% of tokens spent on retries; no native recovery fired; final candidate never merged to base.
  - `<user-home>/claudeprojects/Retinology/.claude/checkouts/flow-node-mockup-parity/docs/development/FLOW_EDITOR_AUTHORING_AND_NODE_EXECUTION_UX_RUN_DEFECT_AND_RETRY_POSTMORTEM.md` (Retinology): 88% retry share; native recovery fired 5× and succeeded 0×; FR-10 burned 21 retries against a monolithic legacy gate.
  - Shared diagnosis: the harness is over-engineered at the gate layer and incomplete at the recovery layer. Recovery that re-runs the whole node against the whole gate is waste automation; retries must be **delta-scoped** (close exactly the open finding keys, re-verify only the node-local slice).

## What happened this session (chronological)

1. **Wrong-target build (reverted).** Delta-scoped retry was fully built and committed into the deprecated skill (`09f818f`), then discovered to target the dead layer and undone via `git reset --soft`. The commit remains reachable in reflog. The draft's design (frozen delta-scope document, byte-hash-bound ledger, candidate-commit import by proof, authority-separated resume) is sound and should inform the rescope; its code lives uncommitted-then-snapshotted inside the dead skill dir.
2. **`e41618f` — WIP snapshot.** The dirty tree (user's in-flight dashboard/audit/run-catalog edits, uncommitted `serial-implement*` skill deletions, the abandoned draft) was committed as a snapshot so merges could run. Four embedded git repos under `experiments/` were deliberately excluded (left untracked).
3. **`d3ec8ee` — merged `codex/plangraph-parallelization-20260810-successor-13-pg-07-candidate`** (17 commits; the first postmortem's final candidate chain PG-00→PG-07).
4. **`9920e64` — merged `codex/plan-graph-bound-feature-run`** (11 commits; Retinology-era harness fixes: finding-obligation transfer, no-change-repair recovery, child-lane custody).
5. Both merged branches still contain the deprecated skills — excision has never been committed anywhere.

## Merge reconciliation decisions (the load-bearing ones)

The merges reconciled the branch work with `0177fc6` ("deterministic PlanGraph registration"), which had rewritten the same files. Decisions a future session must not accidentally undo:

- **Registration is mandatory.** `PlanGraph.__init__(repository, registration, launcher, *, run_root, ...)` — plan derives from `verify_registration()`. `PlanGraph.resume(repository, registration, launcher, *, run_root, directive)` likewise; it no longer accepts a bare plan.
- **Repair digests use the registration-verified plan hash.** `PlanGraphAudit.repair_contract_digest(..., plan_sha256=...)` and `open_repair_predecessor(..., plan_sha256=...)` take the hash; nothing re-reads the plan file from the working tree (the old `_plan_digest(plan)` file-read path is gone from resume verification).
- **Audit checkpoint carries dual identity:** `registration_binding` (exact-key fail-closed validation; `graph_attempt_id` must equal `graph_run_id`) AND the `logical_graph` / `graph_attempt` lineage blocks. `plan_graph_digest` is the *computed* contract digest (not the registration digest) because resume paths recompute it for predecessor matching.
- **`allowed_paths` is part of the immutable node definition** (`_NODE_DEFINITION_FIELDS` in plan_graph.py, `_IMMUTABLE_NODE_FIELDS` in plan_graph_audit.py, and `canonical_definition`). Without this, registrations drop it and finding-transfer targets silently vanish. Note: this changes registration digests vs. any pre-merge registrations.
- **`FeatureRunRequest` / `PlanGraphFeatureRunBinding`** keep required registration identity (`plan`, `plan_base_commit`, `plan_sha256`, with `approved_plan.path/sha256` consistency checks) plus the branch's optional finding-obligation and child-lane allocation fields.
- **`run()`** keeps the recovery short-circuits (live child → return "running"; blocked recovery → finalize blocked) and threads `finding_obligations` through `_request_for_run` and `node_completed`.
- **Legacy state import stays dead:** `scripts/import_plan_graph_state.py` returns 2 with an incompatibility message.
- **`scripts/run_plan_graph.py`** has `register` and `run` subcommands; `run` carries `--resume --logical-graph-id --predecessor-attempt-id --retry-frontier --blocker-evidence-ref`.
- **Dashboard `dist/` was rebuilt** from the merged `App.jsx` (`vite build`); stale hashed assets were removed.
- Dropped as obsolete: two old-API duplicate tests from the B1 side of `tests/test_plan_graph.py` (mapping/queue-order variants — equivalent coverage exists in the registration-style tests). Everything else was adapted, not dropped.

## Test state (all green)

- Repo: `python3 -m pytest tests/` → **245 passed, 0 errors**.
- Skill (deprecated but intact): `cd skills/codex/implement-v13-codex && python3 -m pytest tests/` → **226 passed**.
- **`tests/__init__.py` is new and load-bearing.** Five modules (`test_controller_*`, `test_coordinator_dispatcher`, `test_feature_run`) import via `from tests.controller_scenario_fixtures import ...` and were silently uncollectable for the package's entire history (the fixtures file always existed; the package init didn't). 41 previously-dead tests now run. If a future session sees `ModuleNotFoundError: tests.controller_scenario_fixtures`, that file was deleted — restore it.
- Child-lane tests in `test_feature_run.py` mock `harness_labs.feature_run.subprocess.run` (returning `stdout=b"plan\n"`) because `run_plan_graph_feature_worktree` now verifies the registered plan via `git show` in `base_repository`.

## Next work, in intended order

1. **Rescope delta-scoped retry into the live harness.** The merged `resume()` selects *which nodes* to retry (`retry_frontier`) but a retried node still re-runs its full implement + full verification. The design to port from the abandoned draft: on terminal node, freeze the residual delta (open finding keys from the node's review evidence — B2's `finding_obligations` state is the natural carrier), bind the last verified candidate commit by hash, and have the retried node close exactly those keys with a node-local verification slice instead of the full gate. The Retinology rec list (§"Recommended harness corrections" in its postmortem) is the acceptance checklist; rec #4 (split node-local gates from program gates) is a prerequisite the slice design enforces structurally.
2. **Excise `skills/codex/implement-v13-codex/`** (and confirm `serial-implement*` deletions get committed). This also deletes the abandoned draft — extract anything wanted from reflog commit `09f818f` first (`git show 09f818f`). The draft's most portable pieces: the delta-scope document schema (protocol `implement-v13-codex/delta-resume-scope/1`), the freeze/validate pair with byte-hash ledger binding, and the HEAD==candidate import-by-proof check.
3. **Autonomous trigger policy** (deferred by user, discussed): blocker-class routing, retry budget, non-replay pressure — belongs in the orchestrator layer that calls `run_plan_graph.py`, not inside PlanGraph.

## Cautions

- The tree still holds untracked `experiments/*` embedded git repos and assorted untracked audit/log material — do not `git add -A` from repo root without excluding them.
- `origin/featureRun` is the tracked remote branch; the local branch is named `Impl-redo`. 32 unpushed commits — pushing is the user's call.
- The user's standing instruction from the postmortem lesson: **whatever gets built, merge it to the canonical base (`Impl-redo`) — do not strand work on side branches.** And verify which layer is live before building (this session's original sin).
