# Engineering-memory port: impact analysis, finding history, decision lifecycle

Status: revised after multi-lens review (decomposition, source-binding, design/contract), 2026-08-20
Date: 2026-08-20
Base: `main` (59c7fd9 lineage; this worktree)

Port of three ideas from the graph-engineering-for-coding-agents survey,
adapted to harness_labs' existing evidence/ledger discipline. Explicitly
rejected from the survey: embedding-similarity retrieval of "lessons"
(the CTIM-Rover failure mode; also an inference from
`docs/architecture/context-engineering.md`'s "sufficient, minimal,
traceable" packet rule — the document does not name similarity retrieval,
it forbids what similarity retrieval produces), monolithic graph
databases, and graph query languages as agent environments.

Operational note: `prepare_approval` reads the plan and every referenced
artifact from git at `base_commit`, so this plan and its decomposition
must be committed before admission can run. This plan is deliberately
conformance-unaware — no criterion carries an `OBSERVABLE:` annotation —
so it must be approved with `enforce` left unset; passing `enforce=True`
blocks on every criterion by design (arming is all-or-nothing).

## Problem [em-problem]

Three gaps, each with a documented cost:

1. **`required_paths` is asserted, not verified.**
   `docs/development/SESSION_HANDOFF_DELTA_TO_RUN_20260820.md` names
   `required_paths` the load-bearing field of the finding contract: node
   ownership derives from it alone (full-plan routing is
   `PlanGraph._owner_for_paths` in `plan_graph.py`, resting on
   `harness_labs/core/git_transaction.owner_for_path`; a separate
   `_target_for_path` helper exists in both `plan_graph.py` and
   `review_fix.py` for transfer-target validation), and wrong attribution
   recreates scope-fence review churn — the dominant cost of the CC
   campaign (ADR 0007). Today ingest checks only `file ∈ required_paths`
   (`convergence_ledger.py:769-780`). Nothing checks the set against the
   actual import/definition topology of the repository.

2. **Finding history dies with its campaign.** The convergence ledger is
   campaign-scoped. A finding key `(file, subject)` whose prior campaign
   ruled it (a `finding_ruled` record with disposition `waive`, folding
   to terminal status `excluded`), invalidated it, or confirmed it good
   arrives at a later campaign's ingest as a fresh object. Pitfall 2 of
   the handoff ("reviewers must see operator rulings, or every fresh
   reviewer re-litigates them") currently holds *within* a campaign via
   operator-notes; nothing holds it *across* campaigns.

3. **Decision records carry status but not supersession or scope.**
   `schemas/decision.schema.json` already has
   `status ∈ {proposed, accepted, rejected, superseded}` — but no link
   from a superseding decision to the superseded one, no path scope, no
   commit binding, and today zero code consumers. In `docs/decisions/`
   no ADR supersedes another yet (the two files numbered 0006 are
   distinct active decisions that collide in number only; ADR 0007's
   2026-08-20 amendment is body prose within one record). The failure
   mode is prospective but structural: the moment a supersession happens,
   a superseded decision retrieves identically to an active one, and
   "give a node only the active decisions for its paths" is not
   mechanically answerable.

## Design constraints [em-constraints]

- **No new ledger record kinds, no new plan payload fields.**
  `canonical_plan_graph_payload` has a closed top-level key set (handoff
  pitfall 5); the `state-ledger` record vocabulary is likewise closed.
  A public *read* accessor added to `convergence_ledger.py` is not a
  record kind and does not violate this constraint.
- **Deterministic at issue.** `issue_receipt` re-derives gate evidence
  from git at `base_commit` and refuses on drift (the TOCTOU guard), and
  `warnings` is part of the re-derived, pinned set. Therefore every new
  admission-time signal derives ONLY from git blobs at `base_commit` of
  the repository `prepare_approval` already receives — never from the
  working tree, never from gitignored journals. Signals that need
  mutable inputs (finding history) live at campaign ingest, not at
  admission.
- **Advisory, never gating — and deadlock-aware.** The campaign driver's
  approval packet refuses on *any* unacknowledged warning of any
  severity, while `_require_acknowledged_high_warnings` accepts
  acknowledgements only for high-severity warnings. So the `warnings`
  array receives ONLY actionable high-severity entries; everything
  informational goes to a new optional `gates["notices"]` array that no
  acknowledgement gate scans. Existing warning kinds and their
  `warning_identity` values (which hash kind, severity, runs, and paths)
  are untouched.
- **Exact retrieval only.** Finding history is queried by exact
  `(file, subject)` key and by path containment (exact plus
  directory-prefix, the containment predicate of
  `git_transaction.owner_for_path` — not the single-owner collapse of
  `PlanGraph._owner_for_paths`). Decision retrieval is by declared path
  scope. No similarity search.
- **Single source of truth, and truly read-only.** Finding history is a
  fold over existing campaign ledger journals through the ledger's own
  replay — no second writable store, no bespoke parser. Because the
  ledger's internal `_locked` creates missing journals on open, the fold
  must refuse a non-existent path before constructing a ledger object.
- **Import boundaries.** `impact_analysis` and `finding_history` live in
  the plangraph layer; `decision_registry` lives in core (imports core
  only). `tests/test_import_boundaries.py` must stay green.
- **Production-consumer trace** (required by
  `docs/architecture/context-engineering.md`): every module built here
  names its production consumer in this plan; consumers are wired in
  EM-D1/EM-D2 of the same graph, not deferred.

## Design

### EM-1 Impact analysis for required_paths [em-impact]

New module `harness_labs/plangraph/impact_analysis.py`.

Static Python analysis reading file bytes through an injected source
callable — filesystem-backed in tests and future intake use,
git-blob-backed (`base_commit` tree) at admission, which is what makes
the admission wiring deterministic at issue. The AST technique
generalizes `scripts/dev/check_import_boundaries.py` (all-nodes walk, so
deferred in-function imports are seen); the code is repository-agnostic
and does not import that script.

- `module_neighborhood(source, file)` → files importing the module at
  `file`, files it imports, and files defining names it references at
  module level.
- `assess_required_paths(source, file, required_paths)` →
  `ImpactAssessment` with `supported`, `reason`, `confirmed` (declared
  and in the neighborhood), and `missing` (in the neighborhood, not
  declared; each with edge kind `imported_by`, `imports`, or
  `defines_referenced_name`). No "unrelated declared paths" set is
  computed — over-broad grants are the sibling-overlap warning's job,
  and impact analysis must not become a narrowing authority.
- **Language honesty.** Non-`.py` files and unparseable `.py` files
  yield `supported=False` with a reason. Callers surface the reason,
  never treat unsupported as clean. The interface is the port; the
  Python analyzer is the first backend.

Production consumers: admission warnings and notices (EM-D1), refinement
advisories (EM-D2). The future FI intake lane
(`finding_intake.py` per the handoff — a distinct module from
`finding_history.py`: intake creates findings, history recalls them)
consumes `module_neighborhood` when built.

### EM-2 Repo-scoped finding history [em-history]

Two pieces, both owned by EM-B.

First, `convergence_ledger.py` gains a public read-only per-key lineage
accessor and named record-kind constants. Today the only public read
surfaces (`records()`, `open_set()`, `key_status()`, `exclusion_set()`)
do not expose per-key history-with-dispositions; that lives only in the
private fold. The accessor exposes it without new record kinds or writer
changes. This is a contract addition to a module every campaign depends
on; the implementing run records it as a decision record.

Second, `harness_labs/plangraph/finding_history.py`:

- `fold_campaigns(entries)` where each entry is caller-declared
  `(journal_path, campaign_label, repository_id)` → `FindingHistory`.
  Campaigns do not record a `repository_id` or campaign id in their
  journals (the checkpoint, not the journal, carries `campaign_id`), so
  both are declared by the caller — the campaign driver knows them. The
  guard refuses entries whose declared `repository_id` values differ.
  Journal records carry no timestamps; the journal ordinal is the
  when-learned order, and the `campaign_opened` record's `base_commit`
  is the when-true-in-repo anchor. That pair is the entire bitemporal
  commitment of this plan.
- `FindingHistory.for_key(file, subject)` → prior lifecycle of the exact
  key across campaigns, newest entry first, carrying derived terminal
  status and each `finding_ruled` disposition with its recorded
  `statement` (the ledger's field name; `reason` is a different field on
  `finding_reopened`/`confirmed_good` records).
- `FindingHistory.for_paths(paths)` → prior findings whose recorded
  `required_paths` intersect `paths` by exact match or directory prefix.
  Keys that never received a `finding_opened` record have no recorded
  `required_paths`; they remain reachable by `for_key` and are excluded
  from `for_paths` without raising.
- No writes, ever: a missing journal path raises before a ledger object
  exists (the ledger's own open would create the file).

Production consumer: campaign-driver ingest annotation (EM-D2), via an
explicit `--history-roots` input — the driver today knows exactly one
`--campaign-root` per invocation, so multi-root history is a new input,
not existing knowledge. `for_paths` is additionally consumed by the FI
intake lane when built; within this graph its consumer is the ingest
annotation's context block.

### EM-3 Decision lifecycle [em-decisions]

Two decision object families exist in-tree and stay separate:
`schemas/decision.schema.json` governs run-local JSON decision records
(14 required fields, currently zero consumers);
`controller_kernel._decision_record` emits a different, smaller shape
that is out of scope here. ADR markdown headers are a third, parse-only
shape sharing the new fields' *semantics*. `load_decisions` normalizes
ADR headers and schema-conforming JSON records into one `Decision`
dataclass; it does not ingest kernel records.

- `schemas/decision.schema.json` → `schema_version` becomes
  `enum ["1.0", "1.1"]`; 1.1 adds optional `supersedes` (array of
  decision ids), `concerns_paths` (repo-relative path prefixes), and
  `valid_from_commit`; a conditional clause forbids all three on 1.0
  records so the version marker stays meaningful.
- `docs/decisions/TEMPLATE.md` gains optional `Supersedes:`,
  `Concerns-paths:`, `Valid-from-commit:` header lines after `Status:`
  (which stays within the first 12 lines, preserving the
  `check_repository_contracts.py` status-window convention even though
  that checker is not a gate here).
- Backfill: `Concerns-paths:` only, onto the eight numbered ADRs,
  headers only, bodies byte-identical. **No `Supersedes:` backfill** —
  no existing ADR supersedes another. ADR 0007's `Concerns-paths:` is
  pinned here: `harness_labs/featurerun/review_fix.py`,
  `harness_labs/plangraph/plan_graph.py`,
  `schemas/plan-graph-escalation-judgment.json`,
  `schemas/block-escalation.json`; the other seven are derived from each
  ADR's subject modules by the implementing run.
  `docs/decisions/README.md` says accepted records are immutable except
  status/supersession links; EM-C1 is explicitly authorized to add the
  header-only exemption there and to list both 0006 decisions in the
  index (the parallel-contract 0006 is currently missing from it).
- `harness_labs/core/decision_registry.py`: `load_decisions(root)`
  (decision ids synthesized from filenames; tolerant of wrapped header
  values like ADR 0007's two-line `Run:`) and
  `active_decisions_for_paths(paths)` returning accepted decisions not
  named in any other decision's `supersedes` whose `concerns_paths`
  intersect `paths` by prefix. A decision listed in a `supersedes` array
  while still carrying `status: accepted` is reported as an
  inconsistency, not silently resolved either way.

Production consumer: prepare-stage active-decision notices (EM-D1), so a
generated plan's gate evidence carries "these decisions govern your
paths" instead of "read the ADR directory".

### EM-4 Wiring [em-wiring]

**EM-D1 — admission (`plan_approval.py` only).** Both signals derive
purely from git blobs at `base_commit` of the repository
`prepare_approval` already receives; no new parameters on
`prepare_approval` or `issue_receipt`, so `scripts/approve_plan.py` and
every experiments launcher keep working unchanged, and issue-time
re-derivation is deterministic by construction.

- `REQUIRED_PATHS_IMPACT_WARNING`, severity `high` only: for each of a
  run's `path_intents` paths ending in `.py`, call
  `assess_required_paths` with the git-blob source against the run's
  `allowed_paths`; emit one warning per run aggregating the union of
  `missing`, with those paths in the warning's `paths` field so they
  participate in `warning_identity` (two different gaps must carry two
  different identities, and a changed gap must invalidate a stale
  acknowledgement). Flows through the existing high-warning
  acknowledgement gate.
- `gates["notices"]`, a new optional array in gate evidence (validator
  extended accordingly): carries unsupported-language impact entries
  (with the analyzer's reason) and `ACTIVE_DECISION_NOTICE` entries
  listing active decisions — read from git at `base_commit` using the
  same header shape `decision_registry` parses — whose `concerns_paths`
  intersect the plan's union of `allowed_paths`. Notices are re-derived
  and drift-checked at issue like the rest of gate evidence, but no
  acknowledgement gate scans them, so the campaign driver's all-severity
  acknowledgement rule cannot deadlock on them.

**EM-D2 — refinement and campaign driver.**

- `plan_refinement.py`: `_prepare` stays a pure function of strings (its
  documented git-independent contract) and the judgment protocol is
  unchanged. `refine_repository_decomposition` — which already holds
  `repository` — computes impact assessments once and passes them into
  `refine_decomposition` via a new keyword-with-default input; they
  surface in the refinement outcome's advisories ("reported, never
  repaired"). Making the judge consume them is deferred: it requires a
  `plan-refinement-judgment/1` protocol extension.
- `scripts/run_convergence_campaign.py`: new `--history-roots` input;
  at ingest, `for_key` lookups against the folded history; prior rulings
  are sealed as a recurrence-annotation artifact via the existing
  campaign artifact store, with the digest recorded in the checkpoint
  `state` keyed by finding key — the same open channel `measure` uses
  for `pending_audit_digest`. Journal bytes are untouched. Volume is
  bounded by arriving findings: one annotation per arriving key, exact
  key match only at ingest.
- `docs/development/INDEX.md` registers the new modules.

## Node objectives [em-objectives]

<!-- EM-OBJECTIVES:BEGIN -->
**EM-A.** Build the impact-analysis core: harness_labs/plangraph/impact_analysis.py reading file bytes through an injected source callable (filesystem-backed in tests; git-blob-backed at admission) with module_neighborhood (AST walk covering deferred in-function imports, generalizing the technique of scripts/dev/check_import_boundaries.py without importing it) and assess_required_paths returning an ImpactAssessment carrying supported, reason, confirmed, and missing-with-edge-kinds. Non-Python and unparseable targets return supported=False with a reason — never a clean verdict, never an exception. Read-only analysis; no wiring into approval or refinement (that is EM-D1 and EM-D2). Fixture repositories are built inside the test's tmp path, not committed.

**EM-B.** Build the repo-scoped finding history. First add a public read-only per-key lineage accessor and named record-kind constants to harness_labs/plangraph/convergence_ledger.py (no new record kinds, no writer changes, existing journals readable unchanged). Then build harness_labs/plangraph/finding_history.py folding caller-declared (journal_path, campaign_label, repository_id) entries through that accessor, guarded by repository_id agreement, exposing for_key (exact-key lineage newest-entry-first with ruling dispositions and statements, base_commit, journal ordinals) and for_paths (exact and directory-prefix containment against recorded required_paths — no similarity retrieval). Folding never writes: a missing journal path raises before any ledger object is constructed. EM-B is the sole owner of convergence_ledger.py in this graph; consumers use the new accessor only.

**EM-C1.** Decision lifecycle data. Bump schemas/decision.schema.json to schema_version enum ["1.0", "1.1"], adding optional supersedes, concerns_paths, and valid_from_commit that are forbidden on 1.0 records. Extend docs/decisions/TEMPLATE.md with the optional Supersedes:, Concerns-paths:, and Valid-from-commit: header lines placed after Status: (keeping Status: within the first 12 lines). Backfill Concerns-paths: onto the eight numbered ADRs — header lines only, bodies byte-identical; NO Supersedes: backfill, because no existing ADR supersedes another: the two 0006 files are a numbering collision between distinct active decisions, and ADR 0007's amendment is body prose, not a supersession. Record both 0006 decisions in docs/decisions/README.md's index and add the header-only amendment exemption sentence there.

**EM-C2.** Decision registry: harness_labs/core/decision_registry.py (core layer, imports core only) with load_decisions parsing ADR markdown headers (decision ids synthesized from filenames; tolerant of wrapped multi-line header values) and schema-1.1 JSON records into one Decision dataclass carrying id, status, supersedes, concerns_paths, valid_from_commit, and source_path, plus active_decisions_for_paths by directory-prefix intersection. Status/supersedes contradictions are reported as explicit inconsistencies, never resolved silently. This registry reads ADR files and JSON records conforming to decision.schema.json only; it does not ingest controller_kernel decision records, whose shape is different and out of scope.

**EM-D1.** Wire impact analysis and decision notices into admission: harness_labs/plangraph/plan_approval.py derives both purely from git blobs at base_commit of the repository it already receives — REQUIRED_PATHS_IMPACT_WARNING (high severity only; for each .py path_intent of a run, assess_required_paths against the run's allowed_paths, one warning per run aggregating the union of missing paths, which participate in warning_identity via the paths field) into gates["warnings"] through the existing acknowledgement machinery, and informational entries (unsupported-language impact results, ACTIVE_DECISION_NOTICE) into a new optional gates["notices"] array that no acknowledgement gate scans and issue_receipt re-derives deterministically. No new prepare_approval or issue_receipt parameters, no new plan payload fields, and identities of pre-existing warning kinds unchanged.

**EM-D2.** Wire the remaining consumers. plan_refinement.refine_repository_decomposition computes impact assessments once and passes them into refine_decomposition through a new keyword-with-default advisories input reported in the refinement outcome (judgment protocol unchanged; _prepare stays git-independent and pure). scripts/run_convergence_campaign.py gains an explicit --history-roots input, consults finding_history at ingest, seals a recurrence-annotation artifact via the existing campaign artifact store, and records its digest in the checkpoint state keyed by finding key. docs/development/INDEX.md registers the new modules. The campaign approve path must remain deadlock-free end to end with the new warning and notice kinds present. Consumes EM-B's public ledger accessor; does not edit convergence_ledger.py.
<!-- EM-OBJECTIVES:END -->

## Acceptance criteria [em-criteria]

<!-- EM-CRITERIA:BEGIN -->
- **AC-EM-1**: `module_neighborhood` over a fixture repository, read through a filesystem-backed source callable, returns the files importing the target module — including one whose import statement is inside a function body (deferred import) — and the files the target imports.
- **AC-EM-2**: `assess_required_paths` on a fixture where one importer of `file` is absent from `required_paths` returns that path in `missing` with edge kind `imported_by`, and every declared neighborhood path in `confirmed`; the assessment computes no set of 'unrelated declared paths'.
- **AC-EM-3**: For a non-`.py` target file and for a `.py` file with a syntax error, `assess_required_paths` returns `supported is False` with a non-empty `reason` and raises nothing.
- **AC-EM-4**: A module-scoped fixture records a `{relative_path: sha256}` manifest of the fixture repository immediately after construction and re-asserts equality in teardown, including the absence of new files such as `__pycache__` directories.
- **AC-EM-5**: `fold_campaigns` over two caller-declared entries `(journal_path, campaign_label, repository_id)` for the same `repository_id` yields `for_key(file, subject)` lineage ordered newest-entry-first, each element carrying the derived terminal status, every `finding_ruled` disposition with its recorded `statement`, the caller-supplied campaign label, the `campaign_opened` record's `base_commit`, and each record's journal ordinal (line index) as the when-learned order.
- **AC-EM-6**: `for_paths(["pkg/sub"])` returns findings whose `required_paths` contain `pkg/sub/mod.py` (directory-prefix containment) and exact matches, does NOT return a finding whose only path is `pkg/subx/mod.py`, and excludes keys with no recorded `required_paths` without raising.
- **AC-EM-7**: Folding entries that declare different `repository_id` values raises a `finding_history` error naming both ids.
- **AC-EM-8**: `fold_campaigns` obtains records only through `convergence_ledger`'s public read surface and the named per-kind constants it exports; a journal the ledger rejects raises `ConvergenceLedgerError` out of `fold_campaigns`; `finding_history.py` contains no `state-ledger` record-kind string literal.
- **AC-EM-9**: `schemas/decision.schema.json` declares `schema_version` as `{"enum": ["1.0", "1.1"]}`, keeps `additionalProperties: false`, lists `supersedes`, `concerns_paths`, and `valid_from_commit` under `properties` and in no `required` array, and forbids all three when `schema_version` is `"1.0"`; a hand-written checker in `tests/test_decision_schema.py` (no third-party jsonschema import) accepts a 1.1 record carrying them, accepts a 1.0 record lacking them, rejects a 1.0 record carrying `supersedes`, and rejects a record with an undeclared field.
- **AC-EM-10**: `load_decisions("docs/decisions")` parses every numbered ADR's header block, synthesizing decision ids from filenames so the two files numbered 0006 load as distinct accepted decisions; a wrapped multi-line header value (as in ADR 0007's `Run:`) does not corrupt adjacent fields.
- **AC-EM-11**: `active_decisions_for_paths(["harness_labs/plangraph/plan_graph.py"])` contains every loaded decision with status `accepted` whose `concerns_paths` cover that path by directory prefix, and contains no decision named in any other loaded decision's `supersedes` (containment assertions, never set equality against the live ADR count).
- **AC-EM-12**: A fixture where decision X carries `status: accepted` while decision Y lists X in `supersedes` yields an explicit inconsistency report naming X and Y; X appears in neither the active set nor the superseded set without the inconsistency being surfaced.
- **AC-EM-13**: Each pre-existing file under `docs/decisions/` has body bytes (everything from the first `## ` heading onward) hashing to the sha256 recorded in `ADR_BODY_DIGESTS` in `tests/test_decision_schema.py`, captured from the pre-change tree; header lines before the first `## ` heading are the only permitted difference.
- **AC-EM-14**: `prepare_approval` on a fixture repository whose committed base tree contains a Python importer omitted from a run's `allowed_paths` emits `REQUIRED_PATHS_IMPACT_WARNING` with severity `high` naming the missing path and edge kind, with the missing paths in the identity-participating `paths` field so two distinct gaps carry distinct `warning_identity` values; the approve step refuses an operator approval lacking that warning's acknowledgement via the existing high-warning acknowledgement gate and accepts one carrying it.
- **AC-EM-15**: The same prepare against a run whose paths are all non-Python emits no `warnings` entry for impact and instead a `gates["notices"]` entry carrying the analyzer's `supported=False` reason; approval succeeds with no acknowledgement.
- **AC-EM-16**: The campaign driver's ingest step, given `--history-roots` naming a prior campaign whose journal ruled a key `waive` (a `finding_ruled` record whose disposition folds to terminal status `excluded`), seals a recurrence-annotation artifact through the existing campaign artifact store carrying the prior disposition, its `statement`, and the campaign label, and records the artifact digest in the checkpoint `state` keyed by finding key.
- **AC-EM-17**: The recurrence annotation leaves the current campaign's ledger journal bytes unchanged, and the folded ledger reports no record kinds beyond the existing `state-ledger` vocabulary.
- **AC-EM-18**: Prepare emits an `ACTIVE_DECISION_NOTICE` entry in `gates["notices"]` listing each active decision — read from git at `base_commit`, via the same header shape `decision_registry` parses — whose `concerns_paths` intersect the plan's union of `allowed_paths`; a superseded decision covering the same paths is absent; no acknowledgement gate scans `notices`.
- **AC-EM-19**: `refine_repository_decomposition` computes impact assessments once and passes them into `refine_decomposition` through a keyword-with-default input surfaced in the refinement outcome's advisories; with the input omitted and `judge=None`, revised plans over the fixtures in `tests/test_plan_refinement.py` hash to the values pinned in `PRE_CHANGE_REVISION_DIGESTS`, captured from pre-change behaviour (`judge=None` still applies its intent-aware narrowing; the invariant is no drift, not no-op).
- **AC-EM-20**: `warning_identity` of each pre-existing warning kind over the existing fixtures equals hex constants pinned in `tests/test_plan_approval.py`, captured from pre-change behaviour.
- **AC-EM-21**: `python3 -m pytest tests/test_import_boundaries.py -q` passes with `decision_registry` in core importing core only, and `impact_analysis` and `finding_history` in plangraph; no core module imports the plangraph layer.
- **AC-EM-22**: `python3 -m pytest tests/ -q` passes at EM-D2's `suite` verification gate with no test skipped for reasons introduced by this graph.
- **AC-EM-23**: `fold_campaigns` raises on a non-existent journal path before constructing any ledger object, and folding leaves the journal roots byte-identical with no new files or directories created.
- **AC-EM-24**: An end-to-end driver sequence — prepare, approval packet, issue — over a fixture campaign completes without requiring any acknowledgement when only notices (active-decision, unsupported-language impact) are present, and completes with one high impact warning present once exactly that warning is acknowledged.
- **AC-EM-25**: After prepare, mutating working-tree files under a granted path and appending records to an unrelated campaign journal changes nothing: `issue_receipt` re-derives byte-identical warnings and notices from git at `base_commit` and issuance succeeds.
<!-- EM-CRITERIA:END -->

## Non-goals and deferred [em-deferred]

- **No gating on impact analysis** until a full campaign has measured
  its false-positive rate (the conformance-arming precedent: report
  always, block only when armed).
- **No recurrence signal at admission.** Finding history depends on
  mutable, gitignored journals; surfacing it inside the re-derived gate
  evidence would make issuance non-deterministic. It lives at campaign
  ingest. Trigger to revisit: a pinned-snapshot design that seals the
  folded history as an artifact referenced from the subject.
- **No judge-visible impact advisories.** Requires extending the
  `plan-refinement-judgment/1` request payload. Trigger: refinement
  rounds demonstrably narrowing the wrong grants for lack of impact
  context.
- **No cross-repository history**, no embedding retrieval, no lesson
  synthesis. Trigger: FI intake lands and needs ranked retrieval beyond
  exact key + containment.
- **No general bitemporal store** (memex-style). Journal ordinal +
  `base_commit` is sufficient until a consumer needs interval queries.
- **No non-Python impact backend** in this graph. The `supported=False`
  channel is the honest placeholder; a tree-sitter backend is a separate
  follow-up with its own dependency decision.
- **No third-party dependencies.** The repo has no dependency manifest;
  `tests/test_decision_schema.py` asserts the schema structurally and
  with a hand-written checker rather than importing `jsonschema`.
- **No `Supersedes:` backfill and no ADR body rewrites**: only headers
  are touched, and only `Concerns-paths:` is added.
- **`docs/development/serial_implementation_decisions.jsonl`** and
  controller-kernel decision records stay outside the registry.

## Risks [em-risks]

- Impact false positives on dynamic imports / re-exports: mitigated by
  high-severity-with-acknowledgement rather than rejection; measured via
  the acknowledgement reasons operators record.
- The `notices` key changes `gate-evidence.json` shape: additive and
  optional; the validator change and the issue-time re-derivation land
  together in EM-D1, and AC-EM-25 pins determinism.
- ADR backfill mis-scopes `Concerns-paths`: 0007's set is pinned in this
  plan; the rest are reviewed as part of EM-C1's diff, and AC-EM-13's
  body-digest goldens prevent anything beyond headers.
- Ledger accessor shape is implementer freedom but a public contract:
  EM-B records it as a decision record; EM-D2 consumes it and must not
  edit the ledger.
- History fold cost at ingest: bounded by explicit `--history-roots`
  and single-digit campaign counts today; a cached index is a follow-up,
  not a prerequisite.
- Full-suite wall time at EM-D2's `suite` gate: 3600s matches the
  existing functionality-test budget (968 tests pass well inside it at
  base).

## Build order [em-order]

EM-A (impact core) ∥ EM-B (history core + ledger accessor) ∥ EM-C1
(decision data) → EM-C2 (decision registry), then EM-D1 (admission
wiring), then EM-D2 (refinement + driver wiring). Grants are pairwise
disjoint; EM-B is the sole owner of `convergence_ledger.py`; EM-D1 is
the sole owner of `plan_approval.py`; EM-D2 is the sole owner of
`plan_refinement.py` and the campaign driver.
