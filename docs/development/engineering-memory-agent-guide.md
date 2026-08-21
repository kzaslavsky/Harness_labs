# Engineering memory: agent how-to

Audience: AI agents working in harness_labs. Full design:
`engineering-memory-port-plan.md`. Landed 2026-08-20 (merge `d699193`).

## Gate evidence now has two channels

- `gates["warnings"]` — high-severity only, blocks issue until each
  `warning_identity` is acknowledged in `operator-approval.json`.
- `gates["notices"]` — informational; no acknowledgement gate scans it.
  Read it; never ask an operator to ack it.

All entries derive from git blobs at `base_commit` only. Never add an
admission signal that reads the working tree or a gitignored path —
`issue_receipt` re-derives and refuses on drift.

## Impact analysis (`harness_labs/plangraph/impact_analysis.py`)

Runs automatically in every `prepare_approval`. A run whose `allowed_paths`
omit a file in a declared `.py` file's static import neighborhood gets a
`required-paths-impact-gap` warning (kind constant
`REQUIRED_PATHS_IMPACT_WARNING`), one per run, missing paths in `paths`.

- Authoring a decomposition: before declaring `allowed_paths`, expect this
  check; include importers of the files you edit or plan to acknowledge.
- Non-Python / unparseable targets appear in `notices` with a reason
  (`supported=False`). Unsupported ≠ clean; do not claim coverage from it.
- Library use (e.g. root-causing `required_paths` at intake):
  `assess_required_paths(target_path, required_paths, repo_paths, source)`
  — never raises; inject a `SourceReader` (filesystem or git-blob).

## Finding history (`harness_labs/plangraph/finding_history.py`)

Cross-campaign recall of ruled finding keys. Read-only by construction.

```python
history = fold_campaigns([(journal_path, campaign_label, repository_id), ...])
history.for_key(file, subject)    # exact-key lineage, newest first
history.for_paths([prefix, ...])  # exact + directory-prefix containment
```

- Entries carry terminal status, each ruling's disposition + operator
  `statement` (the field is `statement`, not `reason`), campaign label,
  `base_commit`.
- Campaign driver: `ingest --history-roots <root>...` seals a
  recurrence-annotation artifact for any arriving key a prior campaign
  ruled; digest lands in checkpoint state. Journal bytes never change.
- Retrieval is exact key / path containment only. Do not add similarity
  retrieval; do not parse `state-ledger` records yourself — use
  `ConvergenceLedger.key_lineage()` and the `RECORD_KIND_*` constants.
- Vocabulary: disposition `waive` folds to terminal status `excluded`.

## Decision registry (`harness_labs/core/decision_registry.py`)

```python
registry = load_decisions("docs/decisions")
result = registry.active_decisions_for_paths(("harness_labs/plangraph/plan_graph.py",))
```

Active = status `accepted` and not named in any other decision's
`supersedes`; matched by `Concerns-paths:` prefix. Contradictions surface
as `Inconsistency` records — report them, never resolve silently.

- Prepare emits `active-decision-notice` in `notices` listing decisions
  governing a plan's paths: read those ADRs before editing their paths.
- Writing an ADR: fill `Concerns-paths:`; use `Supersedes:` only for a
  real supersession (the two 0006 ADRs are distinct decisions, not a
  supersession). Schema 1.1 records may carry `supersedes`,
  `concerns_paths`, `valid_from_commit`; 1.0 records may not.

## Hard rules (violations fail review)

1. No new `state-ledger` record kinds; no ledger writer changes.
2. No new `prepare_approval` / `issue_receipt` parameters; no new plan
   payload top-level keys (`canonical_plan_graph_payload` is closed).
3. `warnings` = high only; informational → `notices`.
4. Pre-existing `warning_identity` values must not change (hashes kind,
   severity, runs, paths).
5. `finding_history` never writes — a missing journal path must raise
   before any `ConvergenceLedger` is constructed (its open creates files).
6. No third-party deps (no `jsonschema`); hand-written checkers only.
