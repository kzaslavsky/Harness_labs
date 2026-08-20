# Delta-to-run pipeline: finding intake, sanitizer policy, measurer commissioning, launcher kit

Implements the four missing lanes of the pipeline that turns statements about
required or deviant behavior into an approved, running PlanGraph
(`docs/development/SESSION_HANDOFF_DELTA_TO_RUN_20260820.md`). Base: main at
`8a13917`. The six driving findings were transcribed in-session and
round-tripped through the real `ConvergenceLedger.ingest_audit` validator;
the sealed seed artifact lands with DTR-FI's batch mode as its first fixture.

Revision 2, after three-lens review (adversarial decomposition,
source-binding, design/pitfalls). Material corrections from review: FI
sealing no longer folds mid-round (fabricated-`unobserved` invariant); the
campaign-open checklist seat is `build_campaign_config`/`pin_target`, not a
nonexistent `open_campaign` in `convergence_campaign.py`; SN owns the
driver-side config consumer; LK split into an independent launcher-kit node
and a sink synthesis node; base repinned from `59c7fd9`.

## Problem and current behaviour [dtr-problem]

The convergence machinery on main covers steps 0 and 2–7 of the pipeline
(target pinning, canonicalize/validate, refine, prepare, approve, issue,
register/run). Four gaps remain:

- **DTR-F1** `(harness_labs/plangraph/finding_intake.py, intake-pipeline-absent)`:
  no path from a free-text operator statement or seed-audit transcript to a
  contract-valid finding; statements reach the ledger only as hand-authored
  audit artifacts.
- **DTR-F2/F3** `(scripts/ui_fidelity_capture.py, sanitizer-media-type-policy-absent
  / capture-dry-run-absent)`: the sanitizer is one hook applied uniformly to
  every artifact kind, and the only way to exercise a policy is a real
  journaled capture.
- **DTR-F4** `(scripts/commission_measurer.py, measurer-commissioning-absent)`:
  no pre-campaign calibration; capture instability and inspector recall
  problems surface mid-campaign, at the first post-repair audit.
- **DTR-F5/F6** `(harness_labs/graphrun/campaign_launcher.py,
  campaign-launcher-not-extracted / harness_labs/plangraph/plan_synthesis.py,
  plan-synthesis-step-absent)`: the proven launcher is experiments-grade with
  hardcoded values, and nothing synthesizes an observable-stamped
  decomposition from the ledger's open findings.

## Design [dtr-design]

Layering: `finding_intake` and `plan_synthesis` are plangraph-layer (they
import the ledger and plan contracts). `measurer_commissioning` is
core-layer and therefore **plain JSON in / plain JSON out**: it imports
nothing from `harness_labs.plangraph`, consumes seed findings as mappings
supplied by path, and leaves sealing and checklist wiring to
`scripts/commission_measurer.py` and the campaign driver
(`scripts/dev/check_import_boundaries.py` derives layer from directory, so
the new core module is covered with no checker edit). `campaign_launcher`
joins the existing `harness_labs/graphrun` package; `plangraph` may not
import `graphrun`, so the launcher kit and plan synthesis stay decoupled.
No real-browser dependency enters CI: capture-driven tests run the stub
driver (`--driver stub`), and anything needing a real interpreter follows
the `UI_FIDELITY_PYTHON` skip-with-recorded-reason pattern already in
`tests/test_ui_fidelity_capture.py`.

### FI — finding intake [dtr-fi]

`harness_labs/plangraph/finding_intake.py` turns statements into
contract-valid findings and seals them.

- `draft_finding(statement, *, repo_root, target, evidence_refs=())` returns
  either a `DraftFinding` (fields exactly matching the ledger's
  `_validate_finding` envelope — all twelve: `file`, `subject`,
  `required_paths`, `confidence`, `supersedes_key`, `id`, `statement`,
  `category`, `severity`, `requires_disposition`, `evidence_refs`,
  `source_finding_ids`) or an `IntakeQuestion` naming the ambiguity —
  candidate `required_paths` sets, or an undecidable subject. Ambiguity is
  never resolved by guessing; the question is the return value.
- Root-causing `required_paths` searches the working tree for the code that
  owns the stated behavior; `file` is the primary owner and must be a member
  of `required_paths` (the validator enforces it).
- `confidence` is `"S"` unless capture evidence refs are attached (then
  `"C+S"`); `requires_disposition=True` for judgment calls.
- Every emitted finding round-trips `ConvergenceLedger.ingest_audit` in
  tests — the REAL validator on a scratch ledger, not a mirror of its rules.
- Sealing does NOT fold: `seal_findings(findings, store)` builds the
  audit-artifact envelope `{digest, findings, verdicts: [],
  confirmed_good: [], capture_coverage: {}}`, writes it to a temp file, and
  seals it via `CampaignArtifactStore.seal` (note:
  `seal_audit_result` seals only a result's `evidence_refs` files, never the
  envelope — it is not the API for this). `convergence_campaign.py` is
  imported, not modified. The sealed artifact is carried for the next
  round's real measure/ingest path; folding an operator statement through
  `ingest_audit` mid-round would mark every other open key `unobserved` and
  fabricate failed repair claims — the exact refusal
  `scripts/run_convergence_campaign.py` documents for harvested findings.
- `scripts/report_finding.py`: single-statement mode (operator messages a
  session mid-campaign; the session appends a keyed finding for the next
  round) and `--batch` mode transcribing a seed-audit file of statements
  into one sealed artifact. Takes explicit `--ledger`/`--campaign-root`
  arguments; no driver subcommand is added. Re-running with byte-identical
  input reseals the same digest and is a no-op.

### SN — sanitizer media-type policy [dtr-sn]

Generalize the sanitizer from one uniform hook to a per-artifact-kind
policy, mechanism-only (the PHI policy content is product config). Two
distinct existing surfaces are involved — name them precisely:
`resolve_sanitizer`/`sanitize_before_journal` in
`scripts/ui_fidelity_capture.py` (capture-side, dispatches on artifact
`kind`, raises `SanitizerError`, configured by CLI flag) and
`resolve_pre_journal_sanitizer`/`sanitize_before_journaling` in
`scripts/run_convergence_campaign.py` (driver-side, reads
`CONFIG_SANITIZER_KEY` from campaign config, raises `SanitizerFailure`).

- Campaign config accepts the legacy string form (uniform hook, unchanged
  semantics, byte-identical `campaign_opened.config` mapping) or a mapping
  `{"text": <hook-ref>, "binary": {"<kind>": "scan"|"admit:<reason>"|"reject"}}`.
- Driver side: `sanitize_before_journaling` resolves the `text` hook out of
  the mapping form and applies it to journaled text exactly as the legacy
  string form does; a mapping with no `text` entry raises
  `SanitizerFailure`, never an `AttributeError` (today a mapping value
  would crash `resolve_pre_journal_sanitizer`'s `.partition(":")`).
- Capture side: the policy reaches the capture CLI as
  `--sanitizer-policy <json-file>` holding the mapping form; `--sanitizer`
  keeps its legacy string meaning and the two are mutually exclusive.
  Text-kind artifacts pass through the scanning hook; binary kinds follow
  their declared policy; an undeclared binary kind is refused — fail closed.
- `--dry-run`: run the resolved sanitizer over a sample bundle, emit a
  per-artifact report of would-be rejections (naming the refusing rule),
  and journal nothing.

### MC — measurer commissioning [dtr-mc]

Pre-campaign calibration phase.

- `harness_labs/core/measurer_commissioning.py`: run the capture matrix N
  times through an **injected runner callable** (the same seam
  `driver.measure` already uses), emit a per-cell stability report
  (stable/unstable per a declared divergence threshold, threshold recorded
  in the report); chronically unstable cells are surfaced as explicit
  ruling requests — commissioning exits nonzero while any cell is unstable
  and unruled. The stability classification is exercised over stub-driver
  captures in CI and never skips.
- Inspector recall calibration: score the inspector against a seed-findings
  list of ledger finding envelopes supplied as a JSON path argument (the
  shape `finding_intake --batch` emits; passed as data — the core module
  imports nothing from plangraph); emit a recall report artifact.
- `scripts/commission_measurer.py` drives both and seals both reports via
  `CampaignArtifactStore`.
- Campaign-open checklist: `build_campaign_config` in
  `harness_labs/plangraph/convergence_campaign.py` gains
  `stability_report_digest` and `recall_report_digest` keys and refuses a
  config lacking them unless it carries
  `commissioning_override: {"reason": <non-empty>}`; the refusal names each
  missing artifact. `pin_target` therefore cannot record a
  `campaign_opened` config without them. This is a default-refuse change:
  every existing `pin_target`/`open_campaign` call site in
  `tests/test_convergence_campaign.py`, `tests/test_convergence_campaign_driver.py`,
  and `tests/test_convergence_lifecycle.py` is migrated in this run (grants
  cover all three); already-folded journals are not re-validated.
- Recall wiring: the driver's `close` step derives `inspector_recall` from
  the sealed recall report when the caller does not supply one, so the
  calibrated number actually reaches the recall-threshold gate.
- The checklist is a new refusal authority; an ADR
  (`docs/decisions/0008-campaign-open-commissioning-checklist.md`) records
  it.

### LK — launcher kit and plan synthesis [dtr-lk]

Two independent subsystems, two nodes.

**DTR-LK-KIT** — `harness_labs/graphrun/campaign_launcher.py` extracts the
proven experiments launcher behind a product-config surface.
`build_campaign_launch_config()` returns a plain mapping pinned by test
against a literal golden dict transcribing
`experiments/run_convergence_plan_graph.py` at base `8a13917`: coordinator
spec and 7200.0s silence tolerance, implementer/reviewer specs and models,
`recovery_limit=5`, `continuation_recovery_limit=3`,
`verification_repair_limit=3`, `allow_dirty_baseline=True`,
`require_repository_change=True`, `candidate_only=True`, `merge=False`,
`max_parallelism=5`, and the product-specific values (plan/decomposition
paths, logical graph id, agent-mixture specs, profile-builder hook,
operator-notes directory — parameterized, today hardcoded to
`logs/plan-approval/operator-notes/<node>.md`). Two deliberate widenings
over the source, documented as such because the shim-parity criterion is
scoped to the config surface only:
- anti-placeholder hardening becomes a single shared
  `ANTI_PLACEHOLDER_FLOOR` constant included in implementer, fix, review,
  AND verify instructions (the source carries two divergent copies in
  implementer and fix only);
- CC-08 wiring: the kit's config surface carries an `escalation_judge` seat
  and adds `transfer_ownership` to `automatic_recovery.allowed_actions`
  with a `max_structural_decisions` bound, per ADR 0007 — the source
  launcher predates CC-08 and cannot unseal at all.
Operator-notes stay folded into implementer, review, and fix instructions
(not verify — matching the source). ADR 0007 demoted the notes file as
untyped free text invisible to the audit journal; the kit keeps it as the
transport while the typed replacement (folding ledger `finding_ruled`
records into reviewer instructions) is future work, noted here so the
ruling is not re-litigated. The experiments script becomes a thin shim: a
source scan asserts `run_plan_graph_feature_worktree` no longer appears in
it.

**DTR-LK-SYN** — `harness_labs/plangraph/plan_synthesis.py`: open findings →
decomposition JSON. `ConvergenceLedger` gains
`open_findings() -> tuple[dict, ...]` returning the folded finding envelope
for each key in `open_set()` (today `open_set` returns bare keys and the
envelopes are reachable only by re-folding the journal); `plan_synthesis`
consumes that accessor and never re-folds. Each run's `allowed_paths` is
the union of its findings' `required_paths` (ownership derives from
`required_paths` alone); every grant gets a path intent; every criterion
text carries a trailing `OBSERVABLE:{"kind": ..., "referent": ...}`
annotation, so no criterion trips `S5_MISSING_OBSERVABLE` (and the payload
is conformance-aware); output round-trips `canonical_plan_graph_payload`
unchanged and satisfies the driver's round contract —
`join_regression_node_id` finds a join regression node and
`validate_round_grants` passes. New payload fields are forbidden — the
top-level key set is closed. Driver wiring: the `plan` step invokes
synthesis, and the approve path threads `enforce=True` through BOTH
`render_approval_packet` (prepare) and `issue_approval` (issue) — the
issue side re-derives the gate and refuses on drift, so prepare-only
threading would trip the TOCTOU guard. The `enforce` path is driver-only;
`scripts/approve_plan.py` is unchanged.

## Tests [dtr-tests]

Each lane lands with its own test module
(`tests/test_finding_intake.py`, capture/campaign/driver additions in
`tests/test_ui_fidelity_capture.py`, `tests/test_convergence_campaign.py`
and `tests/test_convergence_campaign_driver.py`,
`tests/test_measurer_commissioning.py`, `tests/test_campaign_launcher.py`,
`tests/test_plan_synthesis.py`, ledger-accessor tests in
`tests/test_convergence_ledger.py`, checklist migration in
`tests/test_convergence_lifecycle.py`). FI tests validate against the real
ledger, never a copy of its rules. The full-suite gate
(`python3 -m pytest tests/ -q`) holds at the sink against base `8a13917`'s
collected set (1027 tests), with skip count no greater than the base's.

## Build order [dtr-build-order]

`DTR-FI ∥ DTR-SN ∥ DTR-LK-KIT` (pairwise-disjoint grants) → `DTR-MC`
(consumes FI's seed-transcription shape as data and SN's policy/driver
changes; shares `convergence_campaign.py` and the driver with SN,
serialized by `depends_on`) → `DTR-LK-SYN` (single writer of the driver
after MC; sink node, runs the lane gate plus the full-suite gate).

## Risks [dtr-risks]

- Wrong `required_paths` attribution in FI recreates scope-fence churn — the
  dominant cost of the CC campaign (ADR 0007). Mitigation: ambiguity returns
  an `IntakeQuestion`, never a guess, and tests pin the attribution
  behavior.
- Sanitizer generalization must not change the semantics or the
  `campaign_opened.config` mapping of existing legacy-string configs
  (existing journals must re-fold identically). The MC checklist keys
  change the config for NEW campaigns only; already-folded journals are not
  re-validated.
- Commissioning "against the real target" is not CI-runnable; CI exercises
  the injected-runner seam with the stub driver, and the threshold-ruling
  path must not require a real browser.
- The launcher extraction pins parity via a golden config dict transcribed
  at `8a13917`; the two documented widenings (shared anti-placeholder
  floor, CC-08 escalation wiring) are outside the parity scope by
  construction.
- The sink's full-suite gate is the collector for any pre-existing flake
  (cf. `docs/development/relax-gate-timeout-flake-20260819.md`; the
  finalize-gate timeout flake itself was fixed at `706e555`, and pitfall 4's
  empty-retry-frontier refusal is mitigated in-graph by CC-08). A flake
  fired there blocks a node that cannot legally touch the flaky file;
  mitigation is operational (rerun, escalate), not decompositional.
- `docs/development/INDEX.md` is updated by the sink node alongside the
  final gate so the plan/decomposition pair is discoverable.
